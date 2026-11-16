"""Prescribing, used by the Consult page (consult.py).

Prescribing happens inside the consultation: once the doctor has a diagnosis, the
Consult page lists the drugs for it (ToAddress), each safety-checked against the
patient; picking one opens the dose, dates and alerts. This module holds that logic:

  drug_options(...)        drugs for a condition, each with its safety check
  parse_prescription(...)  validate dose (template or custom), dates, alerts + reason
  insert_prescription(...) PostgreSQL rows, inside the caller's transaction
  write_audit(...)         MongoDB prescription_audit (retried later if MongoDB is down)

/prescribe remains only as a redirect to Consult, so old links still work.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import Blueprint, redirect, request, url_for

from . import outbox, safety
from .auth import role_required
from .db import query, query_one
from .util import dose_text, now, today

bp = Blueprint("doctor", __name__)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@bp.route("/prescribe")
@role_required("doctor")
def prescribe():
    nric = (request.args.get("nric") or "").strip()
    return redirect(url_for("consult.visit", nric=nric) if nric else url_for("consult.start"))


def drug_options(nric, disease_id, drug_id=None):
    """Drugs linked to the condition, plus one picked by search, each checked against the
    patient. Returns (candidates, selected): fewest alerts first, searched drug last."""
    cands = query("""
        SELECT d.DrugID, d.GenericName FROM ToAddress ta JOIN Drug d ON d.DrugID = ta.DrugID
        WHERE ta.DiseaseID = %s ORDER BY d.GenericName""", (disease_id,))
    ids = [c["drugid"] for c in cands]
    if drug_id and drug_id not in ids:
        extra = query_one("SELECT DrugID, GenericName FROM Drug WHERE DrugID = %s", (drug_id,))
        if extra:
            extra["searched"] = True
            cands.append(extra)
            ids.append(drug_id)
    templates = {}
    for t in query("""SELECT * FROM PrescriptionTemplate WHERE DrugID = ANY(%s)
                      ORDER BY DrugID, PrescriptionTemplateID""", (ids,)):
        templates.setdefault(t["drugid"], []).append(t)
    checks = safety.check(nric, ids) if ids else {}
    for c in cands:
        c["templates"] = templates.get(c["drugid"], [])
        c["check"] = checks[c["drugid"]]
    cands.sort(key=lambda c: (c.get("searched", False), len(c["check"]["alerts"]), c["genericname"]))
    selected = next((c for c in cands if c["drugid"] == drug_id), None)
    return cands, selected


def parse_prescription(f, nric, drug_id):
    """Read the prescription part of the form. Returns (rx, errors); rx is None on errors.
    The safety check is run again here: the page is never trusted."""
    errors = []
    drug = query_one("SELECT DrugID, GenericName FROM Drug WHERE DrugID = %s", (drug_id,))
    if not drug:
        return None, ["That drug wasn't found. Choose it again."]

    choice = f.get("template", "")
    template, custom = None, None
    if choice == "custom":
        try:
            amount = Decimal(f.get("dose_amount", "").strip())
            if amount <= 0 or amount > 100000:
                raise InvalidOperation
        except InvalidOperation:
            errors.append("Enter the dose as a number, e.g. 500.")
            amount = None
        unit = f.get("dose_unit", "").strip()[:20]
        freq = f.get("frequency", "").strip()[:50]
        if not unit:
            errors.append("Enter the dose unit, e.g. mg or tablet.")
        if not freq:
            errors.append("Enter how often, e.g. twice daily after food.")
        custom = {"dose_amount": amount, "dose_unit": unit, "frequency": freq,
                  "route": (f.get("route") or "ORAL").strip().upper()[:50],
                  "dosage_form": (f.get("dosage_form") or "").strip().upper()[:80] or None}
    else:
        template = query_one("SELECT * FROM PrescriptionTemplate WHERE PrescriptionTemplateID = %s AND DrugID = %s",
                             (_int(choice), drug_id))
        if not template:
            errors.append("Choose a dose, or write your own.")

    try:
        start = date.fromisoformat(f.get("start", ""))
    except ValueError:
        start = None
        errors.append("Enter a start date.")
    days_raw = f.get("days", "").strip()
    days = _int(days_raw) if days_raw else None
    if days_raw and (days is None or not 1 <= days <= 365):
        errors.append("Duration must be between 1 and 365 days, or left blank for ongoing.")
    if start and start < today():
        errors.append("The start date can't be in the past.")

    alerts = safety.check(nric, [drug_id])[drug_id]["alerts"]
    reason = f.get("reason", "").strip()
    if alerts and not reason:
        errors.append(f"There {'is 1 alert' if len(alerts) == 1 else f'are {len(alerts)} alerts'} for "
                      f"{drug['genericname'].lower()}. Give a reason to proceed, or choose another medicine.")
    if errors:
        return None, errors
    return {"drug": drug, "template": template, "custom": custom, "start": start,
            "end": safety.end_date(start, days), "alerts": alerts, "reason": reason}, []


def insert_prescription(cur, nric, doctor_id, disease_id, rx):
    """Diagnosis (found, or created today), custom template if any, and the Prescription
    as Pending. Runs inside the caller's transaction; returns (rx_id, dx_id, new_dx)."""
    cur.execute("""SELECT DiagnosisID FROM Diagnosis WHERE NRIC = %s AND DiseaseID = %s
                   ORDER BY DateOfDiagnosis DESC, DiagnosisID DESC LIMIT 1""", (nric, disease_id))
    row = cur.fetchone()
    new_dx = row is None
    if new_dx:
        cur.execute("""INSERT INTO Diagnosis (NRIC, DoctorID, DiseaseID, DateOfDiagnosis)
                       VALUES (%s, %s, %s, CURRENT_DATE) RETURNING DiagnosisID""", (nric, doctor_id, disease_id))
        row = cur.fetchone()
    if rx["custom"]:
        c = rx["custom"]
        cur.execute("""INSERT INTO PrescriptionTemplate (DrugID, dose_amount, dose_unit, frequency, route, dosage_form)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                    (rx["drug"]["drugid"], c["dose_amount"], c["dose_unit"], c["frequency"], c["route"], c["dosage_form"]))
        rx["template"] = cur.fetchone()
    cur.execute("""INSERT INTO Prescription (DiagnosisID, PrescriptionTemplateID, PrescriptionStart, PrescriptionEnd, ReviewStatus)
                   VALUES (%s, %s, %s, %s, 'Pending') RETURNING PrescriptionID""",
                (row["diagnosisid"], rx["template"]["prescriptiontemplateid"], rx["start"], rx["end"]))
    return cur.fetchone()["prescriptionid"], row["diagnosisid"], new_dx


def write_audit(rx_id, dx_id, nric, user, rx, encounter_id=None, advice=None):
    """MongoDB prescription_audit: the alerts the doctor saw, the reason, and the advice
    given to the patient (shown to the pharmacist). Returns False if it was queued."""
    t = rx["template"]
    ingredients = [r["ingredientname"] for r in query("""
        SELECT ai.IngredientName FROM Contains c JOIN ActiveIngredients ai ON ai.IngredientID = c.IngredientID
        WHERE c.DrugID = %s ORDER BY 1""", (rx["drug"]["drugid"],))]
    event = {"at": now(), "action": "prescribed", "by": {"role": "doctor", "id": user["id"], "name": user["name"]}}
    if rx["alerts"]:
        event["alertsAcknowledged"] = len(rx["alerts"])
        event["reason"] = rx["reason"][:1000]
    audit = {
        "_id": rx_id, "nric": nric, "diagnosisId": dx_id,
        "drug": {"drugId": rx["drug"]["drugid"], "name": rx["drug"]["genericname"], "ingredients": ingredients,
                 "dose": f"{dose_text(t)}, {(t.get('route') or '').lower()}".strip(", ")},
        "prescribedBy": {"doctorId": user["id"], "name": user["name"]},
        "status": "Pending", "alerts": safety.mongo_safe_alerts(rx["alerts"]), "events": [event],
    }
    if encounter_id:
        audit["encounterId"] = encounter_id
    if advice:
        audit["advice"] = advice
    return outbox.run("prescription_audit", "insert_one", audit)

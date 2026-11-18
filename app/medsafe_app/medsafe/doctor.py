"""Prescribing, used by the Consult page (consult.py).

Prescribing happens inside the consultation: once the doctor has a diagnosis, the
Consult page lists the drugs for it (ToAddress), each safety-checked against the
patient; ticking one or more opens a dose card for each. Medicines ticked in the same
visit are also checked against each other. This module holds that logic:

  drug_options(...)        drugs for a condition, each with its safety check
  parse_prescription(...)  validate one dose card (template or custom), dates, alerts + reason
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


MAX_PER_VISIT = 6


def drug_options(nric, disease_id, drug_ids=()):
    """Drugs linked to the condition, plus any picked by search, each checked against the
    patient and against the other medicines picked in this visit.
    Returns (candidates, selected): candidates fewest alerts first, searched drugs last;
    selected in the order they were picked."""
    drug_ids = list(dict.fromkeys(drug_ids))
    cands = query("""
        SELECT d.DrugID, d.GenericName FROM ToAddress ta JOIN Drug d ON d.DrugID = ta.DrugID
        WHERE ta.DiseaseID = %s ORDER BY d.GenericName""", (disease_id,))
    ids = [c["drugid"] for c in cands]
    missing = [d for d in drug_ids if d not in ids]
    if missing:
        for extra in query("SELECT DrugID, GenericName FROM Drug WHERE DrugID = ANY(%s)", (missing,)):
            extra["searched"] = True
            cands.append(extra)
            ids.append(extra["drugid"])
    picked = [d for d in drug_ids if d in ids]
    templates = {}
    for t in query("""SELECT * FROM PrescriptionTemplate WHERE DrugID = ANY(%s)
                      ORDER BY DrugID, PrescriptionTemplateID""", (ids,)):
        templates.setdefault(t["drugid"], []).append(t)
    checks = safety.add_together(safety.check(nric, ids), picked) if ids else {}
    for c in cands:
        c["templates"] = templates.get(c["drugid"], [])
        c["check"] = checks[c["drugid"]]
        c["picked"] = c["drugid"] in picked
    cands.sort(key=lambda c: (c.get("searched", False), len(c["check"]["alerts"]), c["genericname"]))
    by_id = {c["drugid"]: c for c in cands}
    return cands, [by_id[d] for d in picked]


def parse_prescription(f, drug, alerts):
    """Read one medicine's dose card. Its fields are suffixed with the DrugID (template_1104,
    days_1104, ...). `alerts` is the safety check, run again by the caller on save: the page
    is never trusted. Returns (rx, errors); rx is None on errors."""
    errors = []
    drug_id = drug["drugid"]
    name = drug["genericname"].lower()
    f = {k[: -len(f"_{drug_id}")]: v for k, v in f.items() if k.endswith(f"_{drug_id}")}

    choice = f.get("template", "")
    template, custom = None, None
    if choice == "custom":
        try:
            amount = Decimal(f.get("dose_amount", "").strip())
            if amount <= 0 or amount > 100000:
                raise InvalidOperation
        except InvalidOperation:
            errors.append(f"{name}: enter the dose as a number, e.g. 500.")
            amount = None
        unit = f.get("dose_unit", "").strip()[:20]
        freq = f.get("frequency", "").strip()[:50]
        if not unit:
            errors.append(f"{name}: enter the dose unit, e.g. mg or tablet.")
        if not freq:
            errors.append(f"{name}: enter how often, e.g. twice daily after food.")
        custom = {"dose_amount": amount, "dose_unit": unit, "frequency": freq,
                  "route": (f.get("route") or "ORAL").strip().upper()[:50],
                  "dosage_form": (f.get("dosage_form") or "").strip().upper()[:80] or None}
    else:
        template = query_one("SELECT * FROM PrescriptionTemplate WHERE PrescriptionTemplateID = %s AND DrugID = %s",
                             (_int(choice), drug_id))
        if not template:
            errors.append(f"{name}: choose a dose, or write your own.")

    try:
        start = date.fromisoformat(f.get("start", ""))
    except ValueError:
        start = None
        errors.append(f"{name}: enter a start date.")
    days_raw = f.get("days", "").strip()
    days = _int(days_raw) if days_raw else None
    if days_raw and (days is None or not 1 <= days <= 365):
        errors.append(f"{name}: duration must be between 1 and 365 days, or left blank for ongoing.")
    if start and start < today():
        errors.append(f"{name}: the start date can't be in the past.")

    reason = f.get("reason", "").strip()
    if alerts and not reason:
        errors.append(f"There {'is 1 alert' if len(alerts) == 1 else f'are {len(alerts)} alerts'} for "
                      f"{name}. Give a reason to proceed, or untick it.")
    if errors:
        return None, errors
    return {"drug": {"drugid": drug_id, "genericname": drug["genericname"]}, "template": template,
            "custom": custom, "start": start,
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
    cur.execute("""INSERT INTO Prescription (DiagnosisID, PrescriberID, PrescriptionTemplateID, PrescriptionStart,
                                             PrescriptionEnd, ReviewStatus)
                   VALUES (%s, %s, %s, %s, %s, 'Pending') RETURNING PrescriptionID""",
                (row["diagnosisid"], doctor_id, rx["template"]["prescriptiontemplateid"], rx["start"], rx["end"]))
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

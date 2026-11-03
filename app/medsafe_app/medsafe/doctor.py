"""Doctor: the Prescribe screen.

Flow (one page, filled in step by step through the URL):
  1. choose patient + condition        /prescribe?nric=...&disease_id=...
  2. see drugs for that condition, each safety-checked against the patient
  3. pick a drug (or search any drug)  ...&drug_id=...
  4. choose a dose template or write one, give a reason if there are alerts, send.
Sending writes PostgreSQL first (Diagnosis if new, PrescriptionTemplate if custom,
Prescription as Pending), then the audit document in MongoDB.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

import psycopg
from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import outbox, records, safety
from .auth import current_user, role_required
from .db import pg, query, query_one
from .util import dose_text, now, today

bp = Blueprint("doctor", __name__)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def render_prescribe(args, form=None, errors=None):
    user = current_user()
    nric = (args.get("nric") or "").strip()
    disease_id = _int(args.get("disease_id"))
    drug_id = _int(args.get("drug_id"))
    ctx = {"form": form or {}, "errors": errors or [], "nric": nric, "disease_id": disease_id, "drug_id": drug_id}

    ctx["recent"] = query("""
        SELECT * FROM v_prescription_detail WHERE DoctorID = %s ORDER BY PrescriptionID DESC LIMIT 8""", (user["id"],))
    ctx["patient"] = records.patient(nric) if nric else None
    ctx["disease"] = query_one("SELECT DiseaseID, Name FROM Disease WHERE DiseaseID = %s", (disease_id,)) if disease_id else None
    if nric and not ctx["patient"]:
        ctx["errors"].append("No patient with that NRIC.")
    if not (ctx["patient"] and ctx["disease"]):
        return render_template("prescribe.html", **ctx)

    p, dis = ctx["patient"], ctx["disease"]
    ctx["allergies"] = records.allergies(nric)
    ctx["active"] = records.active_meds(nric)
    ctx["past_count"] = len(records.other_prescriptions(nric))
    dxs = records.diagnoses(nric)
    hist = records.history(nric)
    ctx["dx_names"], ctx["history_lines"] = records.summary_lines(nric, dxs, hist)
    ctx["existing_dx"] = next((g for g in dxs if g["diseaseid"] == disease_id), None)
    ctx["last_hist"] = max(hist, key=lambda e: e["recordedAt"]) if hist else None

    # Drugs linked to this condition (ToAddress), plus a drug picked by search.
    cands = query("""
        SELECT d.DrugID, d.GenericName FROM ToAddress ta JOIN Drug d ON d.DrugID = ta.DrugID
        WHERE ta.DiseaseID = %s ORDER BY d.GenericName""", (disease_id,))
    cand_ids = [c["drugid"] for c in cands]
    if drug_id and drug_id not in cand_ids:
        extra = query_one("SELECT DrugID, GenericName FROM Drug WHERE DrugID = %s", (drug_id,))
        if extra:
            extra["searched"] = True
            cands.append(extra)
            cand_ids.append(drug_id)
        else:
            drug_id = ctx["drug_id"] = None
    templates = {}
    for t in query("""SELECT * FROM PrescriptionTemplate WHERE DrugID = ANY(%s)
                      ORDER BY DrugID, PrescriptionTemplateID""", (cand_ids,)):
        templates.setdefault(t["drugid"], []).append(t)
    checks = safety.check(nric, cand_ids)
    for c in cands:
        c["templates"] = templates.get(c["drugid"], [])
        c["check"] = checks[c["drugid"]]
    # fewest alerts first, keeping the searched drug visible at the end
    cands.sort(key=lambda c: (c.get("searched", False), len(c["check"]["alerts"]), c["genericname"]))
    ctx["candidates"] = cands

    if drug_id:
        ctx["selected"] = next(c for c in cands if c["drugid"] == drug_id)
    return render_template("prescribe.html", **ctx)


@bp.route("/prescribe")
@role_required("doctor")
def prescribe():
    return render_prescribe(request.args)


@bp.route("/prescribe", methods=["POST"])
@role_required("doctor")
def prescribe_submit():
    f = request.form
    user = current_user()
    nric, disease_id, drug_id = f.get("nric", ""), _int(f.get("disease_id")), _int(f.get("drug_id"))
    errors = []

    patient = records.patient(nric)
    disease = query_one("SELECT DiseaseID, Name FROM Disease WHERE DiseaseID = %s", (disease_id,))
    drug = query_one("SELECT DrugID, GenericName FROM Drug WHERE DrugID = %s", (drug_id,))
    if not (patient and disease and drug):
        flash("Patient, condition or drug not found. Start again.", "error")
        return redirect(url_for("doctor.prescribe"))

    # ---- dose: an existing template of this drug, or a custom one ------------------
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
            errors.append("Choose a dose template, or write your own.")

    # ---- dates ----------------------------------------------------------------------
    try:
        start = date.fromisoformat(f.get("start", ""))
    except ValueError:
        start = None
        errors.append("Enter a start date.")
    days = _int(f.get("days")) if f.get("days", "").strip() else None
    if f.get("days", "").strip() and (days is None or not 1 <= days <= 365):
        errors.append("Duration must be between 1 and 365 days, or left blank for ongoing.")
    if start and start < today():
        errors.append("The start date can't be in the past.")

    # ---- safety check again on the server (never trust the page) --------------------
    alerts = safety.check(nric, [drug_id])[drug_id]["alerts"]
    reason = f.get("reason", "").strip()
    if alerts and not reason:
        errors.append(f"There {'is 1 alert' if len(alerts) == 1 else f'are {len(alerts)} alerts'} for this drug. "
                      "Give a reason to proceed, or choose another drug.")

    if errors:
        return render_prescribe({"nric": nric, "disease_id": disease_id, "drug_id": drug_id}, form=f, errors=errors)

    end = safety.end_date(start, days)

    # ---- 1. PostgreSQL (one transaction) --------------------------------------------
    conn = pg()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT DiagnosisID FROM Diagnosis WHERE NRIC = %s AND DiseaseID = %s
                           ORDER BY DateOfDiagnosis DESC, DiagnosisID DESC LIMIT 1""", (nric, disease_id))
            row = cur.fetchone()
            new_dx = row is None
            if new_dx:
                cur.execute("""INSERT INTO Diagnosis (NRIC, DoctorID, DiseaseID, DateOfDiagnosis)
                               VALUES (%s, %s, %s, CURRENT_DATE) RETURNING DiagnosisID""", (nric, user["id"], disease_id))
                row = cur.fetchone()
            dx_id = row["diagnosisid"]
            if custom:
                cur.execute("""INSERT INTO PrescriptionTemplate (DrugID, dose_amount, dose_unit, frequency, route, dosage_form)
                               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                            (drug_id, custom["dose_amount"], custom["dose_unit"], custom["frequency"],
                             custom["route"], custom["dosage_form"]))
                template = cur.fetchone()
            cur.execute("""INSERT INTO Prescription (DiagnosisID, PrescriptionTemplateID, PrescriptionStart, PrescriptionEnd, ReviewStatus)
                           VALUES (%s, %s, %s, %s, 'Pending') RETURNING PrescriptionID""",
                        (dx_id, template["prescriptiontemplateid"], start, end))
            rx_id = cur.fetchone()["prescriptionid"]
        conn.commit()
    except psycopg.Error as e:
        conn.rollback()
        flash(f"The prescription couldn't be saved: {e.diag.message_primary or e}", "error")
        return redirect(url_for("doctor.prescribe", nric=nric, disease_id=disease_id, drug_id=drug_id))

    # ---- 2. MongoDB audit trail (retried later if MongoDB is down) ------------------
    ingredients = [r["ingredientname"] for r in query("""
        SELECT ai.IngredientName FROM Contains c JOIN ActiveIngredients ai ON ai.IngredientID = c.IngredientID
        WHERE c.DrugID = %s ORDER BY 1""", (drug_id,))]
    event = {"at": now(), "action": "prescribed", "by": {"role": "doctor", "id": user["id"], "name": user["name"]}}
    if alerts:
        event["alertsAcknowledged"] = len(alerts)
        event["reason"] = reason[:1000]
    audit = {
        "_id": rx_id, "nric": nric, "diagnosisId": dx_id,
        "drug": {"drugId": drug_id, "name": drug["genericname"], "ingredients": ingredients,
                 "dose": f"{dose_text(template)}, {(template.get('route') or '').lower()}".strip(", ")},
        "prescribedBy": {"doctorId": user["id"], "name": user["name"]},
        "status": "Pending", "alerts": safety.mongo_safe_alerts(alerts), "events": [event],
    }
    enc = records.todays_encounter(nric)
    if enc:
        audit["encounterId"] = enc["_id"]
    ok = outbox.run("prescription_audit", "insert_one", audit)
    if enc:
        outbox.run("encounters", "update_one", {"_id": enc["_id"]}, {"$addToSet": {"prescriptionIds": rx_id}})

    msg = f"Prescription {rx_id} for {drug['genericname']} sent to the pharmacy."
    if new_dx:
        msg += f" New diagnosis recorded: {disease['name']}."
    if not ok:
        msg += " (The audit record will be saved when MongoDB is back.)"
    flash(msg, "success")
    return redirect(url_for("patients.record", nric=nric))

"""Consult: the doctor's main screen for a visit (consultation steps 3 and 4 start here).

/consult            pick a patient; your recent visits
/consult/<nric>     one screen: what you need to know about the patient on the left
                    (allergies, active medicines, diagnoses, labs, last visit), the visit
                    form on the right (triage, history, examination, assessment and plan)

Saving writes the visit to MongoDB `encounters` (shape in exam.py). A new diagnosis
picked on the form is inserted into PostgreSQL in the same request: the PostgreSQL
insert is committed only after MongoDB accepts the visit, so a refused visit leaves
no stray diagnosis behind. "Save and prescribe" then opens Prescribe for the patient
and condition; Prescribe links the prescription to today's visit.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from pymongo.errors import PyMongoError, WriteError
from werkzeug.datastructures import MultiDict

from . import exam, records
from .auth import current_user, role_required
from .db import mongo, pg, query, query_one
from .patients import _insert_with_id, _mongo_error
from .util import now

bp = Blueprint("consult", __name__)


@bp.route("/consult")
@role_required("doctor")
def start():
    user = current_user()
    try:
        recent = list(mongo().encounters.find({"doctor.doctorId": user["id"]},
                                              {"nric": 1, "visitDate": 1, "triage.chiefComplaint": 1, "visitType": 1})
                      .sort("visitDate", -1).limit(10))
    except PyMongoError:
        recent = []
    names = {}
    if recent:
        names = {r["nric"]: r["name"] for r in query("SELECT NRIC, Name FROM Patient WHERE NRIC = ANY(%s)",
                                                     ([v["nric"] for v in recent],))}
    for v in recent:
        v["name"] = names.get(v["nric"], v["nric"])
    return render_template("consult_start.html", recent=recent)


@bp.route("/consult/<nric>")
@role_required("doctor")
def visit(nric):
    return _render(nric)


def _render(nric, vform=None, errors=None, status=200):
    p = records.patient(nric) or abort(404)
    try:
        visits, labs, mongo_ok = records.encounters(nric, limit=3), records.labs(nric), True
    except PyMongoError:
        visits, labs, mongo_ok = [], [], False
    return render_template(
        "consult.html", p=p, allergies=records.allergies(nric), active=records.active_meds(nric),
        dxs=records.diagnoses(nric), visits=visits, labs=labs, mongo_ok=mongo_ok,
        vf=vform if vform is not None else MultiDict(), visit_errors=errors or []), status


@bp.route("/consult/<nric>", methods=["POST"])
@role_required("doctor")
def save(nric):
    """One visit, in the order of a consultation: triage, history (what the patient says),
    examination (what the doctor finds), then assessment and plan."""
    records.patient(nric) or abort(404)
    f = request.form
    user = current_user()
    errors = []

    def num(name, cast=int):
        v = f.get(name, "").strip()
        return cast(v) if v else None

    # ---- triage ----------------------------------------------------------------------
    complaint = " ".join(f.get("complaint", "").split())[:200]
    if not complaint:
        errors.append("Enter the chief complaint.")
    vitals = {}
    try:
        for key, field, cast in (("systolicBP", "sbp", int), ("diastolicBP", "dbp", int), ("heartRate", "hr", int),
                                 ("respRate", "rr", int), ("temperatureC", "temp", float), ("spo2", "spo2", int),
                                 ("weightKg", "weight", float)):
            v = num(field, cast)
            if v is not None:
                vitals[key] = v
    except ValueError:
        errors.append("Vitals must be numbers (whole numbers except temperature and weight).")
    else:
        missing = [n for n, k in (("systolic BP", "systolicBP"), ("diastolic BP", "diastolicBP"),
                                  ("heart rate", "heartRate"), ("temperature", "temperatureC"), ("SpO2", "spo2"))
                   if k not in vitals]
        if missing:
            errors.append("Enter " + ", ".join(missing) + ".")
    try:
        pain_score = max(0, min(10, num("pain") or 0))
    except ValueError:
        pain_score = 0
        errors.append("Pain score must be a whole number from 0 to 10.")

    # ---- history and examination -----------------------------------------------------
    history, exam_list, exam_errors = exam.parse(f)
    errors += exam_errors

    # ---- assessment: existing diagnoses ticked, and/or one new condition ---------------
    existing = {g["diagnosisid"]: g for g in records.diagnoses(nric)}
    dx_ids = [int(x) for x in f.getlist("diagnosis_ids") if x.isdigit() and int(x) in existing]
    new_disease = None
    if f.get("new_disease_id", "").isdigit():
        new_disease = query_one("SELECT DiseaseID, Name FROM Disease WHERE DiseaseID = %s", (int(f["new_disease_id"]),))
    elif f.get("new_disease_q", "").strip():
        errors.append("Choose the new diagnosis from the list, or clear the box.")
    if new_disease and any(g["diseaseid"] == new_disease["diseaseid"] for g in existing.values()):
        # already diagnosed: tick the existing one instead of adding a duplicate
        same = next(g for g in existing.values() if g["diseaseid"] == new_disease["diseaseid"])
        if same["diagnosisid"] not in dx_ids:
            dx_ids.append(same["diagnosisid"])
        new_disease = None
    if not dx_ids and not new_disease:
        errors.append("Tick a diagnosis this visit addressed, or add a new one.")

    plan_note = f.get("note", "").strip()[:3000]
    note = exam.compose_note(complaint, history, exam_list, pain_score, plan_note)
    if len(note) < 10:
        errors.append("Record what the patient told you, what you found, or a note of 10 or more characters.")

    if errors:
        return _render(nric, vform=f, errors=errors, status=400)

    # ---- save: PostgreSQL diagnosis (held open) -> MongoDB visit -> commit ---------------
    conn = pg()
    try:
        if new_disease:
            row = conn.execute("""INSERT INTO Diagnosis (NRIC, DoctorID, DiseaseID, DateOfDiagnosis)
                                  VALUES (%s, %s, %s, CURRENT_DATE) RETURNING DiagnosisID""",
                               (nric, user["id"], new_disease["diseaseid"])).fetchone()
            dx_ids.append(row["diagnosisid"])
        consultation = {"note": note[:4000], "diagnosisIds": dx_ids, "formVersion": exam.FORM_VERSION}
        if history:
            consultation["history"] = history
        if exam_list:
            consultation["exam"] = exam_list
        if plan_note:
            consultation["plan"] = plan_note
        if num("follow_up"):
            consultation["followUpInDays"] = num("follow_up")
        doc_row = query_one("SELECT Hospital FROM Doctor WHERE DoctorID = %s", (user["id"],))
        doc = {
            "nric": nric, "visitDate": now(),
            "visitType": f.get("visit_type") if f.get("visit_type") in ("new problem", "follow-up") else "follow-up",
            "doctor": {"doctorId": user["id"], "name": user["name"], "hospital": (doc_row or {}).get("hospital") or ""},
            "triage": {"recordedAt": now(), "category": f.get("category") if f.get("category") in ("P1", "P2", "P3", "P4") else "P4",
                       "chiefComplaint": complaint, "painScore": pain_score, "vitals": vitals},
            "consultation": consultation,
            "prescriptionIds": [],
        }
        try:
            eid = _insert_with_id("encounters", "E", doc)
        except WriteError as e:
            conn.rollback()
            return _render(nric, vform=f, errors=[f"MongoDB refused the visit: {_mongo_error(e)}"], status=400)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    flash(f"Visit {eid} saved." + (f" Diagnosis added: {new_disease['name']}." if new_disease else ""), "success")
    if f.get("next") == "prescribe":
        # prescribe for the condition this visit is mainly about (first ticked, or the new one)
        first = dx_ids[-1] if new_disease else dx_ids[0]
        disease_id = new_disease["diseaseid"] if new_disease else existing[first]["diseaseid"]
        return redirect(url_for("doctor.prescribe", nric=nric, disease_id=disease_id))
    return redirect(url_for("patients.record", nric=nric) + "#visits")

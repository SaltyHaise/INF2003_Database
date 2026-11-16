"""Consult: the doctor's one screen for a visit, from the patient's story to the prescription.

/consult            pick a patient; your recent visits and prescriptions
/consult/<nric>     context on the left (allergies, medicines, conditions and history, labs,
                    recent visits); on the right, the visit in the order it happens:
                      1. reason for visit (required)
                      2. examination: vitals, symptoms and findings (optional, folded)
                      3. diagnosis (required): tick existing, or add new
                      4. medicines for that diagnosis, each safety-checked; pick one to
                         set the dose, or prescribe nothing today
                      5. advice given to the patient (optional)

The page refreshes itself (POST with action=refresh) when the diagnosis or medicine
changes, so the medicine list stays in step without losing what was typed.

Saving (action=save) is one unit of work across both databases:
  PostgreSQL  new Diagnosis, PrescriptionTemplate (custom dose), Prescription  - held open
  MongoDB     encounters: the visit, linked to the prescription
  PostgreSQL  COMMIT only after MongoDB accepted the visit (if the commit then fails,
              the visit document is removed again)
  MongoDB     prescription_audit through the outbox (retried later if MongoDB is down)
"""
import psycopg
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from pymongo.errors import PyMongoError, WriteError
from werkzeug.datastructures import MultiDict

from . import doctor, exam, records
from .auth import current_user, role_required
from .db import mongo, pg, query, query_one
from .patients import _insert_with_id, _mongo_error
from .util import dose_text, now

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
    rx_recent = query("""SELECT * FROM v_prescription_detail WHERE DoctorID = %s
                         ORDER BY PrescriptionID DESC LIMIT 8""", (user["id"],))
    return render_template("consult_start.html", recent=recent, rx_recent=rx_recent)


@bp.route("/consult/<nric>")
@role_required("doctor")
def visit(nric):
    return _render(nric)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _diagnoses(f, nric):
    """Diagnoses this visit addressed: ticked existing ones plus at most one new condition.
    A 'new' condition the patient already has is treated as ticking the existing one."""
    errors = []
    existing = {g["diagnosisid"]: g for g in records.diagnoses(nric)}
    dx_ids = [int(x) for x in f.getlist("diagnosis_ids") if x.isdigit() and int(x) in existing]
    new_disease = None
    if f.get("new_disease_id", "").isdigit():
        new_disease = query_one("SELECT DiseaseID, Name FROM Disease WHERE DiseaseID = %s", (int(f["new_disease_id"]),))
    elif f.get("new_disease_q", "").strip():
        errors.append("Choose the new diagnosis from the list, or clear the box.")
    if new_disease:
        same = next((g for g in existing.values() if g["diseaseid"] == new_disease["diseaseid"]), None)
        if same:
            if same["diagnosisid"] not in dx_ids:
                dx_ids.append(same["diagnosisid"])
            new_disease = None
    # conditions the medicines can be for: the new one first, then the ticked ones
    options = ([{"diseaseid": new_disease["diseaseid"], "name": new_disease["name"], "new": True}] if new_disease else []) + \
              [{"diseaseid": existing[d]["diseaseid"], "name": existing[d]["disease"]} for d in dx_ids]
    treat = _int(f.get("treat"))
    if treat not in [o["diseaseid"] for o in options]:
        treat = options[0]["diseaseid"] if options else None
    return existing, dx_ids, new_disease, options, treat, errors


def _drug_choice(f, treat):
    """The medicine picked: a search pick wins; a pick made for another condition is dropped."""
    if not treat:
        return None
    if _int(f.get("drug_search_id")):
        return _int(f["drug_search_id"])
    if _int(f.get("treat_shown")) not in (None, treat):
        return None
    return _int(f.get("drug_id"))


def _render(nric, f=None, errors=None, status=200):
    p = records.patient(nric) or abort(404)
    f = f if f is not None else MultiDict()
    try:
        visits, labs, hist, mongo_ok = records.encounters(nric, limit=3), records.labs(nric), records.history(nric), True
    except PyMongoError:
        visits, labs, hist, mongo_ok = [], [], [], False
    existing, dx_ids, new_disease, options, treat, _ = _diagnoses(f, nric)
    drug_id = _drug_choice(f, treat)
    candidates, selected = doctor.drug_options(nric, treat, drug_id) if treat else ([], None)
    dxs = list(existing.values())
    dx_names, history_lines = records.summary_lines(nric, dxs, hist)
    treat_dx = next((g for g in dxs if g["diseaseid"] == treat), None)
    return render_template(
        "consult.html", p=p, allergies=records.allergies(nric), active=records.active_meds(nric),
        dxs=dxs, dx_names=dx_names, history_lines=history_lines, visits=visits, labs=labs, mongo_ok=mongo_ok,
        vf=f, visit_errors=errors or [], dx_picked=[str(d) for d in dx_ids], treat_options=options, treat=treat,
        treat_name=next((o["name"] for o in options if o["diseaseid"] == treat), None), treat_dx=treat_dx,
        candidates=candidates, selected=selected), status


@bp.route("/consult/<nric>", methods=["POST"])
@role_required("doctor")
def save(nric):
    """action=refresh re-draws the page with what was typed; action=save saves the visit
    and, if a medicine was chosen, the prescription with it."""
    records.patient(nric) or abort(404)
    f = request.form
    if f.get("action") != "save":
        return _render(nric, f)
    user = current_user()
    errors = []

    def num(name, cast=int):
        v = f.get(name, "").strip()
        return cast(v) if v else None

    # ---- 1. reason for visit (required) ----------------------------------------------
    complaint = " ".join(f.get("complaint", "").split())[:200]
    if len(complaint) < 3:
        errors.append("Enter the reason for the visit.")

    # ---- 2. examination (optional) ---------------------------------------------------
    vitals = {}
    try:
        for key, field, cast in (("systolicBP", "sbp", int), ("diastolicBP", "dbp", int), ("heartRate", "hr", int),
                                 ("temperatureC", "temp", float), ("spo2", "spo2", int), ("weightKg", "weight", float)):
            v = num(field, cast)
            if v is not None:
                vitals[key] = v
    except ValueError:
        errors.append("Vitals must be numbers (whole numbers except temperature and weight).")
    else:
        if ("systolicBP" in vitals) != ("diastolicBP" in vitals):
            errors.append("Enter both BP numbers, or leave both blank.")
    try:
        pain_score = num("pain")
        if pain_score is not None and not 0 <= pain_score <= 10:
            errors.append("Pain score must be from 0 to 10.")
    except ValueError:
        pain_score = None
        errors.append("Pain score must be a whole number from 0 to 10.")
    history, exam_list, exam_errors = exam.parse(f)
    errors += exam_errors
    findings = f.get("note", "").strip()[:3000]

    # ---- 3. diagnosis (required) -----------------------------------------------------
    existing, dx_ids, new_disease, options, treat, dx_errors = _diagnoses(f, nric)
    errors += dx_errors
    if not dx_ids and not new_disease:
        errors.append("Tick a diagnosis this visit addressed, or add a new one.")

    # ---- 4. medicine (optional) ------------------------------------------------------
    rx = None
    drug_id = _drug_choice(f, treat)
    if drug_id:
        rx, rx_errors = doctor.parse_prescription(f, nric, drug_id)
        errors += rx_errors

    # ---- 5. advice (optional) --------------------------------------------------------
    advice = f.get("advice", "").replace("\r\n", "\n").strip()[:2000]

    if errors:
        return _render(nric, f, errors, 400)

    # ---- save ------------------------------------------------------------------------
    conn = pg()
    rx_id = rx_dx = None
    try:
        with conn.cursor() as cur:
            if new_disease:
                cur.execute("""INSERT INTO Diagnosis (NRIC, DoctorID, DiseaseID, DateOfDiagnosis)
                               VALUES (%s, %s, %s, CURRENT_DATE) RETURNING DiagnosisID""",
                            (nric, user["id"], new_disease["diseaseid"]))
                dx_ids.append(cur.fetchone()["diagnosisid"])
            if rx:
                rx_id, rx_dx, _ = doctor.insert_prescription(cur, nric, user["id"], treat, rx)
    except psycopg.Error as e:
        conn.rollback()
        return _render(nric, f, [f"PostgreSQL refused the visit: {e.diag.message_primary or e}"], 400)

    dx_names = [existing[d]["disease"] for d in dx_ids if d in existing] + ([new_disease["name"]] if new_disease else [])
    note = exam.compose_note(complaint, history, exam_list, pain_score, findings)
    if not (history or exam_list or findings):
        note = f"{complaint}.\nAssessment: {', '.join(dx_names)}."
    if rx:
        t = rx["template"]
        note += f"\nPrescribed: {rx['drug']['genericname'].lower()} {dose_text(t)}".rstrip()
    if advice:
        note += f"\nAdvice: {advice}"
    consultation = {"note": note[:4000], "diagnosisIds": dx_ids, "formVersion": exam.FORM_VERSION}
    if history:
        consultation["history"] = history
    if exam_list:
        consultation["exam"] = exam_list
    if findings:
        consultation["plan"] = findings
    if advice:
        consultation["advice"] = advice
    if num("follow_up"):
        consultation["followUpInDays"] = num("follow_up")
    doc_row = query_one("SELECT Hospital FROM Doctor WHERE DoctorID = %s", (user["id"],))
    doc = {
        "nric": nric, "visitDate": now(),
        "visitType": "new problem" if new_disease else "follow-up",
        "doctor": {"doctorId": user["id"], "name": user["name"], "hospital": (doc_row or {}).get("hospital") or ""},
        "triage": {"recordedAt": now(), "chiefComplaint": complaint},
        "consultation": consultation,
        "prescriptionIds": [rx_id] if rx_id else [],
    }
    if vitals:
        doc["triage"]["vitals"] = vitals
    if pain_score is not None:
        doc["triage"]["painScore"] = pain_score

    try:
        eid = _insert_with_id("encounters", "E", doc)
    except WriteError as e:
        conn.rollback()
        return _render(nric, f, [f"MongoDB refused the visit: {_mongo_error(e)}"], 400)
    except PyMongoError:
        conn.rollback()
        return _render(nric, f, ["MongoDB isn't reachable, so nothing was saved. Try again in a moment."], 503)
    try:
        conn.commit()
    except psycopg.Error:
        mongo().encounters.delete_one({"_id": eid})   # undo the visit so both databases agree
        raise

    msg = f"Visit {eid} saved."
    if new_disease:
        msg += f" Diagnosis added: {new_disease['name']}."
    if rx:
        ok = doctor.write_audit(rx_id, rx_dx, nric, user, rx, eid, advice or None)
        msg += f" Prescription {rx_id} for {rx['drug']['genericname']} sent to the pharmacy."
        if not ok:
            msg += " (The audit record will be saved when MongoDB is back.)"
    flash(msg, "success")
    return redirect(url_for("patients.record", nric=nric) + "#visits")

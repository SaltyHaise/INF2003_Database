"""Consult: the doctor's one screen for a visit, from the patient's story to the prescription.

/consult            pick a patient; your recent visits and prescriptions
/consult/<nric>     context on the left (allergies, medicines, conditions and history, labs,
                    recent visits); on the right, the visit in the order it happens:
                      1. reason for visit (required)
                      2. examination: vitals, symptoms and findings (optional, folded)
                      3. diagnosis (required): one search box; the patient's conditions first
                      4. medicines for that diagnosis, each safety-checked; tick one or
                         more (each gets its own dose card, and the ticked medicines are
                         checked against each other too), or none today
                      5. advice given to the patient (optional)

The page refreshes itself (POST with action=refresh) when the diagnosis or medicine
changes, so the medicine list stays in step without losing what was typed.

Saving (action=save) is one unit of work across both databases:
  PostgreSQL  new Diagnosis, then per medicine a PrescriptionTemplate (custom dose) and a
              Prescription, all in ONE transaction - held open
  MongoDB     encounters: the visit, listing every prescription (prescriptionIds)
  PostgreSQL  COMMIT only after MongoDB accepted the visit (if the commit then fails,
              the visit document is removed again)
  MongoDB     one prescription_audit per prescription, through the outbox (retried later
              if MongoDB is down)
A visit with three medicines is still one visit: either all three prescriptions are saved
with it, or none are.
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
                                              {"nric": 1, "visitDate": 1, "triage.chiefComplaint": 1, "visitType": 1,
                                               "prescriptionIds": 1})
                      .sort("visitDate", -1).limit(8))
        mongo_ok = True
    except PyMongoError:
        recent, mongo_ok = [], False
    records.attach_prescriptions(recent)
    names = {}
    if recent:
        names = {r["nric"]: r["name"] for r in query("SELECT NRIC, Name FROM Patient WHERE NRIC = ANY(%s)",
                                                     ([v["nric"] for v in recent],))}
    for v in recent:
        v["name"] = names.get(v["nric"], v["nric"])
    # prescriptions the pharmacist sent back: these need the doctor's attention
    returned = query("""SELECT * FROM v_prescription_detail WHERE DoctorID = %s AND ReviewStatus = 'Rejected'
                        ORDER BY ReviewedAt DESC NULLS LAST, PrescriptionID DESC LIMIT 5""", (user["id"],))
    waiting = query_one("""SELECT count(*) AS n FROM v_prescription_detail
                           WHERE DoctorID = %s AND ReviewStatus = 'Pending'""", (user["id"],))["n"]
    return render_template("consult_start.html", recent=recent, returned=returned, waiting=waiting, mongo_ok=mongo_ok)


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
    """The visit's diagnosis, picked from the condition list in one search box.

    If the patient already has that condition, the visit is filed under the existing
    Diagnosis row; otherwise a new one is added on save. Returns
    (existing rows by id, diagnosis ids to link, new disease or None, disease id, errors)."""
    errors = []
    existing = {g["diagnosisid"]: g for g in records.diagnoses(nric)}
    disease = None
    if f.get("disease_id", "").isdigit():
        disease = query_one("SELECT DiseaseID, Name FROM Disease WHERE DiseaseID = %s", (int(f["disease_id"]),))
    if not disease:
        if f.get("diagnosis_q", "").strip():
            errors.append("Choose the diagnosis from the list that appears as you type.")
        return existing, [], None, None, errors
    same = max((g for g in existing.values() if g["diseaseid"] == disease["diseaseid"]),
               key=lambda g: (g["dateofdiagnosis"], g["diagnosisid"]), default=None)
    if same:
        return existing, [same["diagnosisid"]], None, disease["diseaseid"], errors
    return existing, [], disease, disease["diseaseid"], errors


def _drug_choices(f, treat):
    """The medicines ticked, in the order picked, plus one just found by search. Picks made
    for a different diagnosis are dropped when the diagnosis changes."""
    if not treat or _int(f.get("treat_shown")) not in (None, treat):
        return []
    ticked = [d for d in (_int(v) for v in f.getlist("drug_ids")) if d]
    # drug_order remembers the order of earlier picks, so cards don't jump around as the
    # table re-sorts; newly ticked medicines go last
    ids = [d for d in (_int(v) for v in f.getlist("drug_order")) if d in ticked] + ticked
    if _int(f.get("drug_search_id")):
        ids.append(_int(f["drug_search_id"]))
    return list(dict.fromkeys(ids))[: doctor.MAX_PER_VISIT]


def _render(nric, f=None, errors=None, status=200):
    p = records.patient(nric) or abort(404)
    f = f if f is not None else MultiDict()
    try:
        visits, labs, hist, mongo_ok = records.encounters(nric, limit=3), records.labs(nric), records.history(nric), True
    except PyMongoError:
        visits, labs, hist, mongo_ok = [], [], [], False
    existing, dx_ids, new_disease, treat, _ = _diagnoses(f, nric)
    candidates, selected = doctor.drug_options(nric, treat, _drug_choices(f, treat)) if treat else ([], [])
    dxs = list(existing.values())
    dx_names, history_lines = records.summary_lines(nric, dxs, hist)
    treat_dx = existing[dx_ids[0]] if dx_ids else None
    return render_template(
        "consult.html", p=p, allergies=records.allergies(nric), active=records.active_meds(nric),
        dxs=dxs, dx_names=dx_names, history_lines=history_lines, visits=visits, labs=labs, mongo_ok=mongo_ok,
        vf=f, visit_errors=errors or [], treat=treat,
        treat_name=(new_disease["name"] if new_disease else treat_dx["disease"] if treat_dx else None), treat_dx=treat_dx,
        diagnosis=({"existing": existing[dx_ids[0]] if dx_ids else None} if treat else None),
        candidates=candidates, selected=selected, max_meds=doctor.MAX_PER_VISIT), status


@bp.route("/consult/<nric>", methods=["POST"])
@role_required("doctor")
def save(nric):
    """action=refresh re-draws the page with what was typed; action=save saves the visit
    and the prescriptions for the medicines ticked (none, one or several)."""
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
    existing, dx_ids, new_disease, treat, dx_errors = _diagnoses(f, nric)
    errors += dx_errors
    if not treat and not dx_errors:
        errors.append("Choose the diagnosis.")

    # ---- 4. medicines (optional, one dose card each) ---------------------------------
    rxs = []
    if treat:
        if len(f.getlist("drug_ids")) > doctor.MAX_PER_VISIT:
            errors.append(f"Prescribe at most {doctor.MAX_PER_VISIT} medicines in one visit.")
        # the safety check runs again here, including between the medicines ticked
        _, picked = doctor.drug_options(nric, treat, _drug_choices(f, treat))
        for c in picked:
            rx, rx_errors = doctor.parse_prescription(f, c, c["check"]["alerts"])
            errors += rx_errors
            if rx:
                rxs.append(rx)

    # ---- 5. advice (optional) --------------------------------------------------------
    advice = f.get("advice", "").replace("\r\n", "\n").strip()[:2000]

    if errors:
        return _render(nric, f, errors, 400)

    # ---- save ------------------------------------------------------------------------
    conn = pg()
    saved = []   # (prescription id, diagnosis id, rx)
    try:
        with conn.cursor() as cur:
            if new_disease:
                cur.execute("""INSERT INTO Diagnosis (NRIC, DoctorID, DiseaseID, DateOfDiagnosis)
                               VALUES (%s, %s, %s, CURRENT_DATE) RETURNING DiagnosisID""",
                            (nric, user["id"], new_disease["diseaseid"]))
                dx_ids.append(cur.fetchone()["diagnosisid"])
            for rx in rxs:
                rx_id, rx_dx, _ = doctor.insert_prescription(cur, nric, user["id"], treat, rx)
                saved.append((rx_id, rx_dx, rx))
    except psycopg.Error as e:
        conn.rollback()
        return _render(nric, f, [f"PostgreSQL refused the visit: {e.diag.message_primary or e}"], 400)

    dx_names = [existing[d]["disease"] for d in dx_ids if d in existing] + ([new_disease["name"]] if new_disease else [])
    note = exam.compose_note(complaint, history, exam_list, pain_score, findings)
    if not (history or exam_list or findings):
        note = f"{complaint}.\nAssessment: {', '.join(dx_names)}."
    if saved:
        note += "\nPrescribed: " + "; ".join(
            f"{rx['drug']['genericname'].lower()} {dose_text(rx['template'])}".rstrip() for _, _, rx in saved)
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
        "prescriptionIds": [rx_id for rx_id, _, _ in saved],
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
    if saved:
        # alerts between medicines of this visit now point at the sibling prescription
        rx_of = {rx["drug"]["drugid"]: rx_id for rx_id, _, rx in saved}
        queued = False
        for rx_id, rx_dx, rx in saved:
            for a in rx["alerts"]:
                if a.get("withDrugId") in rx_of:
                    a["withPrescriptionId"] = rx_of[a["withDrugId"]]
            queued |= not doctor.write_audit(rx_id, rx_dx, nric, user, rx, eid, advice or None)
        names = ", ".join(f"{rx['drug']['genericname'].lower()} (#{rx_id})" for rx_id, _, rx in saved)
        msg += f" {len(saved)} prescription{'s' if len(saved) > 1 else ''} sent to the pharmacy: {names}."
        if queued:
            msg += " (Audit records will be saved when MongoDB is back.)"
    flash(msg, "success")
    return redirect(url_for("patients.record", nric=nric) + "#visits")

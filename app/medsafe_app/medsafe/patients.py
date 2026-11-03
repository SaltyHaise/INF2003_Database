"""Patient records: search, view, and the create/update actions on a record.

PostgreSQL: patient details, allergies, diagnoses, prescriptions.
MongoDB:    history events (patient_history), visits with triage + notes (encounters), labs.
Pharmacists can view records (read-only); only doctors can change them.
"""
import re
from datetime import date, datetime, time

import psycopg
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from pymongo.errors import DuplicateKeyError, PyMongoError, WriteError

from . import records
from .auth import current_user, role_required
from .db import mongo, pg, query, query_one
from .util import now, today

bp = Blueprint("patients", __name__)

NRIC_RE = re.compile(r"^[STFGM][0-9]{7}[A-Z]$")


def _next_id(collection, prefix):
    """Next readable id like H0203 / E0250 (max existing number + 1)."""
    res = list(mongo()[collection].aggregate([
        {"$match": {"_id": {"$regex": f"^{prefix}[0-9]+$"}}},
        {"$group": {"_id": None, "n": {"$max": {"$toInt": {"$substrCP": ["$_id", 1, 20]}}}}}]))
    return f"{prefix}{(res[0]['n'] if res else 0) + 1:04d}"


def _insert_with_id(collection, prefix, doc):
    for _ in range(5):
        doc["_id"] = _next_id(collection, prefix)
        try:
            mongo()[collection].insert_one(doc)
            return doc["_id"]
        except DuplicateKeyError as e:
            if "_id_" not in str(e):  # a different unique index refused it
                raise
    raise RuntimeError("Could not allocate an id")


def _date(v):
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _dt(d):
    return datetime.combine(d, time(0), tzinfo=now().tzinfo) if d else None


def _mongo_error(e):
    details = getattr(e, "details", None) or {}
    return details.get("errmsg") or str(e)


# --------------------------------------------------------------------------------------
# Search and create
# --------------------------------------------------------------------------------------
@bp.route("/patients")
@role_required("doctor", "pharmacist")
def search():
    q = (request.args.get("q") or "").strip()
    rows = []
    if q:
        rows = query("""
            SELECT p.NRIC, p.Name, p.DOB, p.Sex,
                   (SELECT count(*) FROM v_active_medications a WHERE a.NRIC = p.NRIC) AS active_meds,
                   (SELECT count(*) FROM IsAllergicTo al WHERE al.NRIC = p.NRIC) AS allergies
            FROM Patient p
            WHERE p.Name ILIKE %(like)s OR p.NRIC ILIKE %(prefix)s
            ORDER BY similarity(p.Name, %(q)s) DESC, p.Name LIMIT 30""",
                     {"q": q, "like": f"%{q}%", "prefix": f"{q}%"})
        if len(rows) == 1:
            return redirect(url_for("patients.record", nric=rows[0]["nric"]))
    else:
        rows = query("""
            SELECT p.NRIC, p.Name, p.DOB, p.Sex,
                   (SELECT count(*) FROM v_active_medications a WHERE a.NRIC = p.NRIC) AS active_meds,
                   (SELECT count(*) FROM IsAllergicTo al WHERE al.NRIC = p.NRIC) AS allergies
            FROM Patient p ORDER BY p.Name""")
    return render_template("patients.html", rows=rows, q=q)


@bp.route("/patients/new", methods=["GET", "POST"])
@role_required("doctor")
def new():
    f = request.form
    if request.method == "POST":
        nric = f.get("nric", "").strip().upper()
        name = f.get("name", "").strip()
        dob = _date(f.get("dob"))
        sex = f.get("sex") if f.get("sex") in ("M", "F") else None
        phone = re.sub(r"\s", "", f.get("phone", ""))[:20] or None
        errors = []
        if not NRIC_RE.match(nric):
            errors.append("NRIC must look like S1234567A.")
        if not name:
            errors.append("Enter the patient's name.")
        if not dob or dob > today():
            errors.append("Enter a valid date of birth.")
        if not errors:
            try:
                pg().execute("INSERT INTO Patient (NRIC, Name, DOB, Sex, PhoneNumber) VALUES (%s, %s, %s, %s, %s)",
                             (nric, name[:100], dob, sex, phone))
                pg().commit()
                flash(f"Patient {name} added.", "success")
                return redirect(url_for("patients.record", nric=nric))
            except psycopg.errors.UniqueViolation:
                pg().rollback()
                errors.append("A patient with that NRIC already exists.")
        for e in errors:
            flash(e, "error")
    return render_template("patient_form.html", p=f, mode="new")


@bp.route("/patients/<nric>/edit", methods=["GET", "POST"])
@role_required("doctor")
def edit(nric):
    p = records.patient(nric) or abort(404)
    if request.method == "POST":
        f = request.form
        name, dob = f.get("name", "").strip(), _date(f.get("dob"))
        if not name or not dob or dob > today():
            flash("Enter a name and a valid date of birth.", "error")
            return render_template("patient_form.html", p=f.to_dict() | {"nric": nric}, mode="edit")
        pg().execute("UPDATE Patient SET Name = %s, DOB = %s, Sex = %s, PhoneNumber = %s WHERE NRIC = %s",
                     (name[:100], dob, f.get("sex") if f.get("sex") in ("M", "F") else None,
                      re.sub(r"\s", "", f.get("phone", ""))[:20] or None, nric))
        pg().commit()
        flash("Patient details updated.", "success")
        return redirect(url_for("patients.record", nric=nric))
    return render_template("patient_form.html", p={"nric": p["nric"], "name": p["name"], "dob": p["dob"].isoformat(),
                                                   "sex": p["sex"], "phone": p["phonenumber"] or ""}, mode="edit")


# --------------------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------------------
@bp.route("/patients/<nric>")
@role_required("doctor", "pharmacist")
def record(nric):
    p = records.patient(nric) or abort(404)
    notes_q = (request.args.get("notes") or "").strip()[:100]
    try:
        hist = records.history(nric)
        visits = records.encounters(nric, notes_q or None)
        labs = records.labs(nric)
        mongo_ok = True
    except PyMongoError:
        hist, visits, labs, mongo_ok = [], [], [], False
    return render_template(
        "patient.html", p=p, allergies=records.allergies(nric), active=records.active_meds(nric),
        others=records.other_prescriptions(nric), dxs=records.diagnoses(nric), hist=hist, visits=visits,
        labs=labs, notes_q=notes_q, mongo_ok=mongo_ok,
        diseases=query("SELECT DiseaseID, Name FROM Disease ORDER BY Name"))


# ---- allergies (PostgreSQL) ----------------------------------------------------------
@bp.route("/patients/<nric>/allergies", methods=["POST"])
@role_required("doctor")
def add_allergy(nric):
    f = request.form
    ing = query_one("SELECT IngredientID, IngredientName FROM ActiveIngredients WHERE IngredientID = %s",
                    (f.get("ingredient_id", type=int),))
    sev = f.get("severity") if f.get("severity") in ("Mild", "Moderate", "Severe") else None
    if not ing or not sev:
        flash("Choose an ingredient from the list and a severity.", "error")
    else:
        pg().execute("""
            INSERT INTO IsAllergicTo (NRIC, IngredientID, Date, Severity, Reactions) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (NRIC, IngredientID) DO UPDATE SET Date = EXCLUDED.Date, Severity = EXCLUDED.Severity,
                                                         Reactions = EXCLUDED.Reactions""",
                     (nric, ing["ingredientid"], _date(f.get("date")) or today(), sev,
                      f.get("reaction", "").strip()[:255] or None))
        pg().commit()
        flash(f"Allergy to {ing['ingredientname']} saved.", "success")
    return redirect(url_for("patients.record", nric=nric) + "#allergies")


@bp.route("/patients/<nric>/allergies/<int:ingredient_id>/delete", methods=["POST"])
@role_required("doctor")
def delete_allergy(nric, ingredient_id):
    pg().execute("DELETE FROM IsAllergicTo WHERE NRIC = %s AND IngredientID = %s", (nric, ingredient_id))
    pg().commit()
    flash("Allergy removed.", "success")
    return redirect(url_for("patients.record", nric=nric) + "#allergies")


# ---- diagnoses (PostgreSQL) ----------------------------------------------------------
@bp.route("/patients/<nric>/diagnoses", methods=["POST"])
@role_required("doctor")
def add_diagnosis(nric):
    disease = query_one("SELECT DiseaseID, Name FROM Disease WHERE DiseaseID = %s",
                        (request.form.get("disease_id", type=int),))
    if not disease:
        flash("Choose a condition from the list.", "error")
    else:
        pg().execute("INSERT INTO Diagnosis (NRIC, DoctorID, DiseaseID, DateOfDiagnosis) VALUES (%s, %s, %s, %s)",
                     (nric, current_user()["id"], disease["diseaseid"], _date(request.form.get("date")) or today()))
        pg().commit()
        flash(f"Diagnosis added: {disease['name']}.", "success")
    return redirect(url_for("patients.record", nric=nric) + "#diagnoses")


@bp.route("/patients/<nric>/diagnoses/<int:dx_id>/delete", methods=["POST"])
@role_required("doctor")
def delete_diagnosis(nric, dx_id):
    try:
        pg().execute("DELETE FROM Diagnosis WHERE DiagnosisID = %s AND NRIC = %s", (dx_id, nric))
        pg().commit()
        flash("Diagnosis removed.", "success")
    except psycopg.errors.ForeignKeyViolation:
        pg().rollback()
        flash("This diagnosis has prescriptions, so it can't be removed.", "error")
    return redirect(url_for("patients.record", nric=nric) + "#diagnoses")


# ---- history events (MongoDB patient_history) ----------------------------------------
@bp.route("/patients/<nric>/history", methods=["POST"])
@role_required("doctor")
def add_history(nric):
    f = request.form
    user = current_user()
    kind = f.get("kind")
    start, end = _date(f.get("start")), _date(f.get("end"))
    notes = f.get("notes", "").strip()[:500]
    base = {"nric": nric, "recordedAt": now(), "recordedBy": {"role": "doctor", "id": user["id"], "name": user["name"]},
            "verificationStatus": f.get("verification") if f.get("verification") in
            ("patient_reported", "clinician_confirmed", "external_record") else "patient_reported",
            "schemaVersion": 1}
    enc = records.todays_encounter(nric)
    if enc:
        base["encounterId"] = enc["_id"]
    try:
        if kind in ("condition", "procedure", "family_history"):
            label = f.get("label", "").strip()[:120]
            if not label:
                raise ValueError("Enter what the condition or procedure was.")
            code = re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")[:40]
            details = {"notes": notes} if notes else {}
            if kind == "family_history":
                rel = f.get("relation")
                if rel not in ("father", "mother", "sibling", "child", "grandparent"):
                    raise ValueError("Choose the relative.")
                details["relation"] = rel
                if f.get("age_at_onset", type=int):
                    details["ageAtOnset"] = f.get("age_at_onset", type=int)
                status = "active"
            elif kind == "procedure":
                status = "completed"
                end = end or start
            else:
                status = "resolved" if end else "active"
            doc = base | {"eventType": kind, "status": status,
                          "effectivePeriod": {"start": _dt(start or today()), "end": _dt(end)},
                          "code": {"system": "local-procedure-code" if kind == "procedure" else "local-condition-code",
                                   "value": code, "display": label},
                          "details": details}
            hid = _insert_with_id("patient_history", "H", doc)
        elif kind in ("smoking", "alcohol"):
            if kind == "smoking":
                st = f.get("smoking_status")
                if st not in ("never_smoker", "current_smoker", "former_smoker"):
                    raise ValueError("Choose a smoking status.")
                details = {"smokingStatus": st}
                if st == "current_smoker" and f.get("per_day", type=int):
                    details["cigarettesPerDay"] = f.get("per_day", type=int)
                if st == "former_smoker":
                    details["quitDate"] = _dt(start or today())
            else:
                use = f.get("alcohol_use", "").strip()
                if not use:
                    raise ValueError("Describe alcohol use, e.g. none, occasional, weekly.")
                details = {"alcoholUse": use[:40]}
                if f.get("units") not in (None, ""):
                    details["unitsPerWeek"] = f.get("units", type=float) or 0
            if notes:
                details["notes"] = notes
            # A change of status: close the current event FIRST (the unique index allows
            # only one active smoking / alcohol event per patient), then add the new one.
            change_day = _dt(start or today())
            mongo().patient_history.update_one(
                {"nric": nric, "eventType": "social_history", "category": kind, "status": "active"},
                {"$set": {"status": "superseded", "effectivePeriod.end": change_day}})
            doc = base | {"eventType": "social_history", "category": kind, "status": "active",
                          "effectivePeriod": {"start": change_day, "end": None}, "details": details}
            hid = _insert_with_id("patient_history", "H", doc)
        else:
            raise ValueError("Choose what kind of history to add.")
        flash(f"History event {hid} added.", "success")
    except ValueError as e:
        flash(str(e), "error")
    except WriteError as e:
        flash(f"MongoDB refused the event: {_mongo_error(e)}", "error")
    return redirect(url_for("patients.record", nric=nric) + "#history")


@bp.route("/patients/<nric>/history/<hid>/error", methods=["POST"])
@role_required("doctor")
def history_error(nric, hid):
    """Soft delete: keep the event but mark it entered in error."""
    mongo().patient_history.update_one({"_id": hid, "nric": nric}, {"$set": {"status": "entered_in_error"}})
    flash("History event marked as entered in error.", "success")
    return redirect(url_for("patients.record", nric=nric) + "#history")


# ---- visits: triage + consultation note (MongoDB encounters) -------------------------
@bp.route("/patients/<nric>/visits", methods=["POST"])
@role_required("doctor")
def add_visit(nric):
    f = request.form
    user = current_user()
    dx_ids = [int(x) for x in f.getlist("diagnosis_ids") if x.isdigit()]
    valid = {g["diagnosisid"] for g in records.diagnoses(nric)}
    dx_ids = [d for d in dx_ids if d in valid]
    doc_row = query_one("SELECT Hospital FROM Doctor WHERE DoctorID = %s", (user["id"],))

    def num(name, cast=int):
        v = f.get(name, "").strip()
        return cast(v) if v else None

    try:
        vitals = {"systolicBP": num("sbp"), "diastolicBP": num("dbp"), "heartRate": num("hr"),
                  "respRate": num("rr"), "temperatureC": num("temp", float), "spo2": num("spo2"),
                  "weightKg": num("weight", float)}
    except ValueError:
        flash("Vitals must be numbers.", "error")
        return redirect(url_for("patients.record", nric=nric) + "#visits")
    vitals = {k: v for k, v in vitals.items() if v is not None}
    missing = [n for n, k in (("BP", "systolicBP"), ("BP", "diastolicBP"), ("heart rate", "heartRate"),
                              ("temperature", "temperatureC"), ("SpO2", "spo2")) if k not in vitals]
    if missing or not dx_ids or len(f.get("note", "").strip()) < 10 or not f.get("complaint", "").strip():
        flash("A visit needs the complaint, BP, heart rate, temperature, SpO2, at least one diagnosis "
              "and a note of 10 or more characters.", "error")
        return redirect(url_for("patients.record", nric=nric) + "#visits")
    doc = {
        "nric": nric, "visitDate": now(), "visitType": f.get("visit_type") if f.get("visit_type") in ("new problem", "follow-up") else "follow-up",
        "doctor": {"doctorId": user["id"], "name": user["name"], "hospital": (doc_row or {}).get("hospital") or ""},
        "triage": {"recordedAt": now(), "category": f.get("category") if f.get("category") in ("P1", "P2", "P3", "P4") else "P4",
                   "chiefComplaint": f.get("complaint", "").strip()[:200],
                   "painScore": max(0, min(10, num("pain") or 0)), "vitals": vitals},
        "consultation": {"note": f.get("note", "").strip()[:4000], "diagnosisIds": dx_ids},
        "prescriptionIds": [],
    }
    if num("follow_up"):
        doc["consultation"]["followUpInDays"] = num("follow_up")
    try:
        eid = _insert_with_id("encounters", "E", doc)
        flash(f"Visit {eid} saved.", "success")
    except WriteError as e:
        flash(f"MongoDB refused the visit: {_mongo_error(e)}", "error")
    return redirect(url_for("patients.record", nric=nric) + "#visits")

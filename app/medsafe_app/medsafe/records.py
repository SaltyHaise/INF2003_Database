"""Reading a patient's record from both databases (used by Prescribe and Patient records)."""
from datetime import datetime, time

from pymongo import DESCENDING

from .db import mongo, query, query_one
from .util import now, today

HISTORY_KIND = {"condition": "Condition", "procedure": "Procedure", "social_history": "Lifestyle",
                "family_history": "Family"}


def patient(nric):
    return query_one("SELECT NRIC, Name, DOB, Sex, PhoneNumber FROM Patient WHERE NRIC = %s", (nric,))


def allergies(nric):
    return query("""
        SELECT al.IngredientID, ai.IngredientName, al.Severity, al.Reactions, al.Date
        FROM IsAllergicTo al JOIN ActiveIngredients ai ON ai.IngredientID = al.IngredientID
        WHERE al.NRIC = %s
        ORDER BY CASE al.Severity WHEN 'Severe' THEN 0 WHEN 'Moderate' THEN 1 ELSE 2 END, ai.IngredientName""", (nric,))


def active_meds(nric):
    return query("SELECT * FROM v_active_medications WHERE NRIC = %s ORDER BY PrescriptionStart DESC", (nric,))


def other_prescriptions(nric):
    """Everything that isn't active now: past, future, pending and returned."""
    return query("""
        SELECT * FROM v_prescription_detail
        WHERE NRIC = %s AND PrescriptionID NOT IN (SELECT PrescriptionID FROM v_active_medications WHERE NRIC = %s)
        ORDER BY PrescriptionStart DESC, PrescriptionID DESC""", (nric, nric))


def diagnoses(nric):
    return query("""
        SELECT g.DiagnosisID, g.DiseaseID, ds.Name AS disease, g.DateOfDiagnosis, g.DoctorID, doc.Name AS doctor_name,
               (SELECT count(*) FROM Prescription r WHERE r.DiagnosisID = g.DiagnosisID) AS prescriptions
        FROM Diagnosis g JOIN Disease ds ON ds.DiseaseID = g.DiseaseID JOIN Doctor doc ON doc.DoctorID = g.DoctorID
        WHERE g.NRIC = %s ORDER BY g.DateOfDiagnosis DESC, g.DiagnosisID DESC""", (nric,))


def history(nric):
    """MongoDB patient_history, newest first (entered-in-error events hidden)."""
    events = list(mongo().patient_history.find(
        {"nric": nric, "status": {"$ne": "entered_in_error"}}).sort("effectivePeriod.start", DESCENDING))
    for e in events:
        e["kind"] = HISTORY_KIND.get(e["eventType"], e["eventType"])
        e["title"] = history_title(e)
        e["facts"] = history_facts(e)
    return events


def history_title(e):
    d = e.get("details", {})
    if e["eventType"] == "social_history":
        if e.get("category") == "smoking":
            return {"never_smoker": "Never smoked", "current_smoker": "Current smoker",
                    "former_smoker": "Former smoker"}.get(d.get("smokingStatus"), "Smoking")
        return f"Alcohol: {d.get('alcoholUse', 'unknown')}"
    if e["eventType"] == "family_history":
        return f"{e['code']['display']} ({d.get('relation', 'relative')})"
    return e.get("code", {}).get("display", e["eventType"])


def history_facts(e):
    skip = {"notes", "smokingStatus"}
    facts = []
    for k, v in (e.get("details") or {}).items():
        if k in skip or v in (None, ""):
            continue
        if isinstance(v, datetime):
            v = v.strftime("%d %b %Y")
        facts.append((k, v))
    return facts


def current_social(nric):
    return {h["category"]: h for h in mongo().patient_history.find(
        {"nric": nric, "eventType": "social_history", "status": "active"})}


def encounters(nric, search=None, limit=8):
    f = {"nric": nric}
    if search:
        f["$text"] = {"$search": search}
    cur = mongo().encounters.find(f).sort("visitDate", DESCENDING).limit(limit)
    return list(cur)


def labs(nric):
    """Latest result of each test, with the last few values for a trend (MongoDB aggregation)."""
    return list(mongo().lab_results.aggregate([
        {"$match": {"nric": nric}},
        {"$sort": {"collectedAt": -1}},
        {"$group": {"_id": "$test", "testName": {"$first": "$testName"}, "value": {"$first": "$value"},
                    "unit": {"$first": "$unit"}, "flag": {"$first": "$flag"}, "refRange": {"$first": "$refRange"},
                    "targetRange": {"$first": "$targetRange"}, "collectedAt": {"$first": "$collectedAt"},
                    "recent": {"$push": {"v": "$value", "f": "$flag"}}}},
        {"$project": {"testName": 1, "value": 1, "unit": 1, "flag": 1, "refRange": 1, "targetRange": 1,
                      "collectedAt": 1, "recent": {"$slice": ["$recent", 5]}}},
        {"$sort": {"flag": 1, "_id": 1}},
    ]))


def todays_encounter(nric):
    start = datetime.combine(today(), time(0), tzinfo=now().tzinfo)
    return mongo().encounters.find_one({"nric": nric, "visitDate": {"$gte": start}}, sort=[("visitDate", -1)])


def summary_lines(nric, dxs=None, hist=None):
    """Short 'Conditions & history' lines for the Prescribe screen."""
    from .safety import HISTORY_RULES
    dxs = dxs if dxs is not None else diagnoses(nric)
    hist = hist if hist is not None else history(nric)
    names = []
    for g in dxs:
        if g["disease"] not in names:
            names.append(g["disease"])
    flagged_codes = {c: r["flag"] for r in HISTORY_RULES for c in r["codes"]}
    lines = []
    for e in hist:
        if e["eventType"] in ("condition", "procedure"):
            lines.append({"text": e["title"], "when": e["effectivePeriod"]["start"],
                          "flag": flagged_codes.get(e.get("code", {}).get("value"))})
        elif e["eventType"] == "social_history" and e["status"] == "active" and e.get("category") == "smoking":
            lines.append({"text": e["title"], "when": None, "flag": None,
                          "extra": e["details"].get("quitDate")})
    return names, lines[:6]

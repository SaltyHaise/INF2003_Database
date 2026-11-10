"""Insights: analytics from both databases.

The page is ordered by who can act on it:
  1. Patients to look at now (PostgreSQL): interacting medicines being taken together,
     patients on many medicines, the most prescribed drugs.
  2. Alerts and overrides (MongoDB prescription_audit): when a prescription was sent
     despite alerts, what the pharmacist then did. This is the usual way to judge
     whether alerts are useful or just clicked through ("alert fatigue").
  3. About the system (MongoDB + PostgreSQL): review turnaround, prescription status.
"""
from flask import Blueprint, render_template

from .auth import role_required
from .db import mongo, query

bp = Blueprint("insights", __name__)

ALERT_TYPE_ORDER = {"allergy": 0, "interaction": 1, "lab": 2, "history": 3, "duplicate": 4, "none": 9}
ALERT_TYPE_LABEL = {"allergy": "Allergy", "interaction": "Interaction", "lab": "Lab result", "history": "Past condition",
                    "duplicate": "Duplicate ingredient", "none": "No alerts"}


def _outcome_counts(group_id):
    """$group stage counting a prescription's final status, plus whether the doctor gave a reason."""
    def when(cond):
        return {"$sum": {"$cond": [cond, 1, 0]}}
    return {"$group": {
        "_id": group_id, "total": {"$sum": 1},
        "approved": when({"$eq": ["$status", "Approved"]}),
        "returned": when({"$eq": ["$status", "Rejected"]}),
        "pending": when({"$eq": ["$status", "Pending"]}),
        "withReason": when("$hasReason")}}


@bp.route("/insights")
@role_required("doctor", "pharmacist")
def insights():
    audit = mongo().prescription_audit

    # ---- 2. alerts and overrides (MongoDB) ---------------------------------------------
    # Every audit document is a prescription that was SENT. With alerts, that means the
    # doctor overrode them; the "prescribed" event holds the reason they gave.
    shape = {"$project": {
        "status": 1,
        "hasAlerts": {"$gt": [{"$size": "$alerts"}, 0]},
        # alert types on this prescription, each once; "none" when there were no alerts
        "types": {"$cond": [{"$gt": [{"$size": "$alerts"}, 0]}, {"$setUnion": ["$alerts.type", []]}, ["none"]]},
        "hasReason": {"$gt": [{"$size": {"$filter": {"input": "$events", "cond": {"$and": [
            {"$eq": ["$$this.action", "prescribed"]},
            {"$gt": [{"$ifNull": ["$$this.reason", ""]}, ""]}]}}}}, 0]},  # a non-empty reason
    }}
    # one row per alert type ($unwind the types; a prescription with two types counts in both)
    overrides = list(audit.aggregate([shape, {"$unwind": "$types"}, _outcome_counts("$types")]))
    overrides.sort(key=lambda r: ALERT_TYPE_ORDER.get(r["_id"], 5))
    # totals: any alert vs no alert (each prescription once)
    totals = {r["_id"]: r for r in audit.aggregate([shape, _outcome_counts("$hasAlerts")])}
    for r in overrides + list(totals.values()):
        if isinstance(r["_id"], str):
            r["label"] = ALERT_TYPE_LABEL.get(r["_id"], r["_id"].title())
        decided = r["approved"] + r["returned"]
        r["return_rate"] = 100 * r["returned"] / decided if decided else None
    alerted = totals.get(True)

    # ---- 3. about the system: review turnaround per pharmacist (MongoDB) ----------------
    turnaround = list(audit.aggregate([
        {"$match": {"status": {"$in": ["Approved", "Rejected"]}}},
        {"$project": {
            "p": {"$first": {"$filter": {"input": "$events", "cond": {"$eq": ["$$this.action", "prescribed"]}}}},
            "r": {"$first": {"$filter": {"input": "$events", "cond": {"$in": ["$$this.action", ["approved", "rejected"]]}}}}}},
        {"$project": {"name": "$r.by.name", "hours": {"$divide": [{"$subtract": ["$r.at", "$p.at"]}, 3600000]}}},
        {"$group": {"_id": "$name", "reviews": {"$sum": 1}, "avg": {"$avg": "$hours"}, "max": {"$max": "$hours"}}},
        {"$sort": {"avg": 1}}]))

    # ---- 1. patients to look at now (PostgreSQL), and 3. status counts ------------------
    status_counts = query("SELECT ReviewStatus AS status, count(*) AS n FROM Prescription GROUP BY 1 ORDER BY 2 DESC")
    top_drugs = query("""
        SELECT GenericName, count(*) AS n, count(DISTINCT NRIC) AS patients,
               rank() OVER (ORDER BY count(*) DESC) AS rnk
        FROM v_prescription_detail GROUP BY GenericName ORDER BY n DESC LIMIT 10""")
    polypharmacy = query("""
        SELECT NRIC, patient_name, count(*) AS meds
        FROM v_active_medications GROUP BY NRIC, patient_name
        HAVING count(*) >= (SELECT avg(c) + 1 FROM (SELECT count(*) AS c FROM v_active_medications GROUP BY NRIC) s)
        ORDER BY meds DESC, patient_name LIMIT 10""")
    interacting_now = query("""
        SELECT a1.patient_name, a1.NRIC, a1.GenericName AS drug_1, a2.GenericName AS drug_2,
               string_agg(DISTINCT x.Symptom, ', ') AS effects, bool_or(x.LabelSaysAvoid) AS avoid
        FROM v_active_medications a1
        JOIN v_active_medications a2 ON a2.NRIC = a1.NRIC AND a2.PrescriptionID > a1.PrescriptionID
        JOIN Contains c1 ON c1.DrugID = a1.DrugID
        JOIN Contains c2 ON c2.DrugID = a2.DrugID
        JOIN DrugtoDrugInteraction x ON x.IngredientID_A = LEAST(c1.IngredientID, c2.IngredientID)
                                    AND x.IngredientID_B = GREATEST(c1.IngredientID, c2.IngredientID)
        GROUP BY a1.patient_name, a1.NRIC, a1.GenericName, a2.GenericName
        ORDER BY bool_or(x.LabelSaysAvoid) DESC, a1.patient_name LIMIT 15""")

    return render_template("insights.html", overrides=overrides, alerted=alerted, no_alerts=totals.get(False),
                           turnaround=turnaround, status_counts=status_counts,
                           top_drugs=top_drugs, polypharmacy=polypharmacy, interacting_now=interacting_now)

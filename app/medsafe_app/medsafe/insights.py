"""Insights: analytics from both databases.

MongoDB aggregation pipelines over prescription_audit, plus PostgreSQL aggregate queries.
"""
from flask import Blueprint, render_template

from .auth import role_required
from .db import mongo, query

bp = Blueprint("insights", __name__)


@bp.route("/insights")
@role_required("doctor", "pharmacist")
def insights():
    audit = mongo().prescription_audit

    # MongoDB: which alerts fire most ($unwind + $group)
    alert_types = list(audit.aggregate([
        {"$unwind": "$alerts"},
        {"$group": {"_id": "$alerts.type", "alerts": {"$sum": 1}, "prescriptions": {"$addToSet": "$_id"}}},
        {"$project": {"alerts": 1, "prescriptions": {"$size": "$prescriptions"}}},
        {"$sort": {"alerts": -1}}]))
    top_effects = list(audit.aggregate([
        {"$unwind": "$alerts"},
        {"$match": {"alerts.type": "interaction"}},
        {"$group": {"_id": "$alerts.effect", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 8}]))

    # MongoDB: outcome of prescriptions with vs without alerts ($facet)
    outcome = list(audit.aggregate([
        {"$project": {"status": 1, "hasAlerts": {"$gt": [{"$size": "$alerts"}, 0]}}},
        {"$group": {"_id": {"alerts": "$hasAlerts", "status": "$status"}, "n": {"$sum": 1}}},
        {"$sort": {"_id.alerts": -1, "_id.status": 1}}]))

    # MongoDB: review turnaround per pharmacist (time between the two events)
    turnaround = list(audit.aggregate([
        {"$match": {"status": {"$in": ["Approved", "Rejected"]}}},
        {"$project": {
            "p": {"$first": {"$filter": {"input": "$events", "cond": {"$eq": ["$$this.action", "prescribed"]}}}},
            "r": {"$first": {"$filter": {"input": "$events", "cond": {"$in": ["$$this.action", ["approved", "rejected"]]}}}}}},
        {"$project": {"name": "$r.by.name", "hours": {"$divide": [{"$subtract": ["$r.at", "$p.at"]}, 3600000]}}},
        {"$group": {"_id": "$name", "reviews": {"$sum": 1}, "avg": {"$avg": "$hours"}, "max": {"$max": "$hours"}}},
        {"$sort": {"avg": 1}}]))

    # MongoDB: alert rate per doctor
    per_doctor = list(audit.aggregate([
        {"$group": {"_id": "$prescribedBy.name", "total": {"$sum": 1},
                    "alerted": {"$sum": {"$cond": [{"$gt": [{"$size": "$alerts"}, 0]}, 1, 0]}},
                    "returned": {"$sum": {"$cond": [{"$eq": ["$status", "Rejected"]}, 1, 0]}}}},
        {"$addFields": {"rate": {"$multiply": [{"$divide": ["$alerted", "$total"]}, 100]}}},
        {"$sort": {"rate": -1}}]))

    # PostgreSQL: aggregates, a window function and a nested query
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

    return render_template("insights.html", alert_types=alert_types, top_effects=top_effects, outcome=outcome,
                           turnaround=turnaround, per_doctor=per_doctor, status_counts=status_counts,
                           top_drugs=top_drugs, polypharmacy=polypharmacy, interacting_now=interacting_now)

"""The prescription safety check.

For a patient and one or more candidate drugs, it finds:
  PostgreSQL  interaction   candidate ingredient x active-medication ingredient (DrugtoDrugInteraction)
              allergy       candidate ingredient the patient is allergic to (IsAllergicTo)
              duplicate     candidate ingredient the patient is already taking
  MongoDB     lab           latest lab result crossing a lab_rules threshold (aggregation + $lookup)
              history       past condition in patient_history (or a PostgreSQL diagnosis) that
                            makes the drug risky (HISTORY_RULES below)
together() adds the same interaction and duplicate checks between medicines prescribed in
the same visit, which aren't active medications yet.
Active medications come from the v_active_medications view. "No alerts" only means nothing
is recorded in our data, not that a drug is safe.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta

from .db import mongo, query
from .util import effect_label, now

NSAIDS = ["IBUPROFEN", "NAPROXEN", "DICLOFENAC", "CELECOXIB", "MELOXICAM", "KETOROLAC",
          "INDOMETHACIN", "ETODOLAC", "PIROXICAM", "MEFENAMIC ACID"]
ANTICOAGULANTS = ["WARFARIN", "APIXABAN", "RIVAROXABAN", "DABIGATRAN", "CLOPIDOGREL"]

# Past conditions that make a drug risky. "codes" match patient_history code.value in
# MongoDB; "diseases" match PostgreSQL diagnoses (Disease.Name).
HISTORY_RULES = [
    {"id": "gi-bleed", "codes": ["PEPTIC_ULCER", "UPPER_GI_BLEED"], "diseases": ["Peptic ulcer disease"],
     "ingredients": NSAIDS + ["ASPIRIN"] + ANTICOAGULANTS, "severity": "major", "flag": "bleeding risk",
     "message": "Past peptic ulcer or GI bleed. This drug raises the risk of another bleed."},
    {"id": "kidney", "codes": ["CKD"], "diseases": [],
     "ingredients": NSAIDS + ["METFORMIN"], "severity": "major", "flag": "kidney",
     "message": "Chronic kidney disease on record. Check the dose or avoid."},
    {"id": "asthma-nsaid", "codes": ["ASTHMA_CHILDHOOD", "ASTHMA"], "diseases": ["Asthma, unspecified"],
     "ingredients": NSAIDS + ["ASPIRIN"], "severity": "moderate", "flag": "bronchospasm",
     "message": "History of asthma. NSAIDs and aspirin can trigger bronchospasm in some patients."},
    {"id": "hep-b-liver", "codes": ["HEPATITIS_B_CARRIER"], "diseases": ["Chronic hepatitis B"],
     "ingredients": ["ACETAMINOPHEN", "ATORVASTATIN", "SIMVASTATIN"], "severity": "moderate", "flag": "liver",
     "message": "Chronic hepatitis B. Check liver function before using this drug regularly."},
]

SEVERITY_ORDER = {"severe": 0, "avoid": 0, "major": 1, "moderate": 2, "caution": 3, "mild": 4}


def _trim(text, n=280):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 3].rsplit(" ", 1)[0] + "..."


def check(nric, drug_ids):
    """Return {drug_id: result} for each candidate drug. See module docstring."""
    drug_ids = [int(d) for d in drug_ids]
    out = {d: {"alerts": [], "interactions": [], "allergies": [], "duplicates": [], "labs": [],
               "history": [], "ingredients": [], "side_effects": []} for d in drug_ids}
    if not drug_ids:
        return out

    # ---- ingredients of each candidate --------------------------------------------
    for r in query("""
            SELECT c.DrugID, ai.IngredientName FROM Contains c
            JOIN ActiveIngredients ai ON ai.IngredientID = c.IngredientID
            WHERE c.DrugID = ANY(%s) ORDER BY ai.IngredientName""", (drug_ids,)):
        out[r["drugid"]]["ingredients"].append(r["ingredientname"])

    # ---- interactions with active medications (PostgreSQL) ------------------------
    rows = query("""
        WITH cand AS (SELECT DrugID, IngredientID FROM Contains WHERE DrugID = ANY(%(ids)s)),
             act  AS (SELECT a.PrescriptionID, a.DrugID, a.GenericName, c.IngredientID
                      FROM v_active_medications a JOIN Contains c ON c.DrugID = a.DrugID
                      WHERE a.NRIC = %(nric)s)
        SELECT cand.DrugID AS cand_drug, act.GenericName AS with_drug, act.PrescriptionID AS with_rx,
               ia.IngredientName AS ing_a, ib.IngredientName AS ing_b,
               x.Symptom, x.Direction, x.LabelSaysAvoid, x.ExampleSentence
        FROM cand
        JOIN act ON act.DrugID <> cand.DrugID
        JOIN DrugtoDrugInteraction x ON x.IngredientID_A = LEAST(cand.IngredientID, act.IngredientID)
                                    AND x.IngredientID_B = GREATEST(cand.IngredientID, act.IngredientID)
        JOIN ActiveIngredients ia ON ia.IngredientID = x.IngredientID_A
        JOIN ActiveIngredients ib ON ib.IngredientID = x.IngredientID_B
        ORDER BY x.LabelSaysAvoid DESC, act.GenericName, x.Symptom""", {"ids": drug_ids, "nric": nric})
    seen = set()
    for r in rows:
        key = (r["cand_drug"], r["with_drug"], r["symptom"])
        if key in seen:
            continue
        seen.add(key)
        out[r["cand_drug"]]["interactions"].append({
            "type": "interaction", "severity": "avoid" if r["labelsaysavoid"] else "caution",
            "ingredients": [r["ing_a"], r["ing_b"]], "withDrug": r["with_drug"], "withPrescriptionId": r["with_rx"],
            "effect": r["symptom"], "direction": r["direction"],
            "message": _trim(r["examplesentence"]) or f"{r['ing_a']} + {r['ing_b']}: {effect_label(r['symptom'])}.",
        })

    # ---- same ingredient already being taken (PostgreSQL) -------------------------
    for r in query("""
            SELECT c.DrugID AS cand_drug, a.GenericName AS with_drug, a.PrescriptionID AS with_rx, ai.IngredientName
            FROM Contains c
            JOIN Contains ac ON ac.IngredientID = c.IngredientID
            JOIN v_active_medications a ON a.DrugID = ac.DrugID AND a.NRIC = %s
            JOIN ActiveIngredients ai ON ai.IngredientID = c.IngredientID
            WHERE c.DrugID = ANY(%s)""", (nric, drug_ids)):
        out[r["cand_drug"]]["duplicates"].append({
            "type": "duplicate", "severity": "moderate", "ingredient": r["ingredientname"],
            "withDrug": r["with_drug"], "withPrescriptionId": r["with_rx"],
            "message": f"Already taking {r['ingredientname']} ({r['with_drug']}, prescription {r['with_rx']}).",
        })

    # ---- allergies (PostgreSQL) ----------------------------------------------------
    for r in query("""
            SELECT c.DrugID, ai.IngredientName, al.Severity, al.Reactions, al.Date
            FROM Contains c
            JOIN IsAllergicTo al ON al.IngredientID = c.IngredientID AND al.NRIC = %s
            JOIN ActiveIngredients ai ON ai.IngredientID = c.IngredientID
            WHERE c.DrugID = ANY(%s)""", (nric, drug_ids)):
        sev = (r["severity"] or "moderate").lower()
        out[r["drugid"]]["allergies"].append({
            "type": "allergy", "severity": sev, "ingredient": r["ingredientname"], "reaction": r["reactions"],
            "message": f"Recorded {sev} allergy to {r['ingredientname'].lower()}: {(r['reactions'] or 'reaction not recorded').lower()}.",
        })

    # ---- common side effects (PostgreSQL, per ingredient) -------------------------
    for r in query("""
            SELECT c.DrugID, array_agg(DISTINCT se.EffectName) AS effects
            FROM Contains c
            JOIN HasSideEffects h ON h.IngredientID = c.IngredientID
            JOIN SideEffects se ON se.EffectID = h.EffectID
            WHERE c.DrugID = ANY(%s) GROUP BY c.DrugID""", (drug_ids,)):
        out[r["drugid"]]["side_effects"] = sorted(r["effects"])[:4]

    all_ings = sorted({i for v in out.values() for i in v["ingredients"]})

    # ---- lab results (MongoDB: lab_rules + latest lab_results) ---------------------
    for hit in lab_alerts(nric, all_ings):
        for d, v in out.items():
            if set(hit["ingredients"]) & set(v["ingredients"]):
                v["labs"].append({k: hit[k] for k in ("type", "severity", "ruleId", "test", "value", "unit", "collectedAt", "message")})

    # ---- history (MongoDB patient_history + PostgreSQL diagnoses) ------------------
    hist = history_matches(nric)
    for rule, evidence in hist:
        for d, v in out.items():
            if set(rule["ingredients"]) & set(v["ingredients"]):
                v["history"].append({
                    "type": "history", "severity": rule["severity"], "ruleId": rule["id"],
                    "condition": evidence["label"], "when": evidence["when"], "flag": rule["flag"],
                    "message": rule["message"],
                })

    for v in out.values():
        v["alerts"] = sorted(v["allergies"] + v["interactions"] + v["duplicates"] + v["labs"] + v["history"],
                             key=lambda a: (SEVERITY_ORDER.get(a["severity"], 5), a["type"]))
    return out


def lab_alerts(nric, ingredients, as_of=None):
    """MongoDB: for each lab rule about these ingredients, the patient's latest result for
    that test inside the rule's window, if it crosses the threshold."""
    if not ingredients:
        return []
    as_of = as_of or now()
    pipeline = [
        {"$match": {"ingredients": {"$in": ingredients}}},
        {"$lookup": {
            "from": "lab_results",
            "let": {"test": "$test", "days": "$withinDays"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$nric", nric]},
                    {"$eq": ["$test", "$$test"]},
                    {"$lte": ["$collectedAt", as_of]},
                    {"$gte": ["$collectedAt", {"$dateSubtract": {"startDate": as_of, "unit": "day", "amount": "$$days"}}]},
                ]}}},
                {"$sort": {"collectedAt": -1}},
                {"$limit": 1},
            ],
            "as": "latest"}},
        {"$unwind": "$latest"},
        {"$match": {"$expr": {"$cond": [{"$eq": ["$op", "gt"]},
                                        {"$gt": ["$latest.value", "$threshold"]},
                                        {"$lt": ["$latest.value", "$threshold"]}]}}},
        {"$project": {"_id": 0, "type": "lab", "ruleId": "$_id", "severity": 1, "test": 1, "message": 1,
                      "ingredients": 1, "value": "$latest.value", "unit": "$latest.unit",
                      "collectedAt": "$latest.collectedAt"}},
    ]
    return list(mongo().lab_rules.aggregate(pipeline))


def history_matches(nric):
    """[(rule, evidence)] for past conditions that a HISTORY_RULE cares about."""
    found = []
    codes = defaultdict(list)
    for h in mongo().patient_history.find(
            {"nric": nric, "eventType": "condition", "status": {"$in": ["active", "resolved"]}},
            {"code": 1, "effectivePeriod": 1, "status": 1}):
        codes[h["code"]["value"]].append(h)
    diseases = {r["name"]: r for r in query("""
        SELECT ds.Name, max(g.DateOfDiagnosis) AS since FROM Diagnosis g JOIN Disease ds ON ds.DiseaseID = g.DiseaseID
        WHERE g.NRIC = %s GROUP BY ds.Name""", (nric,))}
    for rule in HISTORY_RULES:
        ev = None
        for c in rule["codes"]:
            if codes.get(c):
                h = codes[c][0]
                ev = {"label": h["code"]["display"], "when": h["effectivePeriod"]["start"], "source": "MongoDB"}
                break
        if not ev:
            for name in rule["diseases"]:
                if name in diseases:
                    ev = {"label": name, "when": diseases[name]["since"], "source": "PostgreSQL"}
                    break
        if ev:
            found.append((rule, ev))
    return found


def together(drug_ids, picked):
    """Interactions and shared ingredients between each drug in drug_ids and the medicines
    picked in this visit (`picked`, not yet prescribed). Returns {drug_id: [alerts]};
    each alert carries withDrugId so the caller can fill in withPrescriptionId on save."""
    drug_ids, picked = [int(d) for d in drug_ids], [int(d) for d in picked]
    out = {d: [] for d in drug_ids}
    if not drug_ids or not picked:
        return out
    names = {r["drugid"]: r["genericname"] for r in query(
        "SELECT DrugID, GenericName FROM Drug WHERE DrugID = ANY(%s)", (picked,))}
    seen = set()
    for r in query("""
            WITH a AS (SELECT DrugID, IngredientID FROM Contains WHERE DrugID = ANY(%(ids)s)),
                 b AS (SELECT DrugID, IngredientID FROM Contains WHERE DrugID = ANY(%(picked)s))
            SELECT a.DrugID AS drug, b.DrugID AS other, ia.IngredientName AS ing_a, ib.IngredientName AS ing_b,
                   x.Symptom, x.Direction, x.LabelSaysAvoid, x.ExampleSentence
            FROM a JOIN b ON b.DrugID <> a.DrugID
            JOIN DrugtoDrugInteraction x ON x.IngredientID_A = LEAST(a.IngredientID, b.IngredientID)
                                        AND x.IngredientID_B = GREATEST(a.IngredientID, b.IngredientID)
            JOIN ActiveIngredients ia ON ia.IngredientID = x.IngredientID_A
            JOIN ActiveIngredients ib ON ib.IngredientID = x.IngredientID_B
            ORDER BY x.LabelSaysAvoid DESC, x.Symptom""", {"ids": drug_ids, "picked": picked}):
        key = (r["drug"], r["other"], r["symptom"])
        if key in seen:
            continue
        seen.add(key)
        out[r["drug"]].append({
            "type": "interaction", "severity": "avoid" if r["labelsaysavoid"] else "caution",
            "ingredients": [r["ing_a"], r["ing_b"]], "withDrug": names[r["other"]], "withDrugId": r["other"],
            "sameVisit": True, "effect": r["symptom"], "direction": r["direction"],
            "message": _trim(r["examplesentence"]) or f"{r['ing_a']} + {r['ing_b']}: {effect_label(r['symptom'])}.",
        })
    for r in query("""
            SELECT DISTINCT a.DrugID AS drug, b.DrugID AS other, ai.IngredientName
            FROM Contains a JOIN Contains b ON b.IngredientID = a.IngredientID AND b.DrugID <> a.DrugID
            JOIN ActiveIngredients ai ON ai.IngredientID = a.IngredientID
            WHERE a.DrugID = ANY(%s) AND b.DrugID = ANY(%s)""", (drug_ids, picked)):
        out[r["drug"]].append({
            "type": "duplicate", "severity": "moderate", "ingredient": r["ingredientname"],
            "withDrug": names[r["other"]], "withDrugId": r["other"], "sameVisit": True,
            "message": f"{r['ingredientname']} is also in {names[r['other']]}, prescribed in this visit.",
        })
    return out


def add_together(checks, picked):
    """Merge together() into safety.check() results, in place."""
    extra = together(list(checks), picked)
    for d, alerts in extra.items():
        v = checks[d]
        v["interactions"] += [a for a in alerts if a["type"] == "interaction"]
        v["duplicates"] += [a for a in alerts if a["type"] == "duplicate"]
        v["alerts"] = sorted(v["alerts"] + alerts, key=lambda a: (SEVERITY_ORDER.get(a["severity"], 5), a["type"]))
    return checks


def mongo_safe_alerts(alerts):
    """Alerts in the shape stored in prescription_audit (dates as datetimes)."""
    clean = []
    for a in alerts:
        a = dict(a)
        for k in ("collectedAt", "when"):
            if k in a and a[k] is not None and not isinstance(a[k], datetime):
                a[k] = datetime.combine(a[k], time(0), tzinfo=now().tzinfo)
        clean.append(a)
    return clean


def end_date(start, days):
    return start + timedelta(days=days - 1) if days else None

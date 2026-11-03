// ===========================================================================
// INF2003 G61 - MedSafety: re-run the MongoDB setup check (after medsafety_setup_mongo.js)
// ===========================================================================
//   Terminal:  mongosh check_setup_mongo.js
//   Compass:   MONGOSH tab -> load("C:/full/path/to/check_setup_mongo.js")
// Every row should say PASS. The validation tests insert and then delete two test documents.
// ===========================================================================

db = db.getSiblingDB("medsafety");

// ---------------------------------------------------------------------------
// 6. Lab part of the safety check
//    Given a patient and the proposed drug's ingredient names (from PostgreSQL:
//    Contains + ActiveIngredients), return lab alerts: the most recent result of each
//    relevant test, inside the rule's time window, that crosses the rule's threshold.
//    Your backend runs the same pipeline (copy it into your code).
// ---------------------------------------------------------------------------
function labAlerts(nric, ingredients, asOf) {
  asOf = asOf || new Date();
  return db.lab_rules.aggregate([
    { $match: { ingredients: { $in: ingredients } } },
    { $lookup: {
        from: "lab_results",
        let: { test: "$test", days: "$withinDays" },
        pipeline: [
          { $match: { $expr: { $and: [
            { $eq: ["$nric", nric] },
            { $eq: ["$test", "$$test"] },
            { $lte: ["$collectedAt", asOf] },
            { $gte: ["$collectedAt", { $dateSubtract: { startDate: asOf, unit: "day", amount: "$$days" } }] }
          ] } } },
          { $sort: { collectedAt: -1 } },
          { $limit: 1 }
        ],
        as: "latest" } },
    { $unwind: "$latest" },
    { $match: { $expr: { $cond: [
        { $eq: ["$op", "gt"] },
        { $gt: ["$latest.value", "$threshold"] },
        { $lt: ["$latest.value", "$threshold"] } ] } } },
    { $project: { _id: 0, ruleId: "$_id", severity: 1, test: 1, message: 1,
                  value: "$latest.value", unit: "$latest.unit", collectedAt: "$latest.collectedAt" } },
    { $sort: { severity: 1, ruleId: 1 } }
  ]).toArray();
}

// ---------------------------------------------------------------------------
// 7. CHECK: every row should say PASS
// ---------------------------------------------------------------------------
const checks = [];
function check(item, expected, actual) {
  const ok = JSON.stringify(expected) === JSON.stringify(actual);
  checks.push({ item, expected: JSON.stringify(expected), actual: JSON.stringify(actual), result: ok ? "PASS" : "FAIL" });
}
function rejects(fn) {
  try { fn(); return "accepted"; } catch (e) { return (e.code === 121 || e.code === 11000 || /validation|duplicate/i.test(String(e))) ? "rejected" : "error: " + e.message; }
}

check("encounters", 249, db.encounters.countDocuments());
check("lab_results", 458, db.lab_results.countDocuments());
check("prescription_audit", 182, db.prescription_audit.countDocuments());
check("lab_rules", 7, db.lab_rules.countDocuments());
check("patient_history", 202, db.patient_history.countDocuments());
check("every patient has one active smoking status", [50, 1],
      (() => { const r = db.patient_history.aggregate([
        { $match: { category: "smoking", status: "active" } },
        { $group: { _id: "$nric", n: { $sum: 1 } } },
        { $group: { _id: null, patients: { $sum: 1 }, most: { $max: "$n" } } }]).toArray()[0];
        return [r.patients, r.most]; })());
check("ended smoking events have an end date", 0,
      db.patient_history.countDocuments({ category: "smoking", status: "superseded", "effectivePeriod.end": null }));
check("audit ids match PrescriptionID 1-182", 182, db.prescription_audit.countDocuments({ _id: { $gte: 1, $lte: 182 } }));
check("pending / rejected audits", [13, 1],
      [db.prescription_audit.countDocuments({ status: "Pending" }), db.prescription_audit.countDocuments({ status: "Rejected" })]);
check("audits with interaction or allergy alerts", 39,
      db.prescription_audit.countDocuments({ "alerts.type": { $in: ["interaction", "allergy"] } }));
check("patients with encounters", 46, db.encounters.distinct("nric").length);

// Scenarios: lab alerts for the pending prescriptions in the PostgreSQL demo
check("D8 lab: Foo Ai Ling, new lisinopril -> high potassium", ["potassium-high-raas"],
      labAlerts("S0047109G", ["LISINOPRIL"]).map(a => a.ruleId));
check("D3 lab: Deepa d/o Raju, new ibuprofen -> kidney + INR", ["egfr-low-nsaid", "inr-high-nsaid"],
      labAlerts("S0418246D", ["IBUPROFEN"]).map(a => a.ruleId).sort());
check("D5 lab: Heng Bee Choo, new ibuprofen -> kidney", ["egfr-low-nsaid"],
      labAlerts("S1958710Z", ["IBUPROFEN"]).map(a => a.ruleId));
check("D4 lab: Ravi s/o Chandran INR in range, no lab alert", [],
      labAlerts("S1343461A", ["WARFARIN"]).map(a => a.ruleId));
check("D1 lab: Daniel de Souza, new amlodipine, no lab alert", [],
      labAlerts("S0960703Z", ["AMLODIPINE"]).map(a => a.ruleId));
check("Lau Ah Seng on metformin, low eGFR", ["egfr-low-metformin"],
      labAlerts("S1154321I", ["METFORMIN"]).map(a => a.ruleId));

check("note search 'chest pain' finds visits", true,
      db.encounters.countDocuments({ $text: { $search: "\"chest pain\"" } }) > 0);

// Validation rules reject bad documents
check("validation: encounter without triage", "rejected", rejects(() =>
  db.encounters.insertOne({ _id: "E9999", nric: "S0047109G", visitDate: new Date(), visitType: "follow-up",
    doctor: { doctorId: 1, name: "x" }, consultation: { note: "test note only", diagnosisIds: [1] } })));
check("validation: lab result with unknown test", "rejected", rejects(() =>
  db.lab_results.insertOne({ nric: "S0047109G", test: "MAGIC", testName: "x", value: 1, unit: "x", refRange: {}, flag: "N", collectedAt: new Date() })));
check("validation: rejection without a note", "rejected", rejects(() =>
  db.prescription_audit.insertOne({ _id: 9999, nric: "S0047109G", diagnosisId: 1, drug: { drugId: 1, name: "x" },
    prescribedBy: { doctorId: 1, name: "x" }, status: "Rejected", alerts: [],
    events: [{ at: new Date(), action: "rejected", by: { role: "pharmacist", id: 1, name: "x" } }] })));
check("validation: condition without a code", "rejected", rejects(() =>
  db.patient_history.insertOne({ _id: "H9999", nric: "S0047109G", eventType: "condition", status: "active",
    effectivePeriod: { start: new Date(), end: null }, recordedAt: new Date(), verificationStatus: "patient_reported",
    details: {}, schemaVersion: 1 })));
check("unique index: second active smoking status", "rejected", rejects(() =>
  db.patient_history.insertOne({ _id: "H9998", nric: "S0047109G", eventType: "social_history", category: "smoking",
    status: "active", effectivePeriod: { start: new Date(), end: null }, recordedAt: new Date(),
    verificationStatus: "patient_reported", details: { smokingStatus: "current_smoker" }, schemaVersion: 1 })));
db.patient_history.deleteMany({ _id: { $in: ["H9998", "H9999"] } });
db.encounters.deleteOne({ _id: "E9999" });
db.prescription_audit.deleteOne({ _id: 9999 });

print("\nCHECK");
const w = Math.max(...checks.map(c => c.item.length));
checks.forEach(c => print(`${c.result}  ${c.item.padEnd(w)}  expected ${c.expected}  actual ${c.actual}`));
const failed = checks.filter(c => c.result !== "PASS").length;
print(failed ? `\n${failed} check(s) FAILED` : `\nAll ${checks.length} checks passed.`);

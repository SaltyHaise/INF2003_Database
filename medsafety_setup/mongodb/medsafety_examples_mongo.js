// ===========================================================================
// INF2003 G61 - MedSafety: example MongoDB queries (run after medsafety_setup_mongo.js)
// ===========================================================================
// Read-only: nothing here changes the data (the write examples at the end are commented out).
//   Terminal:  mongosh medsafety_examples_mongo.js
//   Compass:   MONGOSH tab -> load("C:/full/path/to/medsafety_examples_mongo.js")
// Or paste one query at a time into the MONGOSH tab after:  use medsafety
// ===========================================================================

db = db.getSiblingDB("medsafety");
const show = (title, value) => { print("\n=== " + title); printjson(value); };

// ---------------------------------------------------------------------------
// DOCTOR'S PATIENT VIEW
// ---------------------------------------------------------------------------
// 1. Last 3 visits for a patient: complaint, vitals and note (uses ix_enc_patient_date)
show("1. Last 3 visits: Foo Ai Ling", db.encounters.find(
  { nric: "S0047109G" },
  { _id: 1, visitDate: 1, "doctor.name": 1, "triage.chiefComplaint": 1, "triage.vitals": 1, "consultation.note": 1 }
).sort({ visitDate: -1 }).limit(3).toArray());

// 2. Latest result of every test for a patient, with flag (uses ix_lab_patient_test_date)
show("2. Latest lab results: Deepa d/o Raju", db.lab_results.aggregate([
  { $match: { nric: "S0418246D" } },
  { $sort: { test: 1, collectedAt: -1 } },
  { $group: { _id: "$test", testName: { $first: "$testName" }, value: { $first: "$value" }, unit: { $first: "$unit" },
              flag: { $first: "$flag" }, collectedAt: { $first: "$collectedAt" }, results: { $sum: 1 } } },
  { $sort: { _id: 1 } }
]).toArray());

// 3. Trend of one test over time (for a chart)
show("3. eGFR trend: Lau Ah Seng", db.lab_results.find(
  { nric: "S1154321I", test: "EGFR" }, { _id: 0, collectedAt: 1, value: 1, flag: 1 }
).sort({ collectedAt: 1 }).toArray());

// 4. Search a patient's notes, best match first (uses the text index tx_enc_notes)
show("4. Note search: 'chest pain' (all patients)", db.encounters.find(
  { $text: { $search: "\"chest pain\"" } },
  { score: { $meta: "textScore" }, nric: 1, visitDate: 1, "triage.chiefComplaint": 1 }
).sort({ score: { $meta: "textScore" } }).limit(5).toArray());

// ---------------------------------------------------------------------------
// PATIENT HISTORY (patient_history)
// ---------------------------------------------------------------------------
// 5. A patient's current history, grouped by type (what the doctor sees on the summary)
show("5. Current history: Deepa d/o Raju", db.patient_history.aggregate([
  { $match: { nric: "S0418246D", status: { $in: ["active", "resolved", "completed"] } } },
  { $sort: { "effectivePeriod.start": -1 } },
  { $group: { _id: "$eventType",
              items: { $push: { what: { $ifNull: ["$code.display", "$category"] }, status: "$status",
                                since: "$effectivePeriod.start", until: "$effectivePeriod.end", details: "$details" } } } },
  { $sort: { _id: 1 } }
]).toArray());

// 5b. Smoking timeline for a patient who quit (superseded event + current event)
show("5b. Smoking timeline: Yeo Shu Fen", db.patient_history.find(
  { nric: "S0314398H", category: "smoking" },
  { _id: 1, status: 1, effectivePeriod: 1, "details.smokingStatus": 1, "details.cigarettesPerDay": 1, "details.quitDate": 1 }
).sort({ "effectivePeriod.start": 1 }).toArray());

// 5c. Current smokers (uses the partial unique index: only active social events)
show("5c. Current smokers", db.patient_history.find(
  { eventType: "social_history", category: "smoking", status: "active", "details.smokingStatus": "current_smoker" },
  { _id: 0, nric: 1, "details.cigarettesPerDay": 1, "details.yearsSmoked": 1 }
).toArray());

// 5d. Patients with an active condition recorded outside our diagnoses (e.g. kidney disease)
show("5d. Active external conditions", db.patient_history.aggregate([
  { $match: { eventType: "condition", status: "active" } },
  { $group: { _id: "$code.display", patients: { $push: "$nric" }, count: { $sum: 1 } } },
  { $sort: { count: -1 } }
]).toArray());

// ---------------------------------------------------------------------------
// SAFETY CHECK (lab part). The interaction and allergy parts run in PostgreSQL;
// the backend passes the proposed drug's ingredient names in here.
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
            { $eq: ["$nric", nric] }, { $eq: ["$test", "$$test"] },
            { $lte: ["$collectedAt", asOf] },
            { $gte: ["$collectedAt", { $dateSubtract: { startDate: asOf, unit: "day", amount: "$$days" } }] } ] } } },
          { $sort: { collectedAt: -1 } }, { $limit: 1 } ],
        as: "latest" } },
    { $unwind: "$latest" },
    { $match: { $expr: { $cond: [ { $eq: ["$op", "gt"] },
        { $gt: ["$latest.value", "$threshold"] }, { $lt: ["$latest.value", "$threshold"] } ] } } },
    { $project: { _id: 0, ruleId: "$_id", severity: 1, test: 1, message: 1,
                  value: "$latest.value", unit: "$latest.unit", collectedAt: "$latest.collectedAt" } }
  ]).toArray();
}
show("6. Lab alerts: Foo Ai Ling, proposed LISINOPRIL", labAlerts("S0047109G", ["LISINOPRIL"]));

// ---------------------------------------------------------------------------
// PHARMACIST
// ---------------------------------------------------------------------------
// 7. Review queue: pending prescriptions, most alerts first, oldest first
show("7. Pending review queue", db.prescription_audit.aggregate([
  { $match: { status: "Pending" } },
  { $project: { nric: 1, drug: "$drug.name", prescribedBy: "$prescribedBy.name",
                prescribedAt: { $first: "$events.at" },
                majorAlerts: { $size: { $filter: { input: "$alerts", cond: { $in: ["$$this.severity", ["avoid", "severe", "major"]] } } } },
                alerts: { $size: "$alerts" } } },
  { $sort: { majorAlerts: -1, alerts: -1, prescribedAt: 1 } }
]).toArray());

// 8. Full audit trail of one prescription: alerts shown, doctor's reason, decisions
show("8. Audit trail: prescription 36", db.prescription_audit.findOne({ _id: 36 }, { alerts: 1, events: 1, status: 1 }));

// ---------------------------------------------------------------------------
// ANALYTICS (aggregation pipelines)
// ---------------------------------------------------------------------------
// 9. Which alerts fire most often
show("9. Alerts by type and effect", db.prescription_audit.aggregate([
  { $unwind: "$alerts" },
  { $group: { _id: { type: "$alerts.type", effect: { $ifNull: ["$alerts.effect", { $ifNull: ["$alerts.ruleId", "$alerts.ingredient"] }] } },
              count: { $sum: 1 }, prescriptions: { $addToSet: "$_id" } } },
  { $project: { _id: 0, type: "$_id.type", effect: "$_id.effect", count: 1, prescriptions: { $size: "$prescriptions" } } },
  { $sort: { count: -1 } }
]).toArray());

// 10. Pharmacist review turnaround: hours from prescribing to decision
show("10. Review turnaround per pharmacist", db.prescription_audit.aggregate([
  { $match: { status: { $in: ["Approved", "Rejected"] } } },
  { $project: {
      prescribed: { $first: { $filter: { input: "$events", cond: { $eq: ["$$this.action", "prescribed"] } } } },
      review: { $first: { $filter: { input: "$events", cond: { $in: ["$$this.action", ["approved", "rejected"]] } } } } } },
  { $project: { pharmacist: "$review.by.name",
                hours: { $divide: [{ $subtract: ["$review.at", "$prescribed.at"] }, 3600000] } } },
  { $group: { _id: "$pharmacist", reviews: { $sum: 1 }, avgHours: { $avg: "$hours" }, maxHours: { $max: "$hours" } } },
  { $project: { reviews: 1, avgHours: { $round: ["$avgHours", 1] }, maxHours: { $round: ["$maxHours", 1] } } },
  { $sort: { avgHours: 1 } }
]).toArray());

// 11. Alerted vs non-alerted prescriptions per doctor
show("11. Prescriptions with alerts, per doctor", db.prescription_audit.aggregate([
  { $group: { _id: "$prescribedBy.name", prescriptions: { $sum: 1 },
              withAlerts: { $sum: { $cond: [{ $gt: [{ $size: "$alerts" }, 0] }, 1, 0] } },
              rejected: { $sum: { $cond: [{ $eq: ["$status", "Rejected"] }, 1, 0] } } } },
  { $project: { prescriptions: 1, withAlerts: 1, rejected: 1,
                alertRatePct: { $round: [{ $multiply: [{ $divide: ["$withAlerts", "$prescriptions"] }, 100] }, 1] } } },
  { $sort: { alertRatePct: -1 } }
]).toArray());

// 12. Abnormal lab results in the last 90 days (uses the partial index ix_lab_abnormal)
show("12. Abnormal results, last 90 days", db.lab_results.find(
  { flag: { $in: ["H", "L"] }, collectedAt: { $gte: new Date(Date.now() - 90 * 86400000) },
    test: { $in: ["POTASSIUM", "EGFR", "INR"] } },
  { _id: 0, nric: 1, test: 1, value: 1, unit: 1, flag: 1, collectedAt: 1 }
).sort({ collectedAt: -1 }).toArray());

// ---------------------------------------------------------------------------
// WRITES your backend will do (commented out so this file stays read-only)
// ---------------------------------------------------------------------------
// A. Pharmacist approves or rejects: update PostgreSQL first, then record it here.
// db.prescription_audit.updateOne(
//   { _id: 36, status: "Pending" },
//   { $set: { status: "Rejected" },
//     $push: { events: { at: new Date(), action: "rejected",
//                        by: { role: "pharmacist", id: 2, name: "Farhana binte Aziz" },
//                        note: "Potassium 5.6; adding lisinopril to spironolactone is unsafe. Doctor contacted." } } });
//
// B. Doctor writes a new prescription (after inserting it in PostgreSQL and getting its PrescriptionID):
// db.prescription_audit.insertOne({ _id: 183, nric: "S0960703Z", diagnosisId: 2, encounterId: "E0249",
//   drug: { drugId: 131, name: "AMLODIPINE BESYLATE", ingredients: ["AMLODIPINE"], dose: "5 mg once daily, oral" },
//   prescribedBy: { doctorId: 1, name: "Dr Tan Kah Wee" }, status: "Pending", alerts: [],
//   events: [{ at: new Date(), action: "prescribed", by: { role: "doctor", id: 1, name: "Dr Tan Kah Wee" } }] });
//
// C. Nurse records triage for a new visit, doctor adds the note later:
// db.encounters.insertOne({ _id: "E0250", nric: "S0960703Z", visitDate: new Date(), visitType: "follow-up",
//   doctor: { doctorId: 1, name: "Dr Tan Kah Wee", hospital: "Harbourview Family Clinic" },
//   triage: { recordedAt: new Date(), category: "P4", chiefComplaint: "BP review", painScore: 0,
//             vitals: { systolicBP: 142, diastolicBP: 88, heartRate: 76, respRate: 16, temperatureC: 36.8, spo2: 98 } },
//   consultation: { note: "BP still above target on amlodipine.", diagnosisIds: [2] } });
//
// D. Patient stops smoking: close the old event FIRST, then add the new one.
//    (The unique index refuses a second active smoking event, so the order matters.)
// db.patient_history.updateOne(
//   { nric: "S0305186B", category: "smoking", status: "active" },
//   { $set: { status: "superseded", "effectivePeriod.end": new Date() } });
// db.patient_history.insertOne({ _id: "H0203", nric: "S0305186B", eventType: "social_history", category: "smoking",
//   status: "active", effectivePeriod: { start: new Date(), end: null }, recordedAt: new Date(),
//   recordedBy: { role: "doctor", id: 1, name: "Dr Tan Kah Wee" }, verificationStatus: "patient_reported",
//   details: { smokingStatus: "former_smoker", quitDate: new Date() }, schemaVersion: 1 });
//
// E. Add a past condition reported by the patient:
// db.patient_history.insertOne({ _id: "H0204", nric: "S0960703Z", eventType: "condition", status: "resolved",
//   effectivePeriod: { start: ISODate("2015-04-01"), end: ISODate("2015-05-01") }, recordedAt: new Date(),
//   verificationStatus: "patient_reported", code: { system: "local-condition-code", value: "GALLSTONES", display: "Gallstones" },
//   details: { notes: "Gallbladder removed at another hospital." }, schemaVersion: 1 });

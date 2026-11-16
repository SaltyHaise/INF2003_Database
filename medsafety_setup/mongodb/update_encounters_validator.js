// One-off update for a database set up before 16 Nov 2026.
// Makes vitals, triage category and pain score optional on encounters (the Consult
// form now requires only the reason for visit and a diagnosis). Existing documents
// are untouched. Re-running medsafety_setup_mongo.js does the same from scratch.
//
//   mongosh "mongodb://localhost:27017/medsafety" update_encounters_validator.js
const info = db.getCollectionInfos({ name: "encounters" })[0];
if (!info) { print("encounters not found: run medsafety_setup_mongo.js first"); quit(1); }
const v = info.options.validator;
const triage = v.$jsonSchema.properties.triage;
triage.required = ["recordedAt", "chiefComplaint"];
delete triage.properties.vitals.required;
db.runCommand({ collMod: "encounters", validator: v, validationLevel: "strict" });
print("encounters validator updated: triage requires only " + JSON.stringify(triage.required));

// One-off update for a database set up before 16 Nov 2026. Keeps all existing data.
//
//   mongosh "mongodb://localhost:27017/medsafety" update_validators.js
//
// 1. encounters: vitals, triage category and pain score become optional (the Consult page
//    requires only the reason for visit and a diagnosis).
// 2. prescription_audit: alert type "history" is allowed. The safety check has always raised
//    past-condition alerts (e.g. an NSAID for a patient with a peptic ulcer), but the
//    validator refused them, so those prescriptions never got an audit record.
// Re-running medsafety_setup_mongo.js gives the same rules from scratch.
const infos = Object.fromEntries(db.getCollectionInfos().map(i => [i.name, i]));
if (!infos.encounters || !infos.prescription_audit) { print("Run medsafety_setup_mongo.js first."); quit(1); }

const ev = infos.encounters.options.validator;
const triage = ev.$jsonSchema.properties.triage;
triage.required = ["recordedAt", "chiefComplaint"];
delete triage.properties.vitals.required;
db.runCommand({ collMod: "encounters", validator: ev, validationLevel: "strict" });
print("encounters: triage requires only " + JSON.stringify(triage.required));

const av = infos.prescription_audit.options.validator;
const schema = (av.$and || [av]).map(x => x.$jsonSchema).find(Boolean);
const types = schema.properties.alerts.items.properties.type.enum;
if (!types.includes("history")) types.push("history");
db.runCommand({ collMod: "prescription_audit", validator: av, validationLevel: "strict" });
print("prescription_audit: alert types " + JSON.stringify(types));

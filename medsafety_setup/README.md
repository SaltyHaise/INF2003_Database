# MedSafety database setup (INF2003 G61)

One script builds our whole relational database, all **15 tables from the ER diagram with data**, in **MySQL** or **PostgreSQL**. A second script builds the **5 MongoDB collections** with data that matches it. All the data is inside the scripts, so **you don't need CSV files or an Uploads folder**.

```
mysql/        medsafety_setup_mysql.sql      check_setup_mysql.sql
postgresql/   medsafety_setup_postgres.sql   check_setup_postgres.sql
mongodb/      medsafety_setup_mongo.js       check_setup_mongo.js     medsafety_examples_mongo.js
README.md     this file
```

Both SQL versions contain exactly the same data. I checked every row of all 15 tables against each other.

> ⚠️ The script **deletes and rebuilds** these tables, including the older tables from earlier versions of our scripts. Anything you added by hand will be lost.

---

## MySQL (Workbench)

**You need** MySQL Server 8.0 or newer, plus MySQL Workbench.

1. Connect to your local server in Workbench.
2. **File → Run SQL Script…**, choose `mysql/medsafety_setup_mysql.sql` and click **Run**. The script is about 2.7 MB, so use Run SQL Script rather than opening it in the editor. It creates the `medsafety` database itself and takes under a minute.
3. Open `mysql/check_setup_mysql.sql` (*File → Open SQL Script*) and run it with the **first lightning-bolt button**.
4. Every row of the result should say **PASS**.
5. To see the tables, click the refresh icon next to **SCHEMAS** in the Navigator.

## PostgreSQL (pgAdmin, DBeaver, psql, …)

**You need** PostgreSQL 12 or newer, plus any SQL tool.

1. **Create an empty database** called `medsafety`:
   - **pgAdmin:** right-click *Databases* → *Create → Database*.
   - **DBeaver:** right-click your connection → *Create → New Database*.
   - **psql:** `createdb -U postgres medsafety`
2. **Connect to that database** and run `postgresql/medsafety_setup_postgres.sql`:
   - **pgAdmin:** select `medsafety` → *Tools → Query Tool* → 📂 Open File → **F5**.
   - **DBeaver:** open the file with `medsafety` as the active database, then press **Alt+X** (*Execute SQL Script*). **Not Ctrl+Enter**, which runs only one statement.
   - **psql:** `psql -U postgres -d medsafety -f medsafety_setup_postgres.sql`
3. The last result is the CHECK table, and every row should say **PASS**. You can re-run the check any time with `check_setup_postgres.sql`.

PostgreSQL stores unquoted names in lowercase (`Patient` becomes `patient`). Write your queries **without double quotes** and they work as written, e.g. `SELECT NRIC, Name FROM Patient`.

## MongoDB (Compass or mongosh)

**You need** MongoDB Community Server 7.0 or newer, **MongoDB Shell (mongosh)** and MongoDB Compass. The server installer does **not** include mongosh: get the MSI from mongodb.com/try/download/shell, or run `winget install MongoDB.Shell`.

> Compass's built-in shell tab can't run script files (`load()` gives *"not currently implemented for this platform"*). Run the scripts from a terminal instead.

1. **Run the PostgreSQL setup first, on the same day.** Both scripts date records relative to the day they run, so run both before a demo.
2. Open a **new** Command Prompt or PowerShell, go to the `mongodb` folder and run the script. Keep the quotes if the path has spaces:
   ```
   cd "C:\path\to\medsafety_setup\mongodb"
   mongosh medsafety_setup_mongo.js
   ```
   It connects to `localhost:27017` and creates the `medsafety` database itself.
3. The last thing it prints is the CHECK list. Every row should say **PASS** (23 rows). Re-run the check any time with `mongosh check_setup_mongo.js`.
4. In Compass, connect to `mongodb://localhost:27017` and refresh to browse the `medsafety` collections. Compass's shell tab is fine for single queries: type `use medsafety` first.
5. `medsafety_examples_mongo.js` has ready-made queries (doctor's patient view, patient history, lab trends, note search, pharmacist queue, analytics) plus the inserts and updates the backend will do. It doesn't change any data, so it's safe to run.

### The 5 collections

| Collection | One document per | Holds | Links to PostgreSQL |
|---|---|---|---|
| `encounters` (249) | clinic visit | **triage** (category P1–P4, complaint, pain score, vitals) and the doctor's **consultation note** | `nric`, `consultation.diagnosisIds`, `prescriptionIds`, `doctor.doctorId` |
| `patient_history` (202) | history fact | **smoking and alcohol**, past or outside **conditions**, **procedures**, **family history**, each with an effective period and who verified it. A change (e.g. quitting smoking) closes the old event (`superseded`) and adds a new one | `nric`, `recordedBy.id` (DoctorID), `encounterId` |
| `lab_results` (458) | lab result | test, value, unit, normal range, H/L/N flag, date; INR results for warfarin patients also carry the 2.0–3.0 target range | `nric`, `orderedBy` (DoctorID) |
| `prescription_audit` (182) | prescription | the **alerts** the doctor saw (interaction, allergy, lab), the doctor's reason for going ahead, and every **decision** in an `events` list (prescribed → approved / rejected) | `_id` = `PrescriptionID`, `nric`, `diagnosisId` |
| `lab_rules` (7) | rule | "if the new drug contains X and the latest Y result is above/below Z within N days, alert" | ingredient names = `ActiveIngredients.IngredientName` |

**Validation:** each collection has a `$jsonSchema` validator (required fields, types, allowed values such as triage category and lab test codes). `patient_history` checks a different shape per `eventType` (a smoking event needs `smokingStatus`, a condition needs a `code`, family history needs a `relation`). `prescription_audit` also refuses a **Rejected** prescription that has no rejection note. Extra fields are allowed on purpose, e.g. `quitDate` only appears for former smokers.

**Indexes:** patient + date on visits and lab results, a **text index** on notes and complaints, multikey indexes on `diagnosisIds` / `prescriptionIds` / `alerts.type`, a **partial index** that covers only abnormal lab results, and a **partial unique index** that allows only one *active* smoking (and alcohol) status per patient. To record a change, set the old event to `superseded` first, then insert the new one.

### Lab alerts in the demo scenarios

The safety check now has a third part: PostgreSQL finds interactions and allergies, and MongoDB finds risky lab results (`labAlerts()` in the scripts).

| Scenario | Recent lab result | Lab alert for the new drug |
|---|---|---|
| D8 Foo Ai Ling, new lisinopril | potassium 5.6 (3 days ago) | high potassium |
| D3 Deepa d/o Raju, new ibuprofen | INR 3.6 on warfarin, eGFR 55 | INR above target + reduced kidney function |
| D5 Heng Bee Choo, new ibuprofen | eGFR 48 | reduced kidney function |
| D4 Ravi s/o Chandran | INR 2.6 (in range) | none |
| Lau Ah Seng (already on metformin) | eGFR 41 (2 days ago) | metformin + low kidney function |

### How the app uses both databases

1. **Doctor opens a patient:** PostgreSQL gives details, active medications and allergies. MongoDB gives recent visits, history (smoking, past conditions, surgery, family), latest labs and notes.
2. **Doctor prescribes:** insert into `Prescription` (Pending) in PostgreSQL and run the interaction/allergy check. Then run `labAlerts()` in MongoDB and insert the `prescription_audit` document with all alerts and the doctor's reason.
3. **Pharmacist decides:** update `ReviewStatus` in PostgreSQL **first**, then `$push` the decision onto `events` and `$set` the `status` in MongoDB. The two databases can't share a transaction, so if the MongoDB write fails, retry it. That gap is worth discussing in the SQL vs NoSQL comparison.

---

## What the check shows (27 rows)

**15 `counts` rows**, one per table:

| Drug | ActiveIngredients | Contains | SideEffects | HasSideEffects | DrugtoDrugInteraction |
|---|---|---|---|---|---|
| 2202 | 2008 | 2697 | 202 | 941 | 6684 |

| Patient | Doctor | Pharmacist | Disease | Diagnosis | PrescriptionTemplate | Prescription | IsAllergicTo | ToAddress |
|---|---|---|---|---|---|---|---|---|
| 50 | 8 | 5 | 73 | 120 | 38 | 182 | 8 | 481 |

**12 `scenario` rows**, one per demo prescription waiting for a pharmacist. Each checks that the prescription raises the expected alerts against the patient's **active** medications and allergies:

| | Scenario | Expected alert |
|---|---|---|
| D1 | On metformin, new amlodipine | none |
| D2 | Amoxicillin allergy, new amoxicillin | allergy |
| D3 | On warfarin, new ibuprofen | interaction |
| D4 | On warfarin + simvastatin, new clarithromycin | multiple interactions |
| D5 | Ibuprofen allergy, on aspirin + digoxin, new ibuprofen | allergy + interactions |
| D6 | On clopidogrel, new omeprazole | interaction |
| D7 | On nitroglycerin, new sildenafil | interaction |
| D8 | On spironolactone, new lisinopril | interaction |
| D9 | On sertraline, new tramadol | interaction |
| D10 | Clarithromycin course ended months ago, new simvastatin | none (old meds don't count) |
| D11 | No medications or allergies, new cetirizine | none |
| D12 | Carbamazepine Stevens-Johnson syndrome on record, new carbamazepine | allergy |

---

## How the tables differ from the ER diagram

These are agreed design changes. **Please update the diagram to match.**

| Table | Change | Why |
|---|---|---|
| **Drug** | One row per **generic** drug (e.g. `METFORMIN HYDROCHLORIDE`, `AMOXICILLIN; CLAVULANATE POTASSIUM`). No brand names. | Strength, route and form vary per product, so they belong in the template |
| **PrescriptionTemplate** | Extra **`dosage_form`** column (TABLET, CAPSULE, AEROSOL, METERED, …) | Tells "500 mg tablet" apart from "500 mg extended-release" |
| **HasSideEffects** | `(IngredientID, EffectID)` instead of DrugID | Side effects are known per ingredient, and combination drugs inherit them. Look them up through `Contains` |
| **DrugtoDrugInteraction** | `IngredientID_A`, `IngredientID_B` instead of DrugIDs. Key is `(A, B, Symptom)`. Extra columns `Direction`, `LabelSaysAvoid`, `ExampleSentence` | One row covers every drug containing those ingredients (drug-level would be ~32,000 duplicated rows). A pair can have several symptoms. `ExampleSentence` is the alert text |
| **Disease / ToAddress** | 73 diseases; 481 drug–disease links | Built from the Kaggle / Hugging Face indication data, with wrong and off-label links removed by hand. The separate IngredientIndication table was dropped |

**Interaction pairs are stored once, with the smaller ID first** (`IngredientID_A < IngredientID_B`). To look up a pair, use `LEAST`/`GREATEST`. The bottom of each setup script has a full example safety-check query (interactions + allergies against active medications) and a side-effect lookup.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Workbench is slow or freezes opening the setup file | Use **File → Run SQL Script** instead of opening it in the editor |
| DBeaver ran only the first statement | Use **Alt+X** (*Execute SQL Script*) |
| PostgreSQL tables went into the wrong database | Connect to `medsafety`, then run the script again |
| `relation "Patient" does not exist` (PostgreSQL) | Remove the double quotes from your query |
| `permission denied for schema public` (PostgreSQL) | Connect as the `postgres` user |
| `load is not currently implemented for this platform` | That's the Compass shell tab. Install mongosh and run `mongosh medsafety_setup_mongo.js` in a terminal |
| `'mongosh' is not recognized` | Install mongosh, then open a **new** terminal window |
| MongoDB says `Document failed validation` | The document breaks a collection's rules; the error lists the field. Check the schema at the top of `medsafety_setup_mongo.js` |
| MongoDB lab alerts missing in the demo | Re-run **both** setup scripts on the same day |
| A check row says **FAIL** | Screenshot the check result and send it to the group chat |

## Notes

- All patients, NRICs, phone numbers, doctors and hospitals are made up.
- Clinical dates are relative to the day you run the script, so "active" medications stay active. **Re-run the setup before a demo** to refresh them.
- `Prescription.ReviewStatus` takes one of three values: `Pending` (waiting for a pharmacist), `Approved` (dispensed) or `Rejected`.
- A medication counts as active when `ReviewStatus = 'Approved'` and today falls between `PrescriptionStart` and `PrescriptionEnd`. A NULL end date means it's ongoing or taken as needed.
- These scripts replace the earlier `medsafety_full_setup.sql`, `clinical_tables.sql` and the CSV-loading scripts.

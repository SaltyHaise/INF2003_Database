# MedSafe (INF2003 G61)

A Flask app that helps doctors and pharmacists check a prescription before it is dispensed. It uses **PostgreSQL** and **MongoDB** together. Screens follow `MedSafe_UI_Mockups.pdf`.

## Run it

**First**, set up both databases with `medsafety_setup` (see `SETUP_GUIDE.md` in that folder). Run both setup scripts on the same day.

Then, in a terminal inside this folder:

```
python -m venv .venv
.venv\Scripts\activate            (Mac: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            (Mac: cp .env.example .env)
```

Open `.env` and put your PostgreSQL password into `PG_DSN`. Then:

```
python run.py
```

Open **http://127.0.0.1:5000**, pick a doctor or pharmacist, and you're in.

> On every start the app runs `sql/app_objects_postgres.sql`, which creates its views, trigger and indexes. Re-running the database setup drops them; the next app start puts them back.

## Try the demo

| As | Do this | What you should see |
|---|---|---|
| Doctor (e.g. Dr Ganesh Iyer) | **Prescribe** → patient *Deepa d/o Raju*, condition *Osteoarthritis of knee* → Check medications | Ibuprofen and naproxen show 5 alerts each: warfarin interaction, high INR, low kidney function, past peptic ulcer |
| Doctor | Pick ibuprofen and try to send without a reason | Refused. Add a reason and it is sent to the pharmacy |
| Doctor | *Foo Ai Ling* → *Heart failure* → lisinopril | Spironolactone interaction plus a high-potassium lab alert |
| Pharmacist (e.g. Chong Wai Kit) | **Review queue** | Prescriptions with the most serious alerts first; the alerts the doctor saw and the doctor's reason |
| Pharmacist | Approve *Jonathan Tan Jia Le*'s carbamazepine without a note | Refused by a PostgreSQL trigger (severe allergy) |
| Either | **Drug reference** → warfarin, **Insights** | Products, interactions, side effects; analytics from both databases |

## Screens

| Screen | Who | What it does |
|---|---|---|
| Prescribe | Doctor | Choose patient and condition; see drugs for that condition each checked against the patient; choose a dose template or write one; give a reason if there are alerts; send to the pharmacy |
| Patient records | Doctor (edit), pharmacist (view) | Details, allergies, active and past prescriptions, diagnoses, medical history, visits with triage and notes, lab results |
| Review queue | Pharmacist | Pending prescriptions, alerts first; three checks; approve and dispense, or return to the doctor with a note |
| Dispensed | Pharmacist | Recently approved and returned prescriptions |
| Drug reference | Both | Look up an ingredient or drug: products, recorded interactions, side effects |
| Insights | Both | Alert statistics, review turnaround, alert rate per doctor, most prescribed drugs, patients on many medicines, interacting medicines taken right now |

## Which database does what

| Data | Database | Why |
|---|---|---|
| Patients, doctors, pharmacists, diseases, diagnoses, prescriptions, dose templates | PostgreSQL | Fixed structure, foreign keys, the prescription status must be consistent |
| Drugs, ingredients, interactions, allergies, side effects | PostgreSQL | Joins and the interaction lookup (`LEAST`/`GREATEST` on ingredient pairs) |
| Visits: triage + consultation notes (`encounters`) | MongoDB | Free text with a text index; vitals vary |
| Medical history events (`patient_history`) | MongoDB | Different shape per event type; changes over time are kept as new events |
| Lab results (`lab_results`) and lab rules (`lab_rules`) | MongoDB | Time series per patient; rules joined with `$lookup` |
| Prescription audit trail (`prescription_audit`) | MongoDB | One document per prescription with nested alerts and an events list; aggregation for analytics |

## The safety check (`medsafe/safety.py`)

For a patient and the candidate drugs it finds, against the patient's **active** medications (`v_active_medications`):

| Alert | Source |
|---|---|
| Interaction | PostgreSQL: candidate ingredient × active-medication ingredient in `DrugtoDrugInteraction` |
| Allergy | PostgreSQL: `IsAllergicTo` on the candidate's ingredients |
| Duplicate | PostgreSQL: the patient already takes that ingredient |
| Lab | MongoDB: aggregation over `lab_rules` with `$lookup` of the latest matching `lab_results` |
| History | MongoDB `patient_history` (and PostgreSQL diagnoses) matched against `HISTORY_RULES` |

The check runs again on the server when the doctor sends the prescription, and the alerts are saved in the audit document so the pharmacist sees exactly what the doctor saw.

## Writing to two databases

Sending a prescription or making a review decision writes **PostgreSQL first** (in one transaction), then **MongoDB**. There is no transaction across the two databases. If the MongoDB write fails, it is saved in `instance/mongo_outbox.jsonl` and retried on the next request (`medsafe/outbox.py`). PostgreSQL stays the source of truth for prescription status. This is a good point for the SQL vs NoSQL comparison in the report.

## Database features used (for the report)

**PostgreSQL** (`sql/app_objects_postgres.sql` and the queries in `medsafe/`)
- Views: `v_prescription_detail`, `v_active_medications`, `v_pending_reviews`
- Trigger `trg_check_review`: reviewed prescriptions need a pharmacist and time; returning needs a note; approving a drug the patient is severely allergic to needs a note
- `pg_trgm` GIN indexes for search that matches text anywhere in a name, ranked by `similarity()`
- Partial indexes on approved and pending prescriptions
- CTEs, `LEAST`/`GREATEST` pair lookup, `array_agg`, `bool_or`, `string_agg`, window function `rank()`, `HAVING` with a subquery, self-join
- `ON CONFLICT ... DO UPDATE` for allergies; foreign-key errors handled (a diagnosis with prescriptions can't be removed)
- Optimistic concurrency: a review only updates a row that is still `Pending`

**MongoDB**
- `$jsonSchema` validation on every collection (per event type in `patient_history`)
- Partial unique index: one active smoking / alcohol status per patient
- Text index search on consultation notes
- Aggregation: `$lookup` with a pipeline, `$unwind`, `$group`, `$facet`-style grouping, `$filter`, `$dateSubtract`, `$cond`
- Embedded arrays (`alerts`, `events`), `$push`, `$addToSet`, soft delete (`entered_in_error`)

**App**
- Role-based access (doctor / pharmacist) on every route; CSRF token on every form
- All SQL uses parameters (no string-built queries from user input)

## CRUD map

| | Create | Read | Update | Delete |
|---|---|---|---|---|
| Patient (PG) | New patient | Records, search | Edit details | (not offered: patients have history) |
| Allergy (PG) | Edit allergies | Record, safety check | Same ingredient again updates it | Remove |
| Diagnosis (PG) | Add diagnosis, or new on prescribing | Record | | Remove if no prescriptions |
| Prescription + template (PG) | Prescribe | Everywhere | Review decision | |
| History event (Mongo) | Add history event | Record | Supersede on status change | Mark entered in error |
| Visit (Mongo) | Add visit | Record, note search | Prescription linked on prescribing | |
| Audit (Mongo) | On prescribing | Review queue, Insights | `$push` decision | |

## Project layout

```
run.py                     start the app
medsafe/__init__.py        app factory, CSRF, outbox retry
medsafe/db.py              PostgreSQL + MongoDB connections, runs the startup SQL
medsafe/auth.py            sign-in and role checks
medsafe/safety.py          the safety check
medsafe/records.py         reading a patient's record from both databases
medsafe/doctor.py          Prescribe
medsafe/patients.py        Patient records and their create/update actions
medsafe/pharmacist.py      Review queue, Dispensed
medsafe/reference.py       Drug reference
medsafe/insights.py        Insights
medsafe/api.py             search-as-you-type endpoints
medsafe/outbox.py          retry queue for MongoDB writes
sql/app_objects_postgres.sql   views, trigger, indexes
templates/, static/        pages, CSS, small JavaScript
```

## Limits

- Sign-in is a demo picker, not real authentication.
- "Other clinic" medications from the mockup aren't stored yet (no table for patient-reported medicines).
- Drug-disease links and interactions come from public datasets with known errors; alerts are decision support, not advice.
- All patients are synthetic.

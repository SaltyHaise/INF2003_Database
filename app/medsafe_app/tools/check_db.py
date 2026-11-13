"""Evidence for the report: ACID properties and query performance.

Run from app/medsafe_app with the app's virtual environment:

    python tools/check_db.py acid                 A atomicity, B consistency, C isolation, E limits
    python tools/check_db.py durability-prepare   D part 1: commits 1000 rows, holds 500 uncommitted
    python tools/check_db.py durability-verify    D part 2: run after killing and restarting both servers
    python tools/check_db.py timing               query timing with and without indexes

Nothing here changes the demo data for good. Tests run in transactions that are rolled
back, or on throwaway objects (durability_test). The isolation test has to commit one
change; it puts the row back exactly as it was and checks that it did.

Results are also saved as JSON in instance/db_evidence/ (not in git).
"""
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError, WriteError
from pymongo.write_concern import WriteConcern

APP_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = APP_DIR / "instance" / "db_evidence"
load_dotenv(APP_DIR / ".env")
PG_DSN = os.environ["PG_DSN"]
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "medsafety")

TEST_ID = 999_999_001          # explicit IDs for test rows, so no sequence value is used up
RUNS = 5

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------
def pg(**kw):
    return psycopg.connect(PG_DSN, row_factory=dict_row, **kw)


def mongo_client():
    return MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)


def head(title):
    print(f"\n{'=' * 88}\n{title}\n{'=' * 88}")


def verdict(ok):
    return "PASS" if ok else "FAIL"


def pg_error(e):
    """The parts of a PostgreSQL error worth quoting."""
    d = e.diag
    return {"sqlstate": d.sqlstate, "class": type(e).__name__, "message": d.message_primary,
            "detail": d.message_detail, "constraint": d.constraint_name}


def show_error(err):
    print(f"    SQLSTATE {err['sqlstate']} ({err['class']})")
    print(f"    ERROR:  {err['message']}")
    if err["detail"]:
        print(f"    DETAIL: {err['detail']}")


def save(name, data):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"\n[saved {path}]")


def load(name):
    return json.loads((OUT_DIR / f"{name}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------
# A. Atomicity
# ---------------------------------------------------------------------------------------
def atomicity():
    head("A. ATOMICITY (PostgreSQL): a failed statement undoes the whole transaction")
    res = {}
    with pg() as conn:
        ref = conn.execute("""SELECT (SELECT NRIC FROM Patient ORDER BY NRIC LIMIT 1) AS nric,
                                     (SELECT DoctorID FROM Doctor ORDER BY 1 LIMIT 1) AS doctor,
                                     (SELECT DiseaseID FROM Disease ORDER BY 1 LIMIT 1) AS disease,
                                     (SELECT PrescriptionTemplateID FROM PrescriptionTemplate ORDER BY 1 LIMIT 1) AS tpl""").fetchone()
        before = conn.execute("SELECT count(*) AS n FROM Diagnosis").fetchone()["n"]
        rx_before = conn.execute("SELECT count(*) AS n FROM Prescription").fetchone()["n"]
        conn.rollback()
        print(f"  Diagnosis rows before: {before}    Prescription rows before: {rx_before}")
        try:
            conn.execute("""INSERT INTO Diagnosis (DiagnosisID, NRIC, DoctorID, DiseaseID, DateOfDiagnosis)
                            VALUES (%s, %s, %s, %s, CURRENT_DATE)""", (TEST_ID, ref["nric"], ref["doctor"], ref["disease"]))
            inside = conn.execute("SELECT count(*) AS n FROM Diagnosis").fetchone()["n"]
            print(f"  1. INSERT Diagnosis {TEST_ID}: ok. Rows seen inside the transaction: {inside}")
            print("  2. INSERT Prescription with end date before start date (breaks ck_rx_dates) ...")
            conn.execute("""INSERT INTO Prescription (PrescriptionID, DiagnosisID, PrescriptionTemplateID,
                                                      PrescriptionStart, PrescriptionEnd, ReviewStatus)
                            VALUES (%s, %s, %s, CURRENT_DATE, CURRENT_DATE - 7, 'Pending')""", (TEST_ID, TEST_ID, ref["tpl"]))
            conn.rollback()
            res["error"] = None
            print("    !! the bad prescription was accepted")
        except psycopg.Error as e:
            res["error"] = pg_error(e)
            show_error(res["error"])
            try:
                conn.execute("SELECT 1")
            except psycopg.Error as e2:
                res["after_error"] = pg_error(e2)
                print(f"  3. Next statement in the same transaction: {res['after_error']['message']}")
            conn.rollback()
        after = conn.execute("SELECT count(*) AS n FROM Diagnosis").fetchone()["n"]
        rx_after = conn.execute("SELECT count(*) AS n FROM Prescription").fetchone()["n"]
        left = conn.execute("SELECT count(*) AS n FROM Diagnosis WHERE DiagnosisID = %s", (TEST_ID,)).fetchone()["n"]
    res.update(diagnosis_before=before, diagnosis_inside=locals().get("inside"), diagnosis_after=after,
               prescription_before=rx_before, prescription_after=rx_after, test_row_left=left)
    res["pass"] = bool(res["error"]) and res["error"]["constraint"] == "ck_rx_dates" \
        and before == after and rx_before == rx_after and left == 0
    print(f"  Diagnosis rows after: {after}    Prescription rows after: {rx_after}    test diagnosis still there: {left}")
    print(f"  => {verdict(res['pass'])}: the diagnosis insert was undone together with the failed prescription")
    return res


# ---------------------------------------------------------------------------------------
# B. Consistency
# ---------------------------------------------------------------------------------------
def consistency():
    head("B. CONSISTENCY: rules the databases enforce themselves")
    res = {"postgres": [], "mongo": {}}
    with pg() as conn:
        rx = conn.execute("""SELECT PrescriptionID FROM Prescription WHERE ReviewStatus = 'Pending'
                             ORDER BY PrescriptionID LIMIT 1""").fetchone()["prescriptionid"]
        ref = conn.execute("""SELECT (SELECT PharmacistID FROM Pharmacist ORDER BY 1 LIMIT 1) AS ph,
                                     (SELECT DoctorID FROM Doctor ORDER BY 1 LIMIT 1) AS doctor,
                                     (SELECT DiseaseID FROM Disease ORDER BY 1 LIMIT 1) AS disease""").fetchone()
        bad_nric = "S0000000X"
        assert conn.execute("SELECT count(*) AS n FROM Patient WHERE NRIC = %s", (bad_nric,)).fetchone()["n"] == 0
        conn.rollback()
        tests = [
            ("Trigger trg_check_review: return a prescription without a note", "trigger",
             "UPDATE Prescription SET ReviewStatus = 'Rejected', PharmacistID = %s, ReviewedAt = LOCALTIMESTAMP(0), "
             "ReviewNotes = NULL WHERE PrescriptionID = %s", (ref["ph"], rx)),
            ("CHECK ck_review_status: a status that isn't Pending / Approved / Rejected", "ck_review_status",
             "UPDATE Prescription SET ReviewStatus = 'Dispensed', PharmacistID = %s, ReviewedAt = LOCALTIMESTAMP(0), "
             "ReviewNotes = 'test' WHERE PrescriptionID = %s", (ref["ph"], rx)),
            ("FOREIGN KEY diagnosis_nric_fkey: a diagnosis for a patient who doesn't exist", "diagnosis_nric_fkey",
             "INSERT INTO Diagnosis (DiagnosisID, NRIC, DoctorID, DiseaseID, DateOfDiagnosis) "
             "VALUES (%s, %s, %s, %s, CURRENT_DATE)", (TEST_ID, bad_nric, ref["doctor"], ref["disease"])),
        ]
        for title, expect, sql, params in tests:
            print(f"\n  {title}")
            print(f"    {sql}   {params}")
            item = {"title": title, "sql": sql, "params": list(params), "error": None}
            try:
                conn.execute(sql, params)
                print("    !! accepted")
            except psycopg.Error as e:
                item["error"] = pg_error(e)
                show_error(item["error"])
            finally:
                conn.rollback()
            err = item["error"]
            item["pass"] = bool(err) and (err["constraint"] == expect if expect != "trigger"
                                          else err["sqlstate"] == "23514" and "needs a note" in err["message"])
            print(f"    => {verdict(item['pass'])} (rolled back)")
            res["postgres"].append(item)
        row = conn.execute("SELECT ReviewStatus FROM Prescription WHERE PrescriptionID = %s", (rx,)).fetchone()
        res["prescription_used"] = rx
        res["status_after"] = row["reviewstatus"]
        print(f"\n  Prescription {rx} is still '{row['reviewstatus']}'.")

    print("\n  MongoDB $jsonSchema validator on encounters: a visit with required fields missing")
    client = mongo_client()
    db = client[MONGO_DB]
    nric = db.encounters.find_one({}, {"nric": 1})["nric"]
    doc = {"_id": "E9999999", "nric": nric, "visitType": "follow-up"}
    before = db.encounters.count_documents({})
    m = {"document": doc, "count_before": before, "error": None}
    print(f"    db.encounters.insertOne({json.dumps(doc)})")
    try:
        db.encounters.insert_one(doc)
        db.encounters.delete_one({"_id": doc["_id"]})
        print("    !! accepted (removed again)")
    except WriteError as e:
        info = (e.details or {}).get("errInfo", {})
        m["error"] = {"code": e.code, "message": (e.details or {}).get("errmsg", str(e)), "errInfo": info}
        print(f"    WriteError code {e.code}: {m['error']['message']}")
        for rule in info.get("details", {}).get("schemaRulesNotSatisfied", []):
            print(f"    validator: {json.dumps(rule, default=str)}")
            if rule.get("operatorName") == "required":
                m["missing"] = rule.get("missingProperties")
    m["count_after"] = db.encounters.count_documents({})
    m["pass"] = bool(m["error"]) and m["error"]["code"] == 121 and m["count_after"] == before
    print(f"    encounters before: {before}, after: {m['count_after']}")
    print(f"    => {verdict(m['pass'])}")
    res["mongo"] = m
    res["pass"] = all(t["pass"] for t in res["postgres"]) and m["pass"]
    client.close()
    return res


# ---------------------------------------------------------------------------------------
# C. Isolation
# ---------------------------------------------------------------------------------------
REVIEW_SQL = """UPDATE Prescription SET ReviewStatus = %s, PharmacistID = %s, ReviewedAt = LOCALTIMESTAMP(0), ReviewNotes = %s
                WHERE PrescriptionID = %s AND ReviewStatus = 'Pending'"""   # same statement as medsafe/pharmacist.py


def isolation():
    head("C. ISOLATION (PostgreSQL): two pharmacists approve the same prescription")
    res = {"sql": " ".join(REVIEW_SQL.split())}
    c1, c2, watch = pg(), pg(), pg(autocommit=True)
    try:
        # A pending prescription for a patient without a severe allergy, and two pharmacists.
        rx = watch.execute("""SELECT r.PrescriptionID FROM Prescription r JOIN Diagnosis g ON g.DiagnosisID = r.DiagnosisID
                              WHERE r.ReviewStatus = 'Pending'
                                AND NOT EXISTS (SELECT 1 FROM IsAllergicTo a WHERE a.NRIC = g.NRIC AND a.Severity = 'Severe')
                              ORDER BY r.PrescriptionID LIMIT 1""").fetchone()["prescriptionid"]
        ph = [r["pharmacistid"] for r in watch.execute("SELECT PharmacistID FROM Pharmacist ORDER BY 1 LIMIT 2")]
        original = watch.execute("SELECT * FROM Prescription WHERE PrescriptionID = %s", (rx,)).fetchone()
        res.update(prescription=rx, pharmacists=ph, original=original,
                   isolation_level=watch.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"])
        print(f"  Prescription {rx} is '{original['reviewstatus']}'. Isolation level: {res['isolation_level']}")
        print(f"  Both run:  {res['sql']}")

        cur1 = c1.execute(REVIEW_SQL, ("Approved", ph[0], "isolation test", rx))
        res["conn1_rowcount"] = cur1.rowcount
        print(f"\n  Pharmacist {ph[0]} (connection 1): UPDATE -> rowcount {cur1.rowcount}. Transaction left open.")

        seen = watch.execute("SELECT ReviewStatus FROM Prescription WHERE PrescriptionID = %s", (rx,)).fetchone()["reviewstatus"]
        res["third_reader_sees"] = seen
        print(f"  A third connection reads the row now: '{seen}' (no dirty read of the uncommitted approval)")

        out = {}

        def second():
            t0 = time.perf_counter()
            try:
                cur = c2.execute(REVIEW_SQL, ("Approved", ph[1], "isolation test", rx))
                out["rowcount"] = cur.rowcount
            except psycopg.Error as e:
                out["error"] = pg_error(e)
            out["seconds"] = time.perf_counter() - t0

        pid2 = c2.info.backend_pid
        t = threading.Thread(target=second)
        t.start()
        print(f"  Pharmacist {ph[1]} (connection 2, backend pid {pid2}): same UPDATE sent ...")
        wait = None
        for _ in range(50):
            wait = watch.execute("""SELECT state, wait_event_type, wait_event, pg_blocking_pids(pid) AS blocked_by
                                    FROM pg_stat_activity WHERE pid = %s""", (pid2,)).fetchone()
            if wait and wait["wait_event_type"] == "Lock":
                break
            time.sleep(0.1)
        time.sleep(2.0)
        res["conn2_blocked"] = t.is_alive()
        res["conn2_wait"] = wait
        res["conn1_pid"] = c1.info.backend_pid
        print(f"    still waiting after 2 s: {t.is_alive()}")
        print(f"    pg_stat_activity: state={wait['state']}, wait_event_type={wait['wait_event_type']}, "
              f"wait_event={wait['wait_event']}, blocked by pid {wait['blocked_by']} (connection 1 is pid {c1.info.backend_pid})")

        c1.commit()
        print("  Connection 1: COMMIT")
        t.join(10)
        res["conn2_rowcount"] = out.get("rowcount")
        res["conn2_error"] = out.get("error")
        res["conn2_waited_seconds"] = round(out.get("seconds", 0), 3)
        print(f"  Connection 2: UPDATE returned after {res['conn2_waited_seconds']} s -> rowcount {out.get('rowcount')}")
        c2.rollback()
        final = watch.execute("SELECT ReviewStatus, PharmacistID FROM Prescription WHERE PrescriptionID = %s", (rx,)).fetchone()
        res["after_commit"] = final
        print(f"  Row now: status '{final['reviewstatus']}', pharmacist {final['pharmacistid']} (only the first approval counted)")
        res["pass"] = (cur1.rowcount == 1 and res["conn2_blocked"] and out.get("rowcount") == 0
                       and seen == "Pending" and final["pharmacistid"] == ph[0])
    finally:
        # Put the prescription back exactly as it was.
        c1.rollback()
        c1.execute("""UPDATE Prescription SET ReviewStatus = %(reviewstatus)s, PharmacistID = %(pharmacistid)s,
                             ReviewedAt = %(reviewedat)s, ReviewNotes = %(reviewnotes)s
                      WHERE PrescriptionID = %(prescriptionid)s""", original)
        c1.commit()
        restored = watch.execute("SELECT * FROM Prescription WHERE PrescriptionID = %s", (rx,)).fetchone()
        res["restored"] = restored == original
        print(f"\n  Undo: prescription {rx} set back to '{restored['reviewstatus']}'. Row identical to before the test: {res['restored']}")
        for c in (c1, c2, watch):
            c.close()
    res["pass"] = res.get("pass", False) and res["restored"]
    print(f"  => {verdict(res['pass'])}: the second pharmacist waited, then updated 0 rows")
    return res


# ---------------------------------------------------------------------------------------
# E. Limits
# ---------------------------------------------------------------------------------------
def limits():
    head("E. LIMITS (what the app does not guarantee)")
    res = {}
    client = mongo_client()
    db = client[MONGO_DB]
    hello = client.admin.command("hello")
    res["mongo_replica_set"] = hello.get("setName")
    print(f"  MongoDB replica set name: {hello.get('setName')} (None = standalone)")
    print("  Trying a multi-document transaction on the standalone server (throwaway collection acid_txn_test):")
    try:
        with client.start_session() as s:
            with s.start_transaction():
                db.acid_txn_test.insert_one({"n": 1}, session=s)
                db.acid_txn_test.insert_one({"n": 2}, session=s)
        res["mongo_txn_error"] = None
        print("    !! the transaction worked")
    except PyMongoError as e:
        res["mongo_txn_error"] = {"class": type(e).__name__, "code": getattr(e, "code", None), "message": str(e).split(", full error")[0]}
        print(f"    {type(e).__name__} code {res['mongo_txn_error']['code']}: {res['mongo_txn_error']['message']}")
    db.acid_txn_test.drop()
    client.close()
    notes = [
        "PostgreSQL and MongoDB writes are not one transaction. Prescribing and reviewing commit in PostgreSQL first, "
        "then write the audit document in MongoDB. If MongoDB fails, medsafe/outbox.py saves the write in "
        "instance/mongo_outbox.jsonl and retries on the next request: eventual consistency, not atomicity.",
        "MongoDB runs standalone, so it has no multi-document transactions. Each single-document write is atomic; "
        "the audit insert and the encounters update after prescribing are two separate writes.",
        "The safety check in medsafe/doctor.py reads before the prescribing transaction starts, and reads only "
        "approved, active medications. Two doctors prescribing interacting drugs for the same patient at the same "
        "time would both pass the check (write skew). The pharmacist's review is the only later check.",
    ]
    for i, n in enumerate(notes, 1):
        print(f"\n  {i}. {n}")
    res["notes"] = notes
    return res


# ---------------------------------------------------------------------------------------
# D. Durability
# ---------------------------------------------------------------------------------------
def durability_settings(conn, client):
    pgs = {r["name"]: r["setting"] for r in conn.execute(
        """SELECT name, setting FROM pg_settings WHERE name IN
           ('fsync','synchronous_commit','full_page_writes','wal_level','data_directory',
            'logging_collector','log_directory','log_filename')""")}
    pgs["version"] = conn.execute("SHOW server_version").fetchone()["server_version"]
    status = client.admin.command("serverStatus")
    opts = client.admin.command("getCmdLineOpts")["parsed"]
    mg = {"version": status["version"], "storageEngine": status["storageEngine"]["name"],
          "persistent": status["storageEngine"]["persistent"],
          "dbPath": opts.get("storage", {}).get("dbPath"),
          "journal": opts.get("storage", {}).get("journal", "default (always on for WiredTiger)"),
          "logPath": opts.get("systemLog", {}).get("path"), "pid": status["pid"],
          "startedAt": status["localTime"] - timedelta(seconds=status["uptime"])}
    return pgs, mg


def durability_prepare():
    head("D. DURABILITY, part 1: write, commit, and leave a transaction open")
    client = mongo_client()
    db = client[MONGO_DB]
    c1 = pg()
    pgs, mg = durability_settings(c1, client)
    c1.rollback()
    print("  PostgreSQL settings:")
    for k in ("fsync", "synchronous_commit", "full_page_writes", "wal_level", "data_directory"):
        print(f"    {k:22} {pgs[k]}")
    print("  MongoDB settings:")
    for k in ("storageEngine", "persistent", "journal", "dbPath"):
        print(f"    {k:22} {mg[k]}")

    c1.execute("DROP TABLE IF EXISTS durability_test")
    c1.execute("CREATE TABLE durability_test (id int PRIMARY KEY, note text NOT NULL, written_at timestamptz NOT NULL DEFAULT now())")
    c1.execute("INSERT INTO durability_test (id, note) SELECT g, 'committed' FROM generate_series(1, 1000) g")
    c1.commit()
    committed = c1.execute("SELECT count(*) AS n FROM durability_test").fetchone()["n"]
    lsn = c1.execute("SELECT pg_current_wal_lsn()::text AS l").fetchone()["l"]
    c1.rollback()
    print(f"\n  PostgreSQL: table durability_test created, {committed} rows inserted and COMMITTED (WAL position {lsn})")

    c2 = pg()
    c2.execute("INSERT INTO durability_test (id, note) SELECT g, 'not committed' FROM generate_series(1001, 1500) g")
    inside = c2.execute("SELECT count(*) AS n FROM durability_test").fetchone()["n"]
    info = c2.execute("SELECT pg_postmaster_start_time() AS started, now() AS now").fetchone()
    print(f"  PostgreSQL: second transaction inserted 500 more rows and did NOT commit. It sees {inside} rows.")

    coll = db.get_collection("durability_test", write_concern=WriteConcern(w=1, j=True))
    coll.drop()
    t0 = time.perf_counter()
    for i in range(1, 1001):
        coll.insert_one({"_id": i, "note": "journaled"})
    secs = time.perf_counter() - t0
    mcount = coll.count_documents({})
    print(f"  MongoDB: collection durability_test, {mcount} documents inserted one by one with "
          f"writeConcern {{w: 1, j: true}} ({secs:.2f} s)")

    state = {"prepared_at": datetime.now().astimezone().isoformat(), "pg_now": info["now"].isoformat(),
             "pg_started_before": info["started"].isoformat(), "pg_settings": pgs, "pg_committed": committed,
             "pg_seen_by_open_txn": inside, "pg_wal_lsn": lsn,
             "mongo_settings": mg, "mongo_count": mcount, "mongo_insert_seconds": round(secs, 3)}
    save("durability_prepare", state)
    print(f"\n  PostgreSQL server started at {info['started']}; time now {info['now']}")
    print("\n  READY. The uncommitted transaction stays open until the server is killed.")
    print("  In an ADMINISTRATOR Command Prompt run:")
    print("      taskkill /F /IM postgres.exe\n      taskkill /F /IM mongod.exe")
    print("      net start postgresql-x64-18\n      net start MongoDB")
    print("  Then run:  python tools/check_db.py durability-verify")
    sys.stdout.flush()
    try:
        while True:                       # hold the transaction open until the connection dies
            time.sleep(1)
            c2.execute("SELECT 1")
    except psycopg.Error as e:
        print(f"\n  Connection holding the uncommitted rows was lost: {str(e).strip().splitlines()[0]}")
    except KeyboardInterrupt:
        print("\n  Stopped by hand (the open transaction is rolled back).")


def durability_verify():
    head("D. DURABILITY, part 2: after the crash and restart")
    prep = load("durability_prepare")
    res = {"prepare": prep}
    client = mongo_client()
    db = client[MONGO_DB]
    with pg() as conn:
        row = conn.execute("""SELECT pg_postmaster_start_time() AS started, now() AS now,
                                     (SELECT count(*) FROM durability_test) AS n,
                                     (SELECT count(*) FROM durability_test WHERE note = 'not committed') AS uncommitted,
                                     (SELECT min(id) FROM durability_test) AS lo, (SELECT max(id) FROM durability_test) AS hi""").fetchone()
        pgs, mg = durability_settings(conn, client)
        restarted = row["started"] > datetime.fromisoformat(prep["pg_now"])
        res["postgres"] = {"started_before": prep["pg_started_before"], "prepared_at": prep["pg_now"],
                           "started_after": row["started"].isoformat(), "restarted": restarted,
                           "count": row["n"], "uncommitted_rows": row["uncommitted"], "min_id": row["lo"], "max_id": row["hi"]}
        print(f"  PostgreSQL started before the test: {prep['pg_started_before']}")
        print(f"  Rows written at:                    {prep['pg_now']}")
        print(f"  pg_postmaster_start_time() now:     {row['started']}   restarted after the writes: {restarted}")
        print(f"  durability_test rows: {row['n']} (ids {row['lo']} to {row['hi']}), "
              f"rows from the uncommitted transaction: {row['uncommitted']}")
        pg_ok = restarted and row["n"] == 1000 and row["uncommitted"] == 0
        res["postgres"]["pass"] = pg_ok
        print(f"  => {verdict(pg_ok)}: 1000 committed rows survived, 500 uncommitted rows are gone (not 1500)")

        mcount = db.durability_test.count_documents({})
        m_restarted = mg["pid"] != prep["mongo_settings"]["pid"] and \
            mg["startedAt"].astimezone() > datetime.fromisoformat(prep["prepared_at"]) - timedelta(seconds=5)
        res["mongo"] = {"pid_before": prep["mongo_settings"]["pid"], "pid_after": mg["pid"],
                        "started_after": mg["startedAt"].isoformat(), "restarted": m_restarted, "count": mcount}
        print(f"\n  MongoDB process id before: {prep['mongo_settings']['pid']}, now: {mg['pid']}   restarted: {m_restarted}")
        print(f"  durability_test documents: {mcount}")
        m_ok = m_restarted and mcount == 1000
        res["mongo"]["pass"] = m_ok
        print(f"  => {verdict(m_ok)}: all 1000 journaled documents survived")

        conn.execute("DROP TABLE durability_test")
        conn.commit()
        db.durability_test.drop()
        gone = conn.execute("SELECT to_regclass('durability_test') IS NULL AS gone").fetchone()["gone"]
        res["cleaned_up"] = gone and "durability_test" not in db.list_collection_names()
        print(f"\n  Throwaway table and collection dropped: {res['cleaned_up']}")

    log_dir = Path(pgs["data_directory"]) / pgs["log_directory"]
    res["pg_log_dir"] = str(log_dir)
    res["mongo_log"] = mg["logPath"]
    print(f"\n  PostgreSQL log folder: {log_dir}")
    try:
        newest = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)[-2:]
        res["pg_log_files"] = [str(p) for p in newest]
        lines = []
        for p in newest:
            print(f"    {p.name}")
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if any(k in line for k in ("was interrupted", "not properly shut down", "automatic recovery",
                                           "redo starts", "redo done", "invalid record length",
                                           "checkpoint starting: end-of-recovery", "ready to accept connections",
                                           "terminated by exception", "terminating any other active")):
                    lines.append(line)
        res["pg_log_lines"] = lines[-14:]
        for line in res["pg_log_lines"]:
            print(f"      {line}")
    except OSError as e:
        res["pg_log_error"] = str(e)
        print(f"    (can't read it from here: {e}. Open the newest .log file as administrator.)")
    print(f"  MongoDB log: {mg['logPath']}")
    res["pass"] = pg_ok and m_ok and res["cleaned_up"]
    save("durability_verify", res)
    client.close()


# ---------------------------------------------------------------------------------------
# Step 2: query timing
# ---------------------------------------------------------------------------------------
# The queries the app runs when a doctor prescribes (copied from medsafe/records.py and
# medsafe/safety.py; %(nric)s is the patient and %(ids)s the drug being prescribed).
PG_QUERIES = [
    ("Active medications", "records.active_meds",
     "SELECT * FROM v_active_medications WHERE NRIC = %(nric)s ORDER BY PrescriptionStart DESC"),
    ("Drug-interaction check", "safety.check",
     """WITH cand AS (SELECT DrugID, IngredientID FROM Contains WHERE DrugID = ANY(%(ids)s)),
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
ORDER BY x.LabelSaysAvoid DESC, act.GenericName, x.Symptom"""),
    ("Allergy check", "safety.check",
     """SELECT c.DrugID, ai.IngredientName, al.Severity, al.Reactions, al.Date
FROM Contains c
JOIN IsAllergicTo al ON al.IngredientID = c.IngredientID AND al.NRIC = %(nric)s
JOIN ActiveIngredients ai ON ai.IngredientID = c.IngredientID
WHERE c.DrugID = ANY(%(ids)s)"""),
]
# Every table those queries read. "Without indexes" removes every index on these tables.
PG_TABLES = ["prescription", "diagnosis", "patient", "doctor", "disease", "prescriptiontemplate", "drug",
             "pharmacist", "contains", "drugtodruginteraction", "activeingredients", "isallergicto"]
# (patient name, drug being prescribed): three scenarios from the demo script.
PG_CASES = [("Deepa d/o Raju", "IBUPROFEN"), ("Lau Ah Seng", "IBUPROFEN"), ("Jonathan Tan Jia Le", "CARBAMAZEPINE")]


def walk(node):
    yield node
    for child in node.get("Plans", []):
        yield from walk(child)


def summarise(plan):
    """Scan nodes, rows read by them, and buffers from one EXPLAIN (FORMAT JSON) result."""
    root = plan["Plan"]
    scans, examined = [], 0
    for n in walk(root):
        if "Scan" not in n["Node Type"] or n["Node Type"] in ("CTE Scan", "Subquery Scan"):
            continue
        loops = n.get("Actual Loops", 1)
        if n["Node Type"] != "Bitmap Index Scan":      # its rows are counted again by the heap scan
            # "Actual Rows" is the average per loop, so multiply by the loops.
            examined += round((n.get("Actual Rows", 0) + n.get("Rows Removed by Filter", 0)
                               + n.get("Rows Removed by Index Recheck", 0)) * loops)
        scans.append({"type": n["Node Type"], "table": n.get("Relation Name"), "index": n.get("Index Name"), "loops": loops})
    counts = {}
    for s in scans:
        counts[s["type"]] = counts.get(s["type"], 0) + 1
    return {"execution_ms": plan["Execution Time"], "planning_ms": plan["Planning Time"],
            "scans": scans, "scan_counts": counts, "rows_examined": examined,
            "rows_returned": round(root.get("Actual Rows", 0)),
            "buffers": root.get("Shared Hit Blocks", 0) + root.get("Shared Read Blocks", 0),
            "indexes_used": sorted({s["index"] for s in scans if s["index"]}),
            "seq_scans": sorted({s["table"] for s in scans if s["type"] == "Seq Scan"})}


def run_pg(conn, sql, params):
    """RUNS times EXPLAIN (ANALYZE, BUFFERS); returns the stats and one text plan."""
    cur = psycopg.ClientCursor(conn)
    runs = []
    for _ in range(RUNS):
        cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        runs.append(summarise(cur.fetchone()["QUERY PLAN"][0]))
    cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
    text = "\n".join(r["QUERY PLAN"] for r in cur.fetchall())
    last = runs[-1]
    return {"execution_ms_runs": [r["execution_ms"] for r in runs],
            "execution_ms_median": statistics.median(r["execution_ms"] for r in runs),
            "planning_ms_median": statistics.median(r["planning_ms"] for r in runs),
            "rows_examined": last["rows_examined"], "rows_returned": last["rows_returned"], "buffers": last["buffers"],
            "scan_counts": last["scan_counts"], "indexes_used": last["indexes_used"], "seq_scans": last["seq_scans"],
            "plan_text": text}


def timing_pg():
    head("STEP 2a. PostgreSQL query timing: EXPLAIN (ANALYZE, BUFFERS), with and without indexes")
    res = {"runs": RUNS, "queries": []}
    with pg() as conn:
        conn.execute("SET lock_timeout = '5s'")
        res["version"] = conn.execute("SHOW server_version").fetchone()["server_version"]
        idx = conn.execute("""SELECT i.tablename, i.indexname, i.indexdef, c.conname, c.contype
                              FROM pg_indexes i
                              LEFT JOIN pg_constraint c ON c.conindid = (i.schemaname || '.' || i.indexname)::regclass
                                                       AND c.contype IN ('p', 'u')
                              WHERE i.schemaname = 'public' ORDER BY i.tablename, i.indexname""").fetchall()
        res["indexes"] = idx
        res["table_rows"] = {r["t"]: r["n"] for r in conn.execute(
            "SELECT relname AS t, reltuples::bigint AS n FROM pg_class WHERE relname = ANY(%s)", (PG_TABLES,))}
        for t in PG_TABLES:      # exact row counts (reltuples is only the planner's estimate)
            res["table_rows"][t] = conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
        print("  Indexes in the database (pg_indexes):")
        for r in idx:
            print(f"    {r['tablename']:22} {r['indexname']:38} {'constraint' if r['conname'] else ''}")
        print("  Rows per table: " + ", ".join(f"{t} {n}" for t, n in res["table_rows"].items()))

        cases = []
        for name, drug in PG_CASES:
            p = conn.execute("SELECT NRIC, Name FROM Patient WHERE Name = %s", (name,)).fetchone()
            d = conn.execute("SELECT DrugID, GenericName FROM Drug WHERE GenericName = %s", (drug,)).fetchone()
            n = conn.execute("SELECT count(*) AS n FROM v_active_medications WHERE NRIC = %s", (p["nric"],)).fetchone()["n"]
            cases.append({"nric": p["nric"], "patient": p["name"], "drug_id": d["drugid"], "drug": d["genericname"], "active_meds": n})
        res["cases"] = cases
        conn.rollback()

        drop = []
        for r in idx:
            if r["tablename"] not in PG_TABLES:
                continue
            drop.append(f"ALTER TABLE {r['tablename']} DROP CONSTRAINT {r['conname']} CASCADE" if r["conname"]
                        else f"DROP INDEX {r['indexname']}")
        res["drop_statements"] = drop

        for title, source, sql in PG_QUERIES:
            q = {"title": title, "source": source, "sql": sql, "cases": []}
            print(f"\n  {title}  ({source})")
            for case in cases:
                params = {"nric": case["nric"], "ids": [case["drug_id"]]}
                with_idx = run_pg(conn, sql, params)
                conn.rollback()
                # Without: drop every index on the tables read, time it, then ROLLBACK (DDL is transactional).
                for stmt in drop:
                    conn.execute(stmt)
                left = conn.execute("SELECT count(*) AS n FROM pg_indexes WHERE tablename = ANY(%s)", (PG_TABLES,)).fetchone()["n"]
                without = run_pg(conn, sql, params)
                conn.rollback()
                back = conn.execute("SELECT count(*) AS n FROM pg_indexes WHERE schemaname = 'public'").fetchone()["n"]
                conn.rollback()
                assert left == 0 and back == len(idx), "indexes were not restored"
                q["cases"].append({**case, "with": with_idx, "without": without})
                print(f"    {case['patient']:22} + {case['drug']:14} with: {with_idx['execution_ms_median']:.3f} ms, "
                      f"{with_idx['rows_examined']} rows, {with_idx['scan_counts']}")
                print(f"    {'':39} without: {without['execution_ms_median']:.3f} ms, "
                      f"{without['rows_examined']} rows, {without['scan_counts']}")
            res["queries"].append(q)
        after = conn.execute("""SELECT count(*) AS n FROM pg_indexes WHERE schemaname = 'public'""").fetchone()["n"]
        fks = conn.execute("SELECT count(*) AS n FROM pg_constraint WHERE contype = 'f'").fetchone()["n"]
        res["indexes_after"] = after
        res["foreign_keys_after"] = fks
        print(f"\n  After all rollbacks: {after} indexes (was {len(idx)}), {fks} foreign keys.")
    return res


def mongo_stats(db, coll, cmd):
    """RUNS times explain('executionStats'), plus the client-side time of the real find."""
    runs, wall = [], []
    for _ in range(RUNS):
        ex = db.command("explain", {"find": coll, **cmd}, verbosity="executionStats")
        runs.append(ex)
        t0 = time.perf_counter()
        list(db[coll].find(cmd["filter"], sort=list(cmd.get("sort", {}).items()) or None,
                           limit=cmd.get("limit", 0), hint=cmd.get("hint")))
        wall.append((time.perf_counter() - t0) * 1000)
    ex = runs[-1]
    stages, node = [], ex["queryPlanner"]["winningPlan"]
    node = node.get("queryPlan", node)

    def visit(n):
        s = n["stage"] + (f"({n['indexName']})" if n.get("indexName") else "")
        stages.append(s)
        for k in ("inputStage",):
            if k in n:
                visit(n[k])
        for c in n.get("inputStages", []):
            visit(c)
    visit(node)
    st = ex["executionStats"]
    return {"plan": " <- ".join(stages),
            "executionTimeMillis_runs": [r["executionStats"]["executionTimeMillis"] for r in runs],
            "executionTimeMillis_median": statistics.median(r["executionStats"]["executionTimeMillis"] for r in runs),
            "executionTimeMicros_runs": [r["executionStats"].get("executionTimeMicros") for r in runs],
            "executionTimeMicros_median": statistics.median(r["executionStats"].get("executionTimeMicros", 0) for r in runs),
            "client_ms_runs": [round(w, 3) for w in wall], "client_ms_median": round(statistics.median(wall), 3),
            "totalDocsExamined": st["totalDocsExamined"], "totalKeysExamined": st["totalKeysExamined"],
            "nReturned": st["nReturned"]}


def timing_mongo(nrics):
    head("STEP 2b. MongoDB query timing: explain('executionStats'), with the index and with a forced collection scan")
    client = mongo_client()
    db = client[MONGO_DB]
    res = {"runs": RUNS, "version": client.server_info()["version"],
           "counts": {c: db[c].count_documents({}) for c in ("patient_history", "encounters")},
           "indexes": {c: {k: v["key"] for k, v in db[c].index_information().items()} for c in ("patient_history", "encounters")},
           "queries": []}
    print(f"  Documents: {res['counts']}")
    natural = {"$natural": 1}

    # 1. patient_history by nric (medsafe/records.py history())
    q = {"title": "patient_history lookup by nric", "source": "records.history", "cases": [],
         "shell": 'db.patient_history.find({nric: N, status: {$ne: "entered_in_error"}}).sort({"effectivePeriod.start": -1})'}
    for nric in nrics:
        cmd = {"filter": {"nric": nric, "status": {"$ne": "entered_in_error"}}, "sort": {"effectivePeriod.start": -1}}
        w = mongo_stats(db, "patient_history", cmd)
        wo = mongo_stats(db, "patient_history", {**cmd, "hint": natural})
        q["cases"].append({"nric": nric, "with": w, "without": wo})
    res["queries"].append(q)

    # 2. encounters text search (medsafe/records.py encounters(nric, search)). $text cannot run
    #    without its text index, so the no-index side is the same search as a regex with a forced scan.
    term = "wheeze"
    q = {"title": f'encounters note search for "{term}"', "source": "records.encounters", "cases": [], "term": term,
         "shell": f'db.encounters.find({{$text: {{$search: "{term}"}}}}).sort({{visitDate: -1}}).limit(8)',
         "shell_without": f'db.encounters.find({{$or: [{{"consultation.note": /{term}/i}}, {{"triage.chiefComplaint": /{term}/i}}]}})'
                          '.sort({visitDate: -1}).limit(8).hint({$natural: 1})'}
    try:
        db.command("explain", {"find": "encounters", "filter": {"$text": {"$search": term}}, "hint": natural},
                   verbosity="executionStats")
        q["text_with_natural_hint"] = "ran"
    except OperationFailure as e:
        q["text_with_natural_hint"] = f"code {e.code}: {e.details.get('errmsg', str(e))}"
    print(f"  $text with hint({{$natural: 1}}): {q['text_with_natural_hint']}")
    regex = {"$regex": term, "$options": "i"}
    scan_filter = {"$or": [{"consultation.note": regex}, {"triage.chiefComplaint": regex}]}
    w = mongo_stats(db, "encounters", {"filter": {"$text": {"$search": term}}, "sort": {"visitDate": -1}, "limit": 8})
    wo = mongo_stats(db, "encounters", {"filter": scan_filter, "sort": {"visitDate": -1}, "limit": 8, "hint": natural})
    q["matching_documents"] = {"text": db.encounters.count_documents({"$text": {"$search": term}}),
                               "regex": db.encounters.count_documents(scan_filter)}
    q["cases"].append({"nric": "(all patients)", "with": w, "without": wo})
    # The same search inside one patient's visits, as the record page runs it.
    for nric in nrics[1:2]:
        w = mongo_stats(db, "encounters", {"filter": {"nric": nric, "$text": {"$search": term}}, "sort": {"visitDate": -1}, "limit": 8})
        wo = mongo_stats(db, "encounters", {"filter": {"nric": nric, **scan_filter}, "sort": {"visitDate": -1}, "limit": 8, "hint": natural})
        q["cases"].append({"nric": nric, "with": w, "without": wo})
    res["queries"].append(q)

    for q in res["queries"]:
        print(f"\n  {q['title']}  ({q['source']})")
        for c in q["cases"]:
            for label in ("with", "without"):
                s = c[label]
                print(f"    {c['nric']:15} {label:8} {s['plan'][:58]:58} docs {s['totalDocsExamined']:4} keys {s['totalKeysExamined']:4} "
                      f"returned {s['nReturned']:3}  server {s['executionTimeMillis_median']} ms / {s['executionTimeMicros_median']} us  client {s['client_ms_median']} ms")
    client.close()
    return res


# ---------------------------------------------------------------------------------------
# Markdown tables for docs/db_evidence.md (so the numbers are copied, not retyped)
# ---------------------------------------------------------------------------------------
def scans_text(s):
    return ", ".join(f"{n} {t}" for t, n in sorted(s["scan_counts"].items()))


def timing_markdown(t):
    out = []
    p, m = t["postgres"], t["mongo"]
    out.append("| Query | Patient + drug prescribed | Indexes | Scan nodes in the plan | Rows read by scans | Buffers | "
               "Execution time, median of 5 (ms) | All 5 runs (ms) |\n|---|---|---|---|---|---|---|---|")
    for q in p["queries"]:
        for c in q["cases"]:
            for label, name in (("with", "with"), ("without", "without")):
                s = c[label]
                out.append(f"| {q['title']} | {c['patient']} + {c['drug'].lower()} | {name} | {scans_text(s)} | "
                           f"{s['rows_examined']} | {s['buffers']} | {s['execution_ms_median']:.3f} | "
                           f"{', '.join(f'{x:.3f}' for x in s['execution_ms_runs'])} |")
    out.append("")
    out.append("| Query | Patient | Indexes the plan used | Tables read by sequential scan (with indexes) | Planning time, median (ms): with / without |\n|---|---|---|---|---|")
    for q in p["queries"]:
        for c in q["cases"]:
            w, wo = c["with"], c["without"]
            out.append(f"| {q['title']} | {c['patient']} | {', '.join(w['indexes_used']) or 'none'} | "
                       f"{', '.join(w['seq_scans']) or 'none'} | {w['planning_ms_median']:.3f} / {wo['planning_ms_median']:.3f} |")
    out.append("")
    out.append("| Query | Patient | Plan | Winning plan | totalDocsExamined | totalKeysExamined | nReturned | "
               "executionTimeMillis, median of 5 | executionTimeMicros, median of 5 | All 5 runs (micros) | "
               "Client round trip, median of 5 (ms) |\n|---|---|---|---|---|---|---|---|---|---|---|")
    for q in m["queries"]:
        for c in q["cases"]:
            for label, name in (("with", "index"), ("without", "collection scan")):
                s = c[label]
                out.append(f"| {q['title']} | {c['nric']} | {name} | {s['plan']} | {s['totalDocsExamined']} | "
                           f"{s['totalKeysExamined']} | {s['nReturned']} | {s['executionTimeMillis_median']} | {s['executionTimeMicros_median']} | "
                           f"{', '.join(str(x) for x in s['executionTimeMicros_runs'])} | {s['client_ms_median']:.3f} |")
    out.append("")
    for q in p["queries"]:
        c = q["cases"][0]
        out.append(f"#### {q['title']}: {c['patient']} + {c['drug'].lower()}\n")
        out.append("With indexes:\n\n```\n" + c["with"]["plan_text"] + "\n```\n")
        out.append("Without indexes:\n\n```\n" + c["without"]["plan_text"] + "\n```\n")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "acid":
        res = {"ran_at": datetime.now().astimezone().isoformat(), "atomicity": atomicity(), "consistency": consistency(),
               "isolation": isolation(), "limits": limits()}
        head("SUMMARY")
        for k in ("atomicity", "consistency", "isolation"):
            print(f"  {k:12} {verdict(res[k]['pass'])}")
        save("acid", res)
    elif cmd == "durability-prepare":
        durability_prepare()
    elif cmd == "durability-verify":
        durability_verify()
    elif cmd == "timing":
        p = timing_pg()
        res = {"ran_at": datetime.now().astimezone().isoformat(), "postgres": p,
               "mongo": timing_mongo([c["nric"] for c in p["cases"]])}
        save("timing", res)
        md = OUT_DIR / "timing_tables.md"
        md.write_text(timing_markdown(res), encoding="utf-8")
        print(f"[saved {md}]")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

"""Database connections.

PostgreSQL: one psycopg connection per request (stored on flask.g), rows as dicts.
  Reads need no commit; every write function calls commit() itself.
MongoDB: one shared MongoClient for the app. Dates come back timezone-aware in APP_TZ,
  so a date stored as local midnight shows as the right day.
"""
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from flask import current_app, g
from psycopg.rows import dict_row
from pymongo import MongoClient
from werkzeug.security import generate_password_hash

_mongo_client = None


def init_app(app):
    app.teardown_appcontext(_close_pg)
    with app.app_context():
        _run_startup_sql()


def pg():
    if "pg" not in g:
        g.pg = psycopg.connect(current_app.config["PG_DSN"], row_factory=dict_row)
    return g.pg


def _close_pg(_exc=None):
    conn = g.pop("pg", None)
    if conn is not None:
        conn.close()  # anything not committed is rolled back


def query(sql, params=None):
    with pg().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def mongo():
    global _mongo_client
    cfg = current_app.config
    if _mongo_client is None:
        _mongo_client = MongoClient(cfg["MONGO_URI"], tz_aware=True, tzinfo=ZoneInfo(cfg["APP_TZ"]),
                                    serverSelectionTimeoutMS=3000)
    return _mongo_client[cfg["MONGO_DB"]]


def _run_startup_sql():
    """Create the app's views, trigger and indexes (idempotent).

    The setup script drops and rebuilds the tables, which also drops these objects,
    so the app recreates them every time it starts.
    """
    sql = (Path(__file__).resolve().parent.parent / "sql" / "app_objects_postgres.sql").read_text(encoding="utf-8")
    try:
        with psycopg.connect(current_app.config["PG_DSN"], autocommit=True) as conn:
            conn.execute(sql)
            _seed_logins(conn)
    except psycopg.OperationalError as e:
        print(f"\n!! Could not connect to PostgreSQL: {e}\n   Check PG_DSN in your .env file.\n")
        raise


DEMO_PASSWORD = "medsafe123"


def _seed_logins(conn):
    """Give every doctor and pharmacist without a login one: doctor<ID> / pharmacist<ID>,
    password DEMO_PASSWORD (listed in the setup guide). Existing logins are left alone."""
    missing = conn.execute("""
        SELECT 'doctor' || d.DoctorID, d.DoctorID, NULL::int FROM Doctor d
        WHERE NOT EXISTS (SELECT 1 FROM AppUser u WHERE u.DoctorID = d.DoctorID)
        UNION ALL
        SELECT 'pharmacist' || p.PharmacistID, NULL::int, p.PharmacistID FROM Pharmacist p
        WHERE NOT EXISTS (SELECT 1 FROM AppUser u WHERE u.PharmacistID = p.PharmacistID)""").fetchall()
    for username, doctor_id, pharmacist_id in missing:
        conn.execute("INSERT INTO AppUser (Username, PasswordHash, DoctorID, PharmacistID) VALUES (%s, %s, %s, %s) "
                     "ON CONFLICT (Username) DO NOTHING",
                     (username, generate_password_hash(DEMO_PASSWORD), doctor_id, pharmacist_id))

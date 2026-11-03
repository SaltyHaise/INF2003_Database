"""Sign-in as a doctor or pharmacist from the database, and role checks.

This is a demo sign-in (pick who you are, no password). Every screen checks the role,
so a pharmacist can't prescribe and a doctor can't approve.
"""
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from .db import query, query_one

bp = Blueprint("auth", __name__)


def current_user():
    return session.get("user")


def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            user = current_user()
            if not user:
                return redirect(url_for("auth.login", next=request.full_path))
            if user["role"] not in roles:
                flash("That page is for " + " and ".join(r + "s" for r in roles) + ".", "error")
                return redirect(url_for("auth.home"))
            return fn(*a, **kw)
        return wrapper
    return deco


@bp.app_context_processor
def inject_user():
    return {"user": current_user()}


@bp.route("/")
def home():
    user = current_user()
    if not user:
        return redirect(url_for("auth.login"))
    return redirect(url_for("doctor.prescribe" if user["role"] == "doctor" else "pharmacist.queue"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        role, uid = request.form.get("role"), request.form.get("id", type=int)
        if role == "doctor":
            row = query_one("SELECT DoctorID AS id, Name AS name, Hospital AS place FROM Doctor WHERE DoctorID = %s", (uid,))
        elif role == "pharmacist":
            row = query_one("SELECT PharmacistID AS id, Name AS name FROM Pharmacist WHERE PharmacistID = %s", (uid,))
        else:
            row = None
        if not row:
            flash("Choose a doctor or pharmacist to sign in as.", "error")
            return redirect(url_for("auth.login"))
        session["user"] = {"role": role, "id": row["id"], "name": row["name"], "place": row.get("place")}
        nxt = request.args.get("next")
        return redirect(nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else url_for("auth.home"))
    doctors = query("SELECT DoctorID AS id, Name AS name, Hospital FROM Doctor ORDER BY Name")
    pharmacists = query("SELECT PharmacistID AS id, Name AS name FROM Pharmacist ORDER BY Name")
    return render_template("login.html", doctors=doctors, pharmacists=pharmacists)


@bp.route("/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    return redirect(url_for("auth.login"))

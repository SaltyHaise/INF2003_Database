"""Sign-in with a username and password, and role checks.

Logins live in AppUser (sql/app_objects_postgres.sql): a username, a salted password hash,
and the doctor or pharmacist it belongs to. Every screen checks the role, so a pharmacist
can't prescribe and a doctor can't approve.
"""
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import query_one

# Checked when the username doesn't exist, so a wrong username takes as long as a wrong password.
_DUMMY_HASH = generate_password_hash("not-a-real-password")

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
    return redirect(url_for("consult.start" if user["role"] == "doctor" else "pharmacist.queue"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        row = query_one("""
            SELECT u.PasswordHash,
                   CASE WHEN u.DoctorID IS NOT NULL THEN 'doctor' ELSE 'pharmacist' END AS role,
                   COALESCE(u.DoctorID, u.PharmacistID) AS id,
                   COALESCE(d.Name, p.Name) AS name, d.Hospital AS place
            FROM AppUser u
            LEFT JOIN Doctor d     ON d.DoctorID = u.DoctorID
            LEFT JOIN Pharmacist p ON p.PharmacistID = u.PharmacistID
            WHERE u.Username = %s""", (username,))
        ok = check_password_hash(row["passwordhash"] if row else _DUMMY_HASH, password)
        if not (row and ok):
            flash("Wrong username or password.", "error")
            return render_template("login.html", username=username), 401
        nxt = request.args.get("next")
        csrf = session.get("_csrf")
        session.clear()  # a fresh session for the signed-in user
        if csrf:
            session["_csrf"] = csrf
        session["user"] = {"role": row["role"], "id": row["id"], "name": row["name"], "place": row["place"]}
        return redirect(nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else url_for("auth.home"))
    if current_user():
        return redirect(url_for("auth.home"))
    return render_template("login.html", username="")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))

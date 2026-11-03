"""MedSafe: patient medication safety app (INF2003 G61).

PostgreSQL holds patients, diagnoses, prescriptions, drugs, allergies and interactions.
MongoDB holds visits (encounters), patient history, lab results, lab rules and the
prescription audit trail. See README.md for how each screen uses both.
"""
import os
import secrets

from dotenv import load_dotenv
from flask import Flask, abort, request, session

from . import db, outbox, util


def create_app():
    load_dotenv()
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(16),
        PG_DSN=os.getenv("PG_DSN", "postgresql://postgres:postgres@localhost:5432/medsafety"),
        MONGO_URI=os.getenv("MONGO_URI", "mongodb://localhost:27017"),
        MONGO_DB=os.getenv("MONGO_DB", "medsafety"),
        APP_TZ=os.getenv("APP_TZ", "Asia/Singapore"),
    )

    db.init_app(app)
    util.init_app(app)

    from .auth import bp as auth_bp
    from .api import bp as api_bp
    from .doctor import bp as doctor_bp
    from .patients import bp as patients_bp
    from .pharmacist import bp as pharmacist_bp
    from .reference import bp as reference_bp
    from .insights import bp as insights_bp
    for bp in (auth_bp, api_bp, doctor_bp, patients_bp, pharmacist_bp, reference_bp, insights_bp):
        app.register_blueprint(bp)

    # --- simple CSRF protection for every form POST ------------------------------
    @app.before_request
    def csrf_protect():
        if request.method == "POST":
            sent = request.form.get("_csrf", "")
            if not sent or sent != session.get("_csrf"):
                abort(400, "The form expired. Go back, reload the page and try again.")

    @app.context_processor
    def inject_csrf():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return {"csrf_token": session["_csrf"]}

    # --- retry MongoDB writes that failed earlier (see outbox.py) -----------------
    @app.before_request
    def flush_outbox():
        if request.endpoint and not request.endpoint.startswith("static"):
            outbox.flush()

    return app

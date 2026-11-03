"""Pharmacist: review queue, approve / return to doctor, and the dispensed list.

A decision updates PostgreSQL FIRST (ReviewStatus is the source of truth; the
trg_check_review trigger enforces the rules), then appends the decision to the
prescription's audit document in MongoDB ($set status + $push event).
"""
import psycopg
from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import outbox, records
from .auth import current_user, role_required
from .db import mongo, pg, query, query_one
from .util import now

bp = Blueprint("pharmacist", __name__)

CHECKS = [("dose", "Dose and duration appropriate"),
          ("counselled", "Interaction counselled to patient"),
          ("no_new", "Patient confirms no other new medicines")]
MAJOR = {"avoid", "severe", "major"}


@bp.route("/queue")
@role_required("pharmacist")
def queue():
    alerts_first = request.args.get("alerts_first", "1") == "1"
    rows = query("SELECT * FROM v_pending_reviews ORDER BY PrescriptionID")
    audits = {a["_id"]: a for a in mongo().prescription_audit.find(
        {"_id": {"$in": [r["prescriptionid"] for r in rows]}})}
    for r in rows:
        a = audits.get(r["prescriptionid"]) or {}
        r["alerts"] = a.get("alerts", [])
        r["major"] = sum(1 for x in r["alerts"] if x.get("severity") in MAJOR)
        r["sent_at"] = (a.get("events") or [{}])[0].get("at")
    if alerts_first:
        rows.sort(key=lambda r: (-r["major"], -len(r["alerts"]), r["sent_at"] or now()))
    else:
        rows.sort(key=lambda r: r["sent_at"] or now(), reverse=True)

    sel_id = request.args.get("rx", type=int) or (rows[0]["prescriptionid"] if rows else None)
    sel = next((r for r in rows if r["prescriptionid"] == sel_id), None)
    detail = None
    if sel:
        detail = {"rx": sel, "audit": audits.get(sel_id) or {}, "allergies": records.allergies(sel["nric"])}
        detail["prescribed"] = next((e for e in detail["audit"].get("events", []) if e["action"] == "prescribed"), {})
    return render_template("queue.html", rows=rows, sel=detail, alerts_first=alerts_first, checks=CHECKS)


@bp.route("/queue/<int:rx_id>/decide", methods=["POST"])
@role_required("pharmacist")
def decide(rx_id):
    f = request.form
    user = current_user()
    action = f.get("action")
    note = f.get("note", "").strip()[:255]
    ticked = [k for k, _ in CHECKS if f.get("check_" + k)]
    back = redirect(url_for("pharmacist.queue", rx=rx_id))

    if action == "return" and not note:
        flash("Write a note to the doctor before returning the prescription.", "error")
        return back
    if action == "approve" and len(ticked) < len(CHECKS):
        flash("Tick all three checks before approving.", "error")
        return back
    if action not in ("approve", "return"):
        return back
    status = "Approved" if action == "approve" else "Rejected"

    # ---- 1. PostgreSQL: only if still pending (two pharmacists can't both decide) ----
    conn = pg()
    try:
        cur = conn.execute("""
            UPDATE Prescription SET ReviewStatus = %s, PharmacistID = %s, ReviewedAt = LOCALTIMESTAMP(0), ReviewNotes = %s
            WHERE PrescriptionID = %s AND ReviewStatus = 'Pending'""", (status, user["id"], note or None, rx_id))
        if cur.rowcount != 1:
            conn.rollback()
            flash("This prescription was already reviewed.", "error")
            return redirect(url_for("pharmacist.queue"))
        conn.commit()
    except psycopg.Error as e:  # e.g. the trigger: severe allergy needs a note
        conn.rollback()
        flash(e.diag.message_primary or str(e), "error")
        return back

    # ---- 2. MongoDB audit trail ------------------------------------------------------
    event = {"at": now(), "action": "approved" if status == "Approved" else "rejected",
             "by": {"role": "pharmacist", "id": user["id"], "name": user["name"]}, "checks": ticked}
    if note:
        event["note"] = note
    outbox.run("prescription_audit", "update_one", {"_id": rx_id},
               {"$set": {"status": status}, "$push": {"events": event}})

    rx = query_one("SELECT GenericName, patient_name FROM v_prescription_detail WHERE PrescriptionID = %s", (rx_id,))
    flash(f"{rx['genericname']} for {rx['patient_name']} "
          f"{'approved and dispensed' if status == 'Approved' else 'returned to the doctor'}.", "success")
    return redirect(url_for("pharmacist.queue"))


@bp.route("/dispensed")
@role_required("pharmacist")
def dispensed():
    status = request.args.get("status", "")
    where = "ReviewStatus IN ('Approved','Rejected')" if status not in ("Approved", "Rejected") else "ReviewStatus = %(s)s"
    rows = query(f"""SELECT * FROM v_prescription_detail WHERE {where}
                     ORDER BY ReviewedAt DESC NULLS LAST, PrescriptionID DESC LIMIT 60""", {"s": status})
    return render_template("dispensed.html", rows=rows, status=status)

"""Small helpers shared by the screens: template filters and formatting."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import current_app

EFFECT_LABELS = {
    "drug_exposure": "changes drug levels",
    "qt_prolongation": "QT prolongation",
    "cns_depression": "CNS depression",
    "gi_bleeding": "GI bleeding",
}


def today():
    return datetime.now(ZoneInfo(current_app.config["APP_TZ"])).date()


def now():
    return datetime.now(ZoneInfo(current_app.config["APP_TZ"]))


def to_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(ZoneInfo(current_app.config["APP_TZ"])).date() if v.tzinfo else v.date()
    return v


def fmt_date(v, fmt="%d %b %Y"):
    d = to_date(v)
    return d.strftime(fmt) if d else ""


def fmt_month(v):
    return fmt_date(v, "%b %Y")


def fmt_time(v):
    if not v:
        return ""
    if isinstance(v, datetime) and v.tzinfo:
        v = v.astimezone(ZoneInfo(current_app.config["APP_TZ"]))
    return v.strftime("%H:%M")


def fmt_when(v):
    """'12:31' for today, otherwise '29 Oct 12:31'."""
    if not v:
        return ""
    if isinstance(v, datetime) and v.tzinfo:
        v = v.astimezone(ZoneInfo(current_app.config["APP_TZ"]))
    return v.strftime("%H:%M") if to_date(v) == today() else v.strftime("%d %b %H:%M")


def age(dob):
    if not dob:
        return ""
    t = today()
    return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))


def initials(name):
    parts = [p for p in (name or "").replace("/", " ").split() if p[:1].isalpha() and p.lower() not in ("bin", "binte", "d", "o", "s")]
    return "".join(p[0] for p in parts[:2]).upper()


def effect_label(code):
    if not code:
        return ""
    return EFFECT_LABELS.get(code, code.replace("_", " "))


def dose_text(row):
    """'500 mg twice daily' from a template/prescription row."""
    if not row:
        return ""
    amt = row.get("dose_amount")
    if amt is not None:
        amt = int(amt) if float(amt).is_integer() else float(amt)
    parts = [f"{amt} {row.get('dose_unit') or ''}".strip() if amt is not None else "", row.get("frequency") or ""]
    return " ".join(p for p in parts if p)


def form_route(row):
    bits = [(row.get("dosage_form") or "").title(), (row.get("route") or "").title()]
    return " · ".join(b for b in bits if b)


LAB_NAMES = {"EGFR": "eGFR", "INR": "INR", "HBA1C": "HbA1c", "LDL": "LDL cholesterol", "ALT": "ALT", "TSH": "TSH",
             "CRP": "CRP", "WBC": "White cell count", "URIC_ACID": "Uric acid"}


def lab_name(code):
    return LAB_NAMES.get(code, (code or "").replace("_", " ").title())


def init_app(app):
    app.jinja_env.filters.update(
        d=fmt_date, mon=fmt_month, hm=fmt_time, when=fmt_when, age=age, initials=initials,
        effect=effect_label, dose=dose_text, formroute=form_route, labname=lab_name,
    )
    app.jinja_env.globals.update(today=today)

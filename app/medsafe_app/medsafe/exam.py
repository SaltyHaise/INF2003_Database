"""Structured consultation: what the patient tells the doctor (history) and what the
doctor finds (examination).

The visit form offers tick-boxes ("chips") from the fixed vocabulary below, plus a short
free-text line per section. Only codes from this vocabulary are saved, so every visit in
MongoDB `encounters` uses the same terms (e.g. "crackles", never "Crackles (R)") and the
data can be queried, e.g. db.encounters.find({"consultation.exam": {$elemMatch:
{system: "chest", findings: "crackles"}}}).

Saved shape (inside the encounter's `consultation`):
    history: {symptoms: ["fever", "productive_cough"], duration: "4 days", notes: "...",
              pain: {site: "right chest", trigger: "coughing"}}          # pain is optional
    exam:    [{system: "chest", findings: ["crackles"], note: "right lower zone"},
              {system: "skin",  findings: ["normal"]}]                    # only systems examined
    formVersion: 1                                                        # older visits have none

"normal" is a finding of its own: it means "examined and normal", which is different
from a system that wasn't examined (absent from the list).

`consultation.note` stays required by the collection's validator and is covered by the
text index, so `compose_note` writes the history and findings into it as readable text
ahead of the doctor's assessment and plan. Searching notes for "crackles" therefore finds
structured visits too.
"""

FORM_VERSION = 1

SYMPTOMS = [
    ("fever", "Fever"),
    ("cough", "Dry cough"),
    ("productive_cough", "Productive cough"),
    ("sore_throat", "Sore throat"),
    ("runny_nose", "Runny nose"),
    ("breathless", "Breathless"),
    ("chest_pain", "Chest pain"),
    ("palpitations", "Palpitations"),
    ("headache", "Headache"),
    ("dizziness", "Dizziness"),
    ("nausea", "Nausea"),
    ("vomiting", "Vomiting"),
    ("diarrhoea", "Diarrhoea"),
    ("abdominal_pain", "Abdominal pain"),
    ("urinary", "Urinary symptoms"),
    ("rash", "Rash or itch"),
    ("joint_pain", "Joint pain"),
    ("fatigue", "Tiredness"),
    ("weight_loss", "Weight loss"),
]

# Body systems the doctor can examine. Each has a "normal" finding (with its own wording)
# and the abnormal findings a GP most often records. `keywords` pre-select the system when
# they appear in the chief complaint (the doctor can still untick it).
SYSTEMS = [
    {"key": "general", "label": "General", "normal": "Looks well",
     "findings": [("unwell", "Looks unwell"), ("pale", "Pale"), ("dehydrated", "Dehydrated"), ("jaundiced", "Jaundiced")],
     "keywords": ["fever", "tired", "weak", "unwell", "weight"]},
    {"key": "throat", "label": "Throat", "normal": "Normal",
     "findings": [("injected", "Injected"), ("tonsils_enlarged", "Tonsils enlarged"), ("exudate", "Exudate"),
                  ("neck_nodes", "Neck nodes enlarged")],
     "keywords": ["throat", "cough", "cold", "flu", "fever", "runny"]},
    {"key": "ears", "label": "Ears", "normal": "Normal",
     "findings": [("red_eardrum", "Red eardrum"), ("discharge", "Discharge"), ("wax", "Wax")],
     "keywords": ["ear", "hearing"]},
    {"key": "chest", "label": "Chest", "normal": "Clear",
     "findings": [("crackles", "Crackles"), ("wheeze", "Wheeze"), ("reduced_air_entry", "Reduced air entry")],
     "keywords": ["cough", "breath", "chest", "wheeze", "asthma", "sputum"]},
    {"key": "heart", "label": "Heart", "normal": "Normal heart sounds",
     "findings": [("irregular", "Irregular pulse"), ("murmur", "Murmur"), ("ankle_swelling", "Ankle swelling")],
     "keywords": ["palpitation", "heart", "irregular", "chest pain", "blood pressure", "swollen ankle"]},
    {"key": "abdomen", "label": "Abdomen", "normal": "Soft, non-tender",
     "findings": [("tender", "Tender"), ("guarding", "Guarding"), ("distended", "Distended")],
     "keywords": ["abdo", "stomach", "vomit", "diarrh", "nausea", "reflux", "burning", "constipat"]},
    {"key": "skin", "label": "Skin", "normal": "No rash",
     "findings": [("rash", "Rash"), ("wound", "Wound or ulcer"), ("swelling", "Swelling"), ("bruising", "Bruising")],
     "keywords": ["rash", "itch", "skin", "wound", "ulcer", "bite", "spot"]},
    {"key": "joints", "label": "Joints", "normal": "Normal",
     "findings": [("swollen", "Swollen"), ("tender", "Tender"), ("reduced_movement", "Reduced movement"), ("warm", "Warm")],
     "keywords": ["joint", "knee", "toe", "hip", "shoulder", "back", "gout", "sprain"]},
    {"key": "neuro", "label": "Neuro", "normal": "Normal",
     "findings": [("weakness", "Weakness"), ("numbness", "Numbness"), ("unsteady", "Unsteady gait")],
     "keywords": ["numb", "weak", "dizz", "headache", "faint", "tingl"]},
]

SYSTEM_BY_KEY = {s["key"]: s for s in SYSTEMS}
SYMPTOM_LABEL = dict(SYMPTOMS)
MAX_TEXT = 300


def finding_label(system, code):
    s = SYSTEM_BY_KEY.get(system)
    if not s:
        return code.replace("_", " ")
    if code == "normal":
        return s["normal"]
    return dict(s["findings"]).get(code, code.replace("_", " "))


def system_label(system):
    s = SYSTEM_BY_KEY.get(system)
    return s["label"] if s else system.replace("_", " ").title()


def symptom_label(code):
    return SYMPTOM_LABEL.get(code, code.replace("_", " "))


def _text(form, name, limit=MAX_TEXT):
    return " ".join((form.get(name) or "").split())[:limit]


def parse(form):
    """Read the History and Examination sections of the visit form.

    Returns (history, exam, errors). Unknown codes are refused rather than dropped, so a
    tampered or out-of-date form can't save terms outside the vocabulary.
    """
    errors = []

    # ---- history: what the patient tells you -------------------------------------
    symptoms = []
    for code in form.getlist("symptoms"):
        if code not in SYMPTOM_LABEL:
            errors.append(f"Unknown symptom: {code}.")
        elif code not in symptoms:
            symptoms.append(code)
    history = {"symptoms": symptoms}
    for field in ("duration", "notes"):
        v = _text(form, f"hx_{field}", 40 if field == "duration" else 1000)
        if v:
            history[field] = v
    pain = {k: _text(form, f"pain_{k}", 80) for k in ("site", "trigger")}
    pain = {k: v for k, v in pain.items() if v}
    if pain:
        history["pain"] = pain
    if not (symptoms or len(history) > 1):
        history = None

    # ---- examination: what you find ---------------------------------------------
    exam = []
    for key in form.getlist("exam_systems"):
        s = SYSTEM_BY_KEY.get(key)
        if not s:
            errors.append(f"Unknown body system: {key}.")
            continue
        allowed = {"normal"} | {c for c, _ in s["findings"]}
        found = []
        for code in form.getlist(f"exam_{key}"):
            if code not in allowed:
                errors.append(f"Unknown finding for {s['label'].lower()}: {code}.")
            elif code not in found:
                found.append(code)
        if "normal" in found and len(found) > 1:
            errors.append(f"{s['label']}: choose {s['normal'].lower()} or the abnormal findings, not both.")
        note = _text(form, f"exam_{key}_note")
        if not found and not note:
            errors.append(f"{s['label']} is ticked as examined but has no finding. "
                          f"Pick a finding (or \"{s['normal']}\") or untick it.")
            continue
        entry = {"system": key, "findings": found}
        if note:
            entry["note"] = note
        exam.append(entry)
    return history, exam, errors


def compose_note(complaint, history, exam, pain_score, plan_note):
    """Readable note: history, examination, then the doctor's assessment and plan."""
    parts = []
    if history:
        bits = [", ".join(symptom_label(c).lower() for c in history.get("symptoms", []))]
        if history.get("duration"):
            bits.append(f"for {history['duration']}")
        line = " ".join(b for b in bits if b)
        parts.append("History: " + (line or complaint) + "."
                     + (f" {history['notes']}" if history.get("notes") else ""))
        if history.get("pain"):
            p = history["pain"]
            where = ", ".join(x for x in (p.get("site"), f"on {p['trigger']}" if p.get("trigger") else "") if x)
            parts.append(f"Pain: {pain_score}/10, {where}.")
    if exam:
        items = []
        for e in exam:
            found = ", ".join(finding_label(e["system"], c).lower() for c in e["findings"])
            if e.get("note"):
                found = f"{found} ({e['note']})" if found else e["note"]
            items.append(f"{system_label(e['system'])}: {found}")
        parts.append("Examination: " + ". ".join(items) + ".")
    if plan_note:
        parts.append(plan_note)
    return "\n".join(parts)


def init_app(app):
    app.jinja_env.filters.update(symptom=symptom_label, finding=finding_label, system=system_label)
    app.jinja_env.globals.update(EXAM_SYMPTOMS=SYMPTOMS, EXAM_SYSTEMS=SYSTEMS)

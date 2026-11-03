"""Search-as-you-type endpoints used by the autocomplete boxes (JSON).

Each matches the typed text ANYWHERE in the name (ILIKE '%text%', fast thanks to the
pg_trgm GIN indexes), and ranks names that start with the text first, then by
trigram similarity.
"""
from flask import Blueprint, jsonify, request

from .auth import role_required
from .db import query
from .util import age

bp = Blueprint("api", __name__, url_prefix="/api")


def _term():
    return (request.args.get("q") or "").strip()[:80]


@bp.route("/patients")
@role_required("doctor", "pharmacist")
def patients():
    q = _term()
    if len(q) < 2:
        return jsonify([])
    rows = query("""
        SELECT NRIC, Name, DOB, Sex FROM Patient
        WHERE Name ILIKE %(like)s OR NRIC ILIKE %(prefix)s
        ORDER BY (Name ILIKE %(start)s OR NRIC ILIKE %(prefix)s) DESC, similarity(Name, %(q)s) DESC, Name
        LIMIT 10""", {"q": q, "like": f"%{q}%", "start": f"{q}%", "prefix": f"{q}%"})
    return jsonify([{"id": r["nric"], "label": r["name"],
                     "sub": f"{r['nric']} · {r['sex'] or ''}, {age(r['dob'])}"} for r in rows])


@bp.route("/diseases")
@role_required("doctor", "pharmacist")
def diseases():
    q = _term()
    if len(q) < 2:
        return jsonify([])
    rows = query("""
        SELECT ds.DiseaseID, ds.Name, count(ta.DrugID) AS drugs
        FROM Disease ds LEFT JOIN ToAddress ta ON ta.DiseaseID = ds.DiseaseID
        WHERE ds.Name ILIKE %(like)s
        GROUP BY ds.DiseaseID, ds.Name
        ORDER BY (ds.Name ILIKE %(start)s) DESC, similarity(ds.Name, %(q)s) DESC, ds.Name
        LIMIT 10""", {"q": q, "like": f"%{q}%", "start": f"{q}%"})
    return jsonify([{"id": r["diseaseid"], "label": r["name"], "sub": f"{r['drugs']} drugs in reference data"} for r in rows])


@bp.route("/drugs")
@role_required("doctor", "pharmacist")
def drugs():
    q = _term()
    if len(q) < 2:
        return jsonify([])
    rows = query("""
        SELECT d.DrugID, d.GenericName,
               (SELECT count(*) FROM PrescriptionTemplate t WHERE t.DrugID = d.DrugID) AS templates
        FROM Drug d
        WHERE d.GenericName ILIKE %(like)s
        ORDER BY (d.GenericName ILIKE %(start)s) DESC, similarity(d.GenericName, %(q)s) DESC, d.GenericName
        LIMIT 12""", {"q": q, "like": f"%{q}%", "start": f"{q}%"})
    return jsonify([{"id": r["drugid"], "label": r["genericname"],
                     "sub": f"{r['templates']} dose template{'s' if r['templates'] != 1 else ''}"} for r in rows])


@bp.route("/ingredients")
@role_required("doctor", "pharmacist")
def ingredients():
    q = _term()
    if len(q) < 2:
        return jsonify([])
    rows = query("""
        SELECT ai.IngredientID, ai.IngredientName,
               (SELECT count(*) FROM Contains c WHERE c.IngredientID = ai.IngredientID) AS products
        FROM ActiveIngredients ai
        WHERE ai.IngredientName ILIKE %(like)s
        ORDER BY (ai.IngredientName ILIKE %(start)s) DESC, similarity(ai.IngredientName, %(q)s) DESC, ai.IngredientName
        LIMIT 10""", {"q": q, "like": f"%{q}%", "start": f"{q}%"})
    return jsonify([{"id": r["ingredientid"], "label": r["ingredientname"],
                     "sub": f"in {r['products']} product{'s' if r['products'] != 1 else ''}"} for r in rows])

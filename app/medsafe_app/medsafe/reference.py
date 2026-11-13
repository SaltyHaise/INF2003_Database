"""Drug reference: look up an active ingredient (or a drug name) and see its products,
recorded interactions and side effects. All PostgreSQL."""
from flask import Blueprint, render_template, request

from .auth import role_required
from .db import query, query_one

bp = Blueprint("reference", __name__)


def drug_list(letter):
    """Drugs starting with one letter, each with its ingredients, for browsing without a search."""
    letters = {r["letter"]: r["n"] for r in query(
        "SELECT upper(left(GenericName, 1)) AS letter, count(*) AS n FROM Drug GROUP BY 1 ORDER BY 1")}
    letter = letter.strip().upper()[:1]
    if letter not in letters:
        letter = next(iter(letters), "")
    drugs = query("""
        SELECT d.DrugID, d.GenericName,
               (SELECT json_agg(json_build_object('id', ai.IngredientID, 'name', ai.IngredientName) ORDER BY ai.IngredientName)
                FROM Contains c JOIN ActiveIngredients ai ON ai.IngredientID = c.IngredientID
                WHERE c.DrugID = d.DrugID) AS ingredients,
               (SELECT count(*) FROM PrescriptionTemplate t WHERE t.DrugID = d.DrugID) AS templates
        FROM Drug d WHERE upper(left(d.GenericName, 1)) = %s
        ORDER BY d.GenericName""", (letter,))
    return {"letters": letters, "letter": letter, "drugs": drugs, "drug_total": sum(letters.values())}


@bp.route("/reference")
@role_required("doctor", "pharmacist")
def reference():
    q = (request.args.get("q") or "").strip()[:80]
    ing_id = request.args.get("ingredient_id", type=int)
    avoid_only = request.args.get("avoid") == "1"
    show_all = request.args.get("all") == "1"
    ctx = {"q": q, "avoid_only": avoid_only, "show_all": show_all, "ing": None, "matches": []}

    if not ing_id and q:
        # exact ingredient name, else best match among ingredients, else via a drug name
        ing = query_one("SELECT IngredientID FROM ActiveIngredients WHERE upper(IngredientName) = upper(%s)", (q,))
        if ing:
            ing_id = ing["ingredientid"]
        else:
            ctx["matches"] = query("""
                SELECT ai.IngredientID, ai.IngredientName, count(c.DrugID) AS products
                FROM ActiveIngredients ai LEFT JOIN Contains c ON c.IngredientID = ai.IngredientID
                WHERE ai.IngredientName ILIKE %(like)s OR ai.IngredientID IN (
                    SELECT c2.IngredientID FROM Contains c2 JOIN Drug d ON d.DrugID = c2.DrugID
                    WHERE d.GenericName ILIKE %(like)s)
                GROUP BY ai.IngredientID, ai.IngredientName
                ORDER BY (ai.IngredientName ILIKE %(start)s) DESC, similarity(ai.IngredientName, %(q)s) DESC
                LIMIT 12""", {"q": q, "like": f"%{q}%", "start": f"{q}%"})
            if len(ctx["matches"]) == 1:
                ing_id = ctx["matches"][0]["ingredientid"]
    if not ing_id:
        if not q:
            ctx.update(drug_list(request.args.get("letter", "")))
        return render_template("reference.html", **ctx)

    ing = query_one("SELECT IngredientID, IngredientName FROM ActiveIngredients WHERE IngredientID = %s", (ing_id,))
    if not ing:
        return render_template("reference.html", **ctx)
    ctx["ing"] = ing
    ctx["q"] = q or ing["ingredientname"]

    ctx["products"] = query("""
        SELECT d.DrugID, d.GenericName,
               (SELECT string_agg(ai.IngredientName, ', ' ORDER BY ai.IngredientName)
                FROM Contains c2 JOIN ActiveIngredients ai ON ai.IngredientID = c2.IngredientID
                WHERE c2.DrugID = d.DrugID AND c2.IngredientID <> %(i)s) AS with_others,
               (SELECT json_agg(json_build_object('form', t.dosage_form, 'route', t.route,
                                                  'dose', t.dose_amount, 'unit', t.dose_unit) ORDER BY t.dose_amount)
                FROM PrescriptionTemplate t WHERE t.DrugID = d.DrugID) AS templates
        FROM Contains c JOIN Drug d ON d.DrugID = c.DrugID
        WHERE c.IngredientID = %(i)s
        ORDER BY (SELECT count(*) FROM Contains c3 WHERE c3.DrugID = d.DrugID), d.GenericName""", {"i": ing_id})

    # One row per interacting ingredient, with all its effects (stored per ingredient pair).
    ctx["interactions"] = query("""
        SELECT other.IngredientName AS other,
               array_agg(DISTINCT x.Symptom ORDER BY x.Symptom) AS symptoms,
               bool_or(x.LabelSaysAvoid) AS labelsaysavoid,
               (array_agg(x.ExampleSentence ORDER BY x.LabelSaysAvoid DESC, length(x.ExampleSentence)))[1] AS examplesentence
        FROM DrugtoDrugInteraction x
        JOIN ActiveIngredients other ON other.IngredientID =
             CASE WHEN x.IngredientID_A = %(i)s THEN x.IngredientID_B ELSE x.IngredientID_A END
        WHERE (x.IngredientID_A = %(i)s OR x.IngredientID_B = %(i)s)
        GROUP BY other.IngredientName
        HAVING %(avoid)s = false OR bool_or(x.LabelSaysAvoid)
        ORDER BY bool_or(x.LabelSaysAvoid) DESC, other.IngredientName""", {"i": ing_id, "avoid": avoid_only})
    ctx["total_interactions"] = query_one("""
        SELECT count(DISTINCT CASE WHEN IngredientID_A = %(i)s THEN IngredientID_B ELSE IngredientID_A END) AS n
        FROM DrugtoDrugInteraction WHERE IngredientID_A = %(i)s OR IngredientID_B = %(i)s""", {"i": ing_id})["n"]

    ctx["side_effects"] = [r["effectname"] for r in query("""
        SELECT se.EffectName FROM HasSideEffects h JOIN SideEffects se ON se.EffectID = h.EffectID
        WHERE h.IngredientID = %s ORDER BY se.EffectName""", (ing_id,))]
    return render_template("reference.html", **ctx)

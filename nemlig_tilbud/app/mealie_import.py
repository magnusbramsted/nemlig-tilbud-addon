"""Henter opskrifter fra Mealie med ingredienser, mængder og antal portioner."""

import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("nemlig.mealie")


def mealie_get(base_url, path, token):
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_all_recipe_summaries(base_url, token):
    recipes = []
    page = 1
    while True:
        data = mealie_get(base_url, f"/api/recipes?page={page}&perPage=100", token)
        items = data.get("items", [])
        if not items:
            break
        recipes.extend(items)
        total = data.get("total")
        if total is None or len(recipes) >= total:
            break
        page += 1
    return recipes


def get_servings(recipe):
    """Antal portioner fra Mealie: recipeServings, ellers tal i recipeYield."""
    for key in ("recipeServings", "recipeYieldQuantity"):
        value = recipe.get(key)
        try:
            if value and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            pass

    match = re.search(r"\d+(?:[.,]\d+)?", str(recipe.get("recipeYield") or ""))
    if match:
        value = float(match.group(0).replace(",", "."))
        if value > 0:
            return value
    return None


def make_ingredient(ingredient):
    """Linjer uden fødevare beholdes, så de vises som 'mangler' i prisen."""
    food = ingredient.get("food") or {}
    unit = ingredient.get("unit") or {}
    food_name = (food.get("name") or "").strip()
    display = (
        ingredient.get("display") or ingredient.get("originalText") or ingredient.get("note") or ""
    ).strip()
    if not food_name and not display:
        return None
    return {
        "name": display or food_name,
        "mealie_food_name": food_name or None,
        "quantity": ingredient.get("quantity"),
        "unit": unit.get("name"),
    }


def run(base_url, token, output_file):
    if not token:
        raise RuntimeError("Mealie API-token mangler. Udfyld 'mealie_api_token' i add-on-konfigurationen.")

    recipes = []
    without_food = 0

    for summary in get_all_recipe_summaries(base_url, token):
        recipe_id = summary.get("id")
        if not recipe_id:
            continue

        recipe = mealie_get(base_url, f"/api/recipes/{quote(recipe_id)}", token)
        ingredients = []
        for item in recipe.get("recipeIngredient") or []:
            converted = make_ingredient(item)
            if converted:
                ingredients.append(converted)
                if not converted["mealie_food_name"]:
                    without_food += 1

        recipes.append({
            "name": recipe.get("name") or summary.get("name") or "Unavngivet ret",
            "mealie_id": recipe_id,
            "mealie_slug": recipe.get("slug") or summary.get("slug"),
            "servings": get_servings(recipe),
            "main_ingredients": ingredients,
        })

    result = {
        "source": "Mealie",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "recipe_count": len(recipes),
        "recipes": recipes,
    }

    temporary = output_file.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output_file)

    LOGGER.info("Importerede %s opskrifter fra Mealie.", len(recipes))
    if without_food:
        LOGGER.warning(
            "%s ingredienslinjer mangler en fødevare i Mealie og kan ikke prissættes. "
            "Kør 'Parse ingredienser' på opskriften i Mealie.",
            without_food,
        )
    return recipes

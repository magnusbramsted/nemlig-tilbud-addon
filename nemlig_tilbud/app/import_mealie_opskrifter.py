import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

MEALIE_BASE_URL = "http://a0d7b954-mealie:9000"
OPTIONS_FILE = Path("/data/options.json")
OUTPUT_FILE = Path("/share/mealie_opskrifter.json")


def load_mealie_token():
    """Læser Mealie-token fra add-on'ens egne indstillinger uden at logge det."""
    try:
        options = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise RuntimeError(f"Kunne ikke læse add-on-indstillinger: {OPTIONS_FILE}.") from err
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Ugyldig JSON i add-on-indstillinger: {OPTIONS_FILE}.") from err

    token = str(options.get("mealie_api_token") or "").strip()
    if not token:
        raise RuntimeError(
            "Mealie API-token mangler. Udfyld 'mealie_api_token' i add-on-konfigurationen."
        )

    return token

def mealie_get(path, token):
    request = Request(
        f"{MEALIE_BASE_URL}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_all_recipe_summaries(token):
    """Henter alle opskrifter; pagination er med for fremtidig udvidelse."""
    recipes = []
    page = 1

    while True:
        data = mealie_get(f"/api/recipes?page={page}&perPage=100", token)
        items = data.get("items", [])

        if not items:
            break

        recipes.extend(items)

        total = data.get("total")
        if total is None or len(recipes) >= total:
            break

        page += 1

    return recipes


def make_ingredient(ingredient):
    """Omsætter en Mealie-ingrediens til den nuværende matchers format."""
    food = ingredient.get("food") or {}
    unit = ingredient.get("unit") or {}

    food_name = (food.get("name") or "").strip()
    display = (ingredient.get("display") or "").strip()

    # Uden food.name kan ingrediensen ikke matches stabilt mod Nemlig-produkter.
    if not food_name:
        return None

    # display er den gældende, menneskeligt læsbare ingrediens fra Mealie.
    # food.name er det tekniske alias, der søges efter i Nemligs tilbud.
    return {
        "name": display or food_name,
        "priority": "secondary",
        "aliases": [food_name.lower()],
        "mealie_food_name": food_name,
        "quantity": ingredient.get("quantity"),
        "unit": unit.get("name"),
    }


def main():
    token = load_mealie_token()
    summaries = get_all_recipe_summaries(token)
    recipes = []

    for summary in summaries:
        recipe_id = summary.get("id")
        if not recipe_id:
            continue

        recipe = mealie_get(f"/api/recipes/{quote(recipe_id)}", token)
        ingredients = []

        for item in recipe.get("recipeIngredient") or []:
            converted = make_ingredient(item)
            if converted:
                ingredients.append(converted)

        recipes.append(
            {
                "name": recipe.get("name") or summary.get("name") or "Unavngivet ret",
                "mealie_name": recipe.get("name") or summary.get("name"),
                "mealie_id": recipe_id,
                "mealie_slug": recipe.get("slug") or summary.get("slug"),
                "main_ingredients": ingredients,
            }
        )

    result = {
        "source": "Mealie",
        "source_url": MEALIE_BASE_URL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "recipe_count": len(recipes),
        "recipes": recipes,
    }

    temporary_file = OUTPUT_FILE.with_suffix(".json.tmp")
    temporary_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(OUTPUT_FILE)

    print(f"Importerede {len(recipes)} Mealie-opskrifter.")
    print(f"Mealie-opskriftsfil gemt i {OUTPUT_FILE}.")


if __name__ == "__main__":
    main()

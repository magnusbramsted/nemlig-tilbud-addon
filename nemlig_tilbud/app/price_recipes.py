"""Beregn hvad hver Mealie-ret koster hos Nemlig lige nu.

For hver ret beregnes:
- kurvpris:     hele pakninger, der skal i kurven (100 g af 200 g bacon = hele pakken)
- forbrugspris: kun den mængde, retten bruger (52 g sukker af 1 kg til 7,50)
- begge pr. portion

Basisvarer (salt, peber, olie, tørrede krydderier) udelades helt.
"""

import json
import logging
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pricing

APP_DIR = Path(__file__).resolve().parent
CATALOG_DEFAULT = APP_DIR / "ingredients.json"
CATALOG_USER = Path("/share/nemlig_ingredienser.json")
RECIPES_FILE = Path("/share/mealie_opskrifter.json")
CACHE_FILE = Path("/share/nemlig_produktcache.json")
OUTPUT_FILE = Path("/share/nemlig_retpriser.json")
DETAILS_FILE = Path("/share/nemlig_retpriser_detaljer.json")

CACHE_MAX_AGE = timedelta(hours=20)
DEFAULT_SERVINGS = 4

# Ikke-parsede Mealie-linjer, der kun består af basisvarer.
PANTRY_LINE_WORDS = {"salt", "peber", "olie", "olivenolie", "vand", "og", "evt", "evt.", "efter", "smag", "lidt"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger("price_recipes")


# --- Katalog ------------------------------------------------------------------

def load_catalog(default_path=CATALOG_DEFAULT, user_path=CATALOG_USER):
    catalog = json.loads(Path(default_path).read_text(encoding="utf-8"))
    if Path(user_path).exists():
        try:
            user = json.loads(Path(user_path).read_text(encoding="utf-8"))
            catalog.update({k.lower(): v for k, v in user.items()})
            LOGGER.info("Indlæste %s egne katalogposter fra %s.", len(user), user_path)
        except json.JSONDecodeError as err:
            LOGGER.error("Ugyldig JSON i %s: %s. Bruger kun standardkatalog.", user_path, err)
    return {k.lower(): v for k, v in catalog.items() if not k.startswith("_")}


def resolve_entry(food, catalog):
    """Find katalogposten for en fødevare (følger same_as). Ukendt -> auto-post."""
    key = (food or "").strip().lower()
    seen = set()
    while key in catalog and "same_as" in catalog[key] and key not in seen:
        seen.add(key)
        key = catalog[key]["same_as"].lower()
    if key in catalog:
        entry = dict(catalog[key])
        entry.setdefault("query", key)
        entry["_key"] = key
        return entry
    return {"query": key, "require": [key], "_key": key, "_auto": True}


# --- Varer ---------------------------------------------------------------------

def load_cache(path=CACHE_FILE):
    try:
        cache = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    fetched = cache.get("fetched_at")
    if not fetched:
        return {}
    age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched)
    if age > CACHE_MAX_AGE:
        return {}
    return cache.get("queries", {})


def fetch_products(driver, queries, cached):
    """Hent de søgninger, der ikke allerede ligger i cachen."""
    import nemlig_soeg  # Kræver en åben browser på nemlig.com.

    missing = sorted(q for q in queries if q not in cached)
    results = {q: cached[q] for q in queries if q in cached}
    if not missing:
        return results, {}
    fetched, errors = nemlig_soeg.search_many(driver, missing)
    results.update(fetched)
    return results, errors


# --- Beregning -----------------------------------------------------------------

def is_pantry_line(text):
    words = re.findall(r"[a-zæøå.]+", str(text or "").lower())
    return bool(words) and all(w in PANTRY_LINE_WORDS for w in words)


def price_ingredient(ingredient, catalog, products_by_query):
    food = ingredient.get("mealie_food_name")
    line = ingredient.get("name") or food or ""
    base = {"food": food, "line": line}

    if not food:
        if is_pantry_line(line):
            return {**base, "status": "basisvare"}
        return {**base, "status": "mangler", "reason": "ikke parset i Mealie"}

    entry = resolve_entry(food, catalog)
    unit = (ingredient.get("unit") or "").strip().lower() or None
    unit_override = entry.get("units", {}).get(unit or "", {})

    if entry.get("ignore"):
        return {**base, "status": "udeladt", "reason": entry.get("note")}
    if entry.get("pantry") or unit_override.get("pantry"):
        return {**base, "status": "basisvare"}

    need, note = pricing.resolve_need(ingredient.get("quantity"), unit, entry)
    if need is None:
        status = "udeladt" if note == "ingen mængde i opskriften" else "mangler"
        return {**base, "status": status, "reason": note}

    # Bouillon: liter bouillon -> antal terninger.
    if need["dim"] == "ml" and entry.get("yield_ml_per_piece"):
        need = {"dim": "stk", "amount": need["amount"] / float(entry["yield_ml_per_piece"])}

    products = products_by_query.get(entry["query"])
    if products is None:
        return {**base, "status": "mangler", "reason": "søgning fejlede", "need": pricing.format_amount(need)}

    product, candidates = pricing.choose_product(products, entry, need["dim"])
    if not product:
        return {
            **base,
            "status": "mangler",
            "reason": "ingen passende vare" if not candidates else "kunne ikke omregne mængde",
            "need": pricing.format_amount(need),
            "search_top": [p.get("name") for p in products[:5]],
        }

    priced = pricing.price_need(need, product, entry)
    if not priced:
        return {**base, "status": "mangler", "reason": "kunne ikke omregne mængde",
                "need": pricing.format_amount(need), "product": product.get("name")}

    return {
        **base,
        "status": "ok",
        "need": pricing.format_amount(need),
        "product": product.get("name"),
        "package": product.get("description"),
        "price": product.get("price"),
        "offer": product.get("offer"),
        "prismatch": product.get("prismatch", False),
        "packages": priced["packages"],
        "basket": priced["basket"],
        "usage": priced["usage"],
        "approx": bool(priced["approx"] or note or entry.get("_auto")),
        "auto_matched": bool(entry.get("_auto")),
        "alternatives": candidates - 1,
        "url": product.get("url"),
    }


def price_recipe(recipe, catalog, products_by_query):
    lines = [
        price_ingredient(ingredient, catalog, products_by_query)
        for ingredient in recipe.get("main_ingredients", [])
    ]

    ok = [l for l in lines if l["status"] == "ok"]

    # Kurvpris: samme vare på flere linjer (fx 2 x løg) lægges sammen
    # som én vare, så vi ikke køber to pakker, hvis én er nok.
    basket_total = 0.0
    by_product = {}
    for line in ok:
        by_product.setdefault(line["url"] or line["product"], []).append(line)
    for group in by_product.values():
        if len(group) == 1:
            basket_total += group[0]["basket"]
            continue
        usage = sum(l["usage"] for l in group)
        per_package = group[0]["basket"] / group[0]["packages"]
        packages = max(1, math.ceil(round(usage / per_package, 6)))
        basket_total += min(sum(l["basket"] for l in group), packages * per_package)

    usage_total = sum(l["usage"] for l in ok)
    servings = recipe.get("servings")
    servings_assumed = not servings
    servings = servings or DEFAULT_SERVINGS

    missing = [l for l in lines if l["status"] == "mangler"]

    return {
        "name": recipe.get("name"),
        "slug": recipe.get("mealie_slug"),
        "servings": servings,
        "servings_assumed": servings_assumed,
        "basket_total": round(basket_total, 2),
        "usage_total": round(usage_total, 2),
        "basket_per_portion": round(basket_total / servings, 2),
        "usage_per_portion": round(usage_total / servings, 2),
        "priced": len(ok),
        "missing": [l["food"] or l["line"] for l in missing],
        "approx": any(l.get("approx") for l in ok),
        "prismatch_count": sum(1 for l in ok if l.get("prismatch")),
        "offer_count": sum(1 for l in ok if l.get("offer")),
        "ingredients": lines,
    }


def compute(recipes_data, catalog, products_by_query):
    priced = [price_recipe(r, catalog, products_by_query) for r in recipes_data.get("recipes", [])]
    # Billigst først pr. portion; retter med manglende varer nederst,
    # da deres pris ellers ser for lav ud.
    priced.sort(key=lambda r: (bool(r["missing"]), r["basket_per_portion"]))
    return priced


def collect_queries(recipes_data, catalog):
    queries = set()
    for recipe in recipes_data.get("recipes", []):
        for ingredient in recipe.get("main_ingredients", []):
            food = ingredient.get("mealie_food_name")
            if not food:
                continue
            entry = resolve_entry(food, catalog)
            if entry.get("pantry") or entry.get("ignore"):
                continue
            queries.add(entry["query"])
    return queries


def write_json(path, payload):
    tmp = Path(path).with_suffix(Path(path).suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def run(driver, recipes_data):
    """Prissæt alle retter med en åben browser på nemlig.com."""
    catalog = load_catalog()
    queries = collect_queries(recipes_data, catalog)

    cached = load_cache()
    products_by_query, errors = fetch_products(driver, queries, cached)

    if len(products_by_query) > len(cached):
        write_json(CACHE_FILE, {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "queries": products_by_query,
        })

    recipes = compute(recipes_data, catalog, products_by_query)
    now = datetime.now(timezone.utc).isoformat()

    summary = {
        "updated_at": now,
        "recipe_count": len(recipes),
        "search_errors": len(errors),
        "cheapest": recipes[0]["name"] if recipes else None,
        "recipes": [{k: v for k, v in r.items() if k != "ingredients"} for r in recipes],
    }
    details = {"updated_at": now, "search_errors": errors, "recipes": recipes}

    write_json(OUTPUT_FILE, summary)
    write_json(DETAILS_FILE, details)

    LOGGER.info("Prissatte %s retter ud fra %s søgninger.", len(recipes), len(products_by_query))
    if recipes:
        top = recipes[0]
        LOGGER.info("Billigst: %s — %.2f kr./portion (kurv).", top["name"], top["basket_per_portion"])
    if errors and len(errors) == len(queries):
        raise RuntimeError("Alle søgninger hos Nemlig fejlede.")
    return summary


if __name__ == "__main__":
    # Manuel kørsel i add-on'en: python3 /app/price_recipes.py
    import browser

    data = json.loads(RECIPES_FILE.read_text(encoding="utf-8"))
    web = browser.create_driver()
    try:
        browser.open_nemlig(web)
        run(web, data)
    finally:
        web.quit()

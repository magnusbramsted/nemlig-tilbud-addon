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
    else:
        entry = {"query": key, "require": [key], "_key": key, "_auto": True}
    # "query" kan være ét søgeord eller en liste; varerne fra alle lægges sammen.
    queries = entry["query"] if isinstance(entry["query"], list) else [entry["query"]]
    entry["_queries"] = [q for q in queries if q]
    return entry


def products_for(entry, products_by_query):
    """Saml varer fra alle søgeord (uden dubletter). None, hvis alle søgninger fejlede."""
    found, seen, any_ok = [], set(), False
    for query in entry["_queries"]:
        products = products_by_query.get(query)
        if products is None:
            continue
        any_ok = True
        for product in products:
            key = product.get("id") or product.get("name")
            if key not in seen:
                seen.add(key)
                found.append(product)
    return found if any_ok else None


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


def need_key(ingredient, catalog):
    """(katalognøgle, enhed, mængde) for en ingrediens, eller None hvis den ikke købes."""
    food = ingredient.get("mealie_food_name")
    if not food:
        return None
    entry = resolve_entry(food, catalog)
    unit = (ingredient.get("unit") or "").strip().lower() or None
    if entry.get("ignore") or entry.get("pantry") or entry.get("units", {}).get(unit or "", {}).get("pantry"):
        return None
    need, _note = pricing.resolve_need(ingredient.get("quantity"), unit, entry)
    if need is None:
        return None
    if need["dim"] == "ml" and entry.get("yield_ml_per_piece"):
        need = {"dim": "stk", "amount": need["amount"] / float(entry["yield_ml_per_piece"])}
    return entry["_key"], need["dim"], need["amount"]


def price_ingredient(ingredient, catalog, products_by_query, totals=None):
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

    products = products_for(entry, products_by_query)
    if products is None:
        return {**base, "status": "mangler", "reason": "søgning fejlede", "need": pricing.format_amount(need)}

    # Varen vælges ud fra rettens samlede behov for ingrediensen (fx 50 g + 50 g),
    # så en større pakke kan vinde over flere små.
    choose_need = need
    total = (totals or {}).get((entry["_key"], need["dim"]))
    if total and total > need["amount"]:
        choose_need = {"dim": need["dim"], "amount": total}
    product, usage_product, candidates = pricing.choose_products(products, entry, choose_need)
    if product and not pricing.price_need(need, product, entry):
        product, usage_product, candidates = pricing.choose_products(products, entry, need)
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

    usage = pricing.usage_cost(need, usage_product, entry)
    if usage is None:
        usage = priced["usage"]

    line = {
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
        "usage": min(usage, priced["basket"]),
        "approx": bool(priced["approx"] or note or entry.get("_auto")),
        "auto_matched": bool(entry.get("_auto")),
        "alternatives": candidates - 1,
        "key": entry["_key"],
        "url": product.get("url"),
    }
    # Bruges til at regne fælles pakker, når samme vare optræder på flere linjer
    # (i én ret her, og på tværs af ugens retter i madplanen).
    line["_reprice"] = (need, product, entry)
    size = 1.0 if need["dim"] == "pakning" else pricing.package_size_in(product, need["dim"], entry)[0]
    if size:
        line["qty"] = [round(need["amount"], 3), need["dim"], round(size, 3)]
    alt = None if entry.get("product_id") else pricing.larger_alternative(products, entry, need, product)
    if alt and alt[2]:
        line["alt"] = [alt[0].get("name"), alt[1], round(alt[2], 3)]
    if usage_product is not product:
        # Forbrugsprisen er regnet på en anden (større) vare med lavere kilopris.
        line["usage_product"] = usage_product.get("name")
        line["usage_package"] = usage_product.get("description")
    return line


def combined_basket(group):
    """Kurvpris for flere linjer med samme vare: samlet mængde i hele pakker."""
    needs = [l.get("_reprice") for l in group]
    if all(needs) and len({n[0]["dim"] for n in needs}) == 1:
        need, product, entry = needs[0]
        total = {"dim": need["dim"], "amount": sum(n[0]["amount"] for n in needs)}
        priced = pricing.price_need(total, product, entry)
        if priced:
            return {"basket": priced["basket"], "packages": priced["packages"]}
    # Forskellige enheder: køb det, hver linje kræver for sig (sikker side).
    first = group[0]
    per_package = first["basket"] / first["packages"]
    packages = sum(l["packages"] for l in group)
    return {"basket": round(packages * per_package, 2), "packages": packages}


def price_recipe(recipe, catalog, products_by_query):
    totals = {}
    for ingredient in recipe.get("main_ingredients", []):
        found = need_key(ingredient, catalog)
        if found:
            totals[found[:2]] = totals.get(found[:2], 0.0) + found[2]
    lines = [
        price_ingredient(ingredient, catalog, products_by_query, totals)
        for ingredient in recipe.get("main_ingredients", [])
    ]

    ok = [l for l in lines if l["status"] == "ok"]

    # Kurvpris: samme vare på flere linjer (fx 25 g + 15 g parmesan) købes som én
    # samlet mængde. Antal pakker regnes ud fra de samlede mængder og pakkestørrelsen,
    # ikke ud fra forbrugsprisen (den kan være regnet på en anden, billigere vare).
    basket_total = 0.0
    by_product = {}
    for line in ok:
        by_product.setdefault(line["url"] or line["product"], []).append(line)
    for group in by_product.values():
        if len(group) > 1:
            group_basket = combined_basket(group)
            group[0]["basket"] = group_basket["basket"]
            group[0]["packages"] = group_basket["packages"]
            group[0]["shared_lines"] = len(group)
            for extra in group[1:]:
                extra["basket"] = 0.0
                extra["usage"] = min(extra["usage"], group_basket["basket"])
                extra["same_package"] = True
        basket_total += sum(l["basket"] for l in group)
    for line in lines:
        line.pop("_reprice", None)

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
    # Laveste forbrugspris pr. portion først; retter med manglende varer nederst,
    # da deres pris ellers ser for lav ud.
    priced.sort(key=lambda r: (bool(r["missing"]), r["usage_per_portion"]))
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
            queries.update(entry["_queries"])
    return queries


def compact_line(line):
    """Kort udgave af en ingredienslinje til dashboardet (HA-attributter skal holdes små)."""
    if line["status"] != "ok":
        return {"food": line.get("food") or line.get("line"), "missing": True}
    compact = {
        "food": line["food"],
        "need": line["need"],
        "product": line["product"],
        "basket": line["basket"],
        "usage": line["usage"],
        "key": line.get("key"),
    }
    if line.get("packages", 1) > 1:
        compact["packages"] = line["packages"]
    if line.get("qty"):
        compact["qty"] = line["qty"]
    if line.get("alt"):
        compact["alt"] = line["alt"]
    if line.get("same_package"):
        compact["same_package"] = True
    if line.get("shared_lines"):
        compact["shared_lines"] = line["shared_lines"]
    if line.get("prismatch"):
        compact["prismatch"] = True
    if line.get("offer"):
        compact["offer"] = line["offer"]
    return compact


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
        "recipes": [
            {**{k: v for k, v in r.items() if k != "ingredients"},
             "lines": [compact_line(l) for l in r["ingredients"] if l["status"] in ("ok", "mangler")]}
            for r in recipes
        ],
    }
    details = {"updated_at": now, "search_errors": errors, "recipes": recipes}

    write_json(OUTPUT_FILE, summary)
    write_json(DETAILS_FILE, details)

    LOGGER.info("Prissatte %s retter ud fra %s søgninger.", len(recipes), len(products_by_query))
    if recipes:
        top = recipes[0]
        LOGGER.info("Billigst: %s — %.2f kr./portion (forbrug).", top["name"], top["usage_per_portion"])
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

"""Prisberegning for opskrifter ud fra Nemlig-produkter.

Rene funktioner uden netværk, så de kan testes isoleret.

Begreber
--------
need      : hvad en opskrift kræver, normaliseret til én dimension:
            {"dim": "g" | "ml" | "stk", "amount": float}
package   : hvad en vare indeholder: {"g": .., "ml": .., "stk": ..} (en eller flere)
product   : normaliseret Nemlig-vare (se nemlig_soeg.normalise_product):
            price (aktuel stykpris), mix {"min_qty", "total"} | None,
            package, prismatch, offer, name, url ...
"""

import math
import re

# --- Enheder fra Mealie -------------------------------------------------------

# Faste omregninger til en basisdimension.
UNIT_TABLE = {
    "gram": ("g", 1.0),
    "g": ("g", 1.0),
    "kilogram": ("g", 1000.0),
    "kg": ("g", 1000.0),
    "milliliter": ("ml", 1.0),
    "ml": ("ml", 1.0),
    "centiliter": ("ml", 10.0),
    "deciliter": ("ml", 100.0),
    "dl": ("ml", 100.0),
    "liter": ("ml", 1000.0),
    "l": ("ml", 1000.0),
    "spiseske": ("ml", 15.0),
    "spsk": ("ml", 15.0),
    "teske": ("ml", 5.0),
    "tsk": ("ml", 5.0),
    "knivspids": ("ml", 0.5),
    "dryp": ("ml", 0.5),
    "sprøjt": ("ml", 2.0),
    "skvæt": ("ml", 5.0),
    "stk": ("stk", 1.0),
    "stk.": ("stk", 1.0),
    None: ("stk", 1.0),
    "": ("stk", 1.0),
}

# Enheder, der kun giver mening pr. fødevare (kataloget skal angive dem).
# Uden katalog-oplysning tolkes de som "1 pakning af den valgte vare".
FOOD_SPECIFIC_UNITS = {
    "fed", "håndfulde", "håndfuld", "dåse", "pakke", "pose", "portion",
    "stilk", "stængler", "stængel", "bundt", "glas",
}

PACKAGE_UNITS = {"dåse", "pakke", "pose", "portion", "glas"}


def resolve_need(quantity, unit, food_entry):
    """
    Omsæt (mængde, Mealie-enhed) til et behov i g, ml, stk eller pakninger.

    Returnerer (need, note). need er None, hvis behovet ikke kan bestemmes.
    """
    if quantity is None:
        quantity = 1.0
    try:
        quantity = float(quantity)
    except (TypeError, ValueError):
        return None, "ukendt mængde"
    if quantity <= 0:
        return None, "ingen mængde i opskriften"

    unit_key = (unit or "").strip().lower() or None
    overrides = (food_entry or {}).get("units", {})

    if unit_key in overrides:
        spec = overrides[unit_key]
        for dim in ("g", "ml", "stk"):
            if dim in spec:
                return {"dim": dim, "amount": quantity * float(spec[dim])}, None

    if unit_key in UNIT_TABLE:
        dim, factor = UNIT_TABLE[unit_key]
        return {"dim": dim, "amount": quantity * factor}, None

    if unit_key in PACKAGE_UNITS or unit_key in FOOD_SPECIFIC_UNITS:
        return {"dim": "pakning", "amount": quantity}, (
            f"'{unit_key}' tolket som hel pakning"
        )

    return None, f"ukendt enhed '{unit_key}'"


# --- Pakningsstørrelse fra Nemlig ---------------------------------------------

_NUM = r"(\d+(?:[.,]\d+)?)"
_SIZE_RE = re.compile(
    _NUM + r"(?:\s*-\s*" + _NUM + r")?\s*"
    r"(kg|g|gr|ltr|l|cl|dl|ml|stk|bdt|bundt|breve)\b\.?",
    re.IGNORECASE,
)

_UNIT_TO_DIM = {
    "kg": ("g", 1000.0), "g": ("g", 1.0), "gr": ("g", 1.0),
    "l": ("ml", 1000.0), "ltr": ("ml", 1000.0), "cl": ("ml", 10.0),
    "dl": ("ml", 100.0), "ml": ("ml", 1.0),
    "stk": ("stk", 1.0), "bdt": ("stk", 1.0), "bundt": ("stk", 1.0),
    "breve": ("stk", 1.0),
}


def _to_float(text):
    return float(str(text).replace(",", "."))


def parse_package(description):
    """
    '300-350 g'             -> {'g': 325}
    '2 stk. / 350 g'        -> {'g': 350, 'stk': 2}
    '0,75 l / rødvin'       -> {'ml': 750}
    '390 g / 230 g / ...'   -> {'g': 390}   (første vægt = brutto)
    '4 g / 225 stk.'        -> {'g': 900, 'stk': 225}   (vægt pr. stk.)
    """
    package = {}
    for match in _SIZE_RE.finditer(str(description or "")):
        low = _to_float(match.group(1))
        high = _to_float(match.group(2)) if match.group(2) else low
        dim, factor = _UNIT_TO_DIM[match.group(3).lower()]
        if dim not in package:
            package[dim] = (low + high) / 2 * factor

    # Små stykvægte med mange stk. er vægt pr. styk (fx sukker-sticks).
    if package.get("stk", 0) >= 20 and package.get("g", 0) and package["g"] < 50:
        package["g"] = package["g"] * package["stk"]

    return package


def package_from_unit_price(price, unit_price, unit_label):
    """Bagudregn pakningsstørrelse ud fra kr./kg, kr./ltr eller kr./stk."""
    try:
        price = float(price)
        unit_price = float(unit_price)
    except (TypeError, ValueError):
        return {}
    if price <= 0 or unit_price <= 0:
        return {}
    label = str(unit_label or "").lower()
    ratio = price / unit_price
    if "kg" in label:
        return {"g": round(ratio * 1000, 1)}
    if "l" in label.replace("kr.", "").replace("kr", ""):
        return {"ml": round(ratio * 1000, 1)}
    if "stk" in label:
        return {"stk": round(ratio, 2)}
    return {}


# --- Omregning mellem dimensioner ---------------------------------------------

def convert_amount(amount, from_dim, to_dim, food_entry):
    """Omregn mellem g, ml og stk via fødevarens densitet og stykvægt."""
    if from_dim == to_dim:
        return amount, False

    entry = food_entry or {}
    density = entry.get("density")          # g pr. ml
    g_per_piece = entry.get("g_per_piece")  # g pr. stk

    grams = None
    if from_dim == "g":
        grams = amount
    elif from_dim == "ml":
        grams = amount * (density if density else 1.0)
    elif from_dim == "stk" and g_per_piece:
        grams = amount * g_per_piece

    if grams is None:
        return None, False

    approx = from_dim == "ml" and not density
    if to_dim == "g":
        return grams, approx
    if to_dim == "ml":
        return grams / (density if density else 1.0), approx or not density
    if to_dim == "stk" and g_per_piece:
        return grams / g_per_piece, approx
    return None, False


def package_size_in(product, dim, food_entry):
    """Pakningens indhold udtrykt i behovets dimension."""
    package = product.get("package") or {}
    if dim in package:
        return package[dim], False
    for source_dim in ("g", "ml", "stk"):
        if source_dim in package:
            value, approx = convert_amount(package[source_dim], source_dim, dim, food_entry)
            if value:
                return value, approx
    return None, False


# --- Priser -------------------------------------------------------------------

def cost_for_packages(product, count):
    """Billigste pris for mindst `count` pakninger inkl. mix-tilbud.

    Returnerer (pris, antal_købt). Ved mix kan det betale sig at købe op til
    mix-antallet, fx 2 stk. 40,- mod 1 stk. 21,95.
    """
    price = float(product["price"])
    mix = product.get("mix") or None
    if not mix or not mix.get("min_qty") or mix.get("total") is None:
        return round(price * count, 2), count

    min_qty = int(mix["min_qty"])
    total = float(mix["total"])

    best = None
    for bought in range(count, count + min_qty):
        cost = (bought // min_qty) * total + (bought % min_qty) * price
        if best is None or cost < best[0] - 1e-9:
            best = (cost, bought)
    return round(best[0], 2), best[1]


def unit_price(product, dim, food_entry):
    """Laveste opnåelige pris pr. g/ml/stk (til rangering af varer)."""
    size, _ = package_size_in(product, dim, food_entry)
    if not size:
        return None
    price = float(product["price"])
    candidates = [price / size]
    mix = product.get("mix") or None
    if mix and mix.get("min_qty") and mix.get("total") is not None:
        candidates.append(float(mix["total"]) / (int(mix["min_qty"]) * size))
    return min(candidates)


def price_need(need, product, food_entry):
    """
    Kurvpris og forbrugspris for ét behov med én vare.

    kurvpris    : hele pakninger, der skal i kurven
    forbrugspris: kun den mængde, retten bruger
    """
    if need["dim"] == "pakning":
        basket, bought = cost_for_packages(product, math.ceil(need["amount"]))
        return {
            "packages": bought,
            "basket": basket,
            "usage": round(basket / bought * need["amount"], 2),
            "approx": True,
        }

    size, approx = package_size_in(product, need["dim"], food_entry)
    if not size:
        return None

    packages = max(1, math.ceil(need["amount"] / size - 1e-9))
    basket, bought = cost_for_packages(product, packages)
    per_unit = basket / (bought * size)
    return {
        "packages": bought,
        "basket": basket,
        "usage": round(need["amount"] * per_unit, 2),
        "approx": approx,
    }


# --- Valg af vare --------------------------------------------------------------

_FLAVOUR_SPLIT = re.compile(r"\s(?:m\.|med|smag|à la)\s", re.IGNORECASE)


def _normalise(text):
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def name_head(name):
    """'Mini ciabatta m. rosmarin' -> 'mini ciabatta'."""
    return _FLAVOUR_SPLIT.split(_normalise(name), maxsplit=1)[0]


def word_starts(term, text):
    """Matcher term i starten af et ord (dansk sammensætning tillades bagud)."""
    pattern = r"(?<![\wæøå])" + re.escape(term.lower())
    return re.search(pattern, text) is not None


def word_exact(term, text):
    pattern = r"(?<![\wæøå])" + re.escape(term.lower()) + r"(?![\wæøå])"
    return re.search(pattern, text) is not None


def term_in(term, text):
    """
    Danske sammensætninger: 'eddike' skal findes i 'lagereddike' og
    'dressing' i 'hvidløgsdressing'. Korte ord (under 4 tegn) matches kun
    i starten af et ord, så 'is' ikke rammer 'frisk' og 'ost' ikke 'frost'.
    """
    term = term.lower()
    if len(term) >= 4:
        return term in text
    return word_starts(term, text)


# Kategorier, der aldrig er rå ingredienser, medmindre kataloget tillader dem.
EXCLUDED_CATEGORY_KEYWORDS = [
    "chips", "slik", "snack", "faerdig", "pleje", "husholdning", "baby",
    "dyremad", "dyrefoder", "kiosk", "blomster", "koedpaalaeg",
    "fiskepaalaeg", "vinogspiritus", "drikkevarer",
]


def _fold(text):
    text = _normalise(text)
    for old, new in (("æ", "ae"), ("ø", "oe"), ("å", "aa")):
        text = text.replace(old, new)
    return re.sub(r"[^a-z0-9]", "", text)


def product_matches(product, entry):
    """Afgør om en Nemlig-vare kan bruges som fødevaren i kataloget."""
    if not product.get("available", True):
        return False
    if product.get("price") in (None, 0):
        return False

    head = name_head(product.get("name"))
    fields = entry.get("match_in", ["name"])
    haystack = " ".join(
        head if field == "name" else _normalise(product.get(field))
        for field in fields
    )

    require_all = [t.lower() for t in entry.get("require", [])]
    require_any = [t.lower() for t in entry.get("require_any", [])]
    exact = entry.get("exact", False)
    matcher = word_exact if exact else term_in

    if require_all and not all(matcher(t, haystack) for t in require_all):
        return False
    if require_any and not any(matcher(t, haystack) for t in require_any):
        return False

    full_text = _normalise(" ".join(
        str(product.get(f) or "") for f in ("name", "description")
    ))
    if any(term_in(word, full_text) for word in entry.get("exclude", [])):
        return False

    if not entry.get("allow_any_category"):
        category_text = _fold(" ".join(
            str(product.get(f) or "") for f in ("category", "subcategory", "main_group")
        ))
        allowed = [_fold(c) for c in entry.get("categories", [])]
        if allowed:
            if not any(a in category_text for a in allowed):
                return False
        elif any(k in category_text for k in EXCLUDED_CATEGORY_KEYWORDS):
            return False

    dims = entry.get("dims")
    if dims and not any(d in (product.get("package") or {}) for d in dims):
        return False

    return True


def choose_products(products, entry, need):
    """
    Vælg varer til et behov. Returnerer (kurvvare, forbrugsvare, antal_kandidater).

    kurvvare    : billigste måde at få nok (fx 0,75 l rødvin frem for 3 l papvin,
                  når retten bruger 1 dl)
    forbrugsvare: laveste pris pr. kg/l/stk. (det, mængden "egentlig" koster)
    """
    candidates = [p for p in products if product_matches(p, entry)]
    locked = entry.get("product_id")
    if locked:
        locked_items = [p for p in candidates if str(p.get("id")) == str(locked)]
        if locked_items:
            return locked_items[0], locked_items[0], len(candidates)

    scored = []
    for product in candidates:
        priced = price_need(need, product, entry)
        if not priced:
            continue
        if need["dim"] == "pakning":
            per_unit = float(product["price"])
        else:
            per_unit = unit_price(product, need["dim"], entry)
        scored.append((per_unit, priced["basket"], product))

    if not scored:
        return None, None, len(candidates)

    cheapest_basket = min(scored, key=lambda s: (s[1], s[0]))[2]
    cheapest_unit = min(scored, key=lambda s: (s[0], s[1]))[2]
    return cheapest_basket, cheapest_unit, len(candidates)


def usage_cost(need, product, food_entry):
    """Præcis mængde x laveste opnåelige pris pr. enhed (inkl. mix-tilbud)."""
    if need["dim"] == "pakning":
        per_package = float(product["price"])
        mix = product.get("mix") or None
        if mix and mix.get("min_qty") and mix.get("total") is not None:
            per_package = min(per_package, float(mix["total"]) / int(mix["min_qty"]))
        return round(per_package * need["amount"], 2)
    per_unit = unit_price(product, need["dim"], food_entry)
    return None if per_unit is None else round(per_unit * need["amount"], 2)


def format_amount(need):
    dim, amount = need["dim"], need["amount"]
    if dim == "g" and amount >= 1000:
        return f"{amount / 1000:g} kg"
    if dim == "ml" and amount >= 1000:
        return f"{amount / 1000:g} l"
    if dim == "ml" and amount >= 100:
        return f"{amount / 100:g} dl"
    if dim == "pakning":
        return f"{amount:g} pakn."
    return f"{round(amount, 1):g} {dim}"

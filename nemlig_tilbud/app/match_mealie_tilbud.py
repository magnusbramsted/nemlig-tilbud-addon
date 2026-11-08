import json
import re
from datetime import datetime, timezone
from pathlib import Path

OFFERS_FILE = Path("/share/nemlig_tilbud.json")
RECIPES_FILE = Path("/share/mealie_opskrifter.json")
RULES_FILE = Path("/share/ingredient_match_rules.json")
OUTPUT_FILE = Path("/share/nemlig_opskriftsmatches.json")

# Produkttyper, der aldrig må tolkes som rå ingredienser i en opskrift.
# Fx skal "Kyllingeskiver m. bacon" ikke tælle som bacon.
# Kategori/underkategori-par fra den gamle Nemlig-kilde.
GLOBAL_EXCLUDED_CATEGORIES = {
    ("køl", "pålæg i skiver"),
}

# Hele kategorier, der aldrig må tolkes som en rå opskriftsingrediens.
# Den nye Nemlig-side bruger bl.a. "Paalaeg" som kategori.
GLOBAL_EXCLUDED_CATEGORY_NAMES = {
    "paalaeg",
    "pålæg",
}


def alias_matches(alias, text):
    """Matcher alias som hele ord eller hele udtryk."""
    pattern = r"(?<!\w)" + re.escape(alias.lower()) + r"(?!\w)"
    return re.search(pattern, text.lower()) is not None


def field_text(product, field_name):
    """Returnerer tekst fra ét godkendt produktfelt."""
    value = product.get(field_name, "")

    if isinstance(value, list):
        return " ".join(str(item).lower() for item in value)

    return str(value or "").lower()


def offer_is_globally_excluded(offer):
    """Afvis produktkategorier, som ikke er rå ingredienser."""
    category = str(offer.get("category") or "").strip().lower()
    subcategory = str(offer.get("subcategory") or "").strip().lower()

    return (
        category in GLOBAL_EXCLUDED_CATEGORY_NAMES
        or (category, subcategory) in GLOBAL_EXCLUDED_CATEGORIES
    )


def offer_matches_aliases(offer, aliases, match_fields):
    """Matcher mindst ét alias i mindst ét tilladt produktfelt."""
    for field_name in match_fields:
        text = field_text(offer, field_name)
        if any(alias_matches(alias, text) for alias in aliases):
            return True
    return False


def offer_is_in_allowed_categories(offer, allowed_categories):
    """
    Returnerer True, hvis tilbuddet er i en tilladt kategori.
    En tom liste betyder, at alle kategorier fortsat er tilladt.
    """
    if not allowed_categories:
        return True

    category = str(offer.get("category") or "").strip().lower()

    return category in {
        str(item).strip().lower()
        for item in allowed_categories
    }


# --- Generiske matchregler (gælder alle ingredienser) ---

# Kategorier med forarbejdede varer eller non-food.
# En rå opskriftsingrediens matches aldrig herinde,
# medmindre en regel udtrykkeligt sætter allowed_categories.
PROCESSED_CATEGORY_KEYWORDS = [
    "chips", "slik", "snacks", "paalaeg", "faerdigmad",
    "drikkevarer", "vinogspiritus", "pleje", "husholdning",
    "baby", "dyremad", "kiosk", "blomster",
]

# Alt efter disse ord i et produktnavn er smag/tilbehør, ikke selve varen.
FLAVOUR_SPLIT_PATTERN = re.compile(r"\s(?:m\.|med|smag|à la)\s", re.IGNORECASE)


def normalise_category(value):
    value = str(value or "").lower()
    for old, new in (("æ", "ae"), ("ø", "oe"), ("å", "aa")):
        value = value.replace(old, new)
    return re.sub(r"[^a-z0-9]", "", value)


def offer_is_processed_category(offer):
    """True hvis varen ligger i en forarbejdet/non-food-kategori."""
    for field in ("category", "subcategory", "subsubcategory"):
        value = normalise_category(offer.get(field))
        if any(keyword in value for keyword in PROCESSED_CATEGORY_KEYWORDS):
            return True
    return False


def product_head(name):
    """'Mini ciappa m. rosmarin' -> 'mini ciappa'."""
    return FLAVOUR_SPLIT_PATTERN.split(str(name or ""), maxsplit=1)[0].lower()


def passes_generic_checks(offer, aliases, match_fields, allowed_categories):
    """Generiske kvalitetstjek før et tilbud må matche en ingrediens."""
    # 1) Forarbejdede kategorier afvises, medmindre reglen åbner for dem.
    if not allowed_categories and offer_is_processed_category(offer):
        return False

    # 2) Ved navnematch skal ingrediensen stå i varens hoveddel,
    #    ikke kun i smagsdelen efter "m." / "med".
    if "name" in match_fields:
        head = product_head(offer.get("name"))
        if not any(alias_matches(alias, head) for alias in aliases):
            return False

    return True


def offer_contains_excluded_word(offer, exclude_words):
    """Afviser tilbud, hvis et forbudt ord findes i produktets samlede tekst."""
    if not exclude_words:
        return False

    all_fields = [
        "name",
        "category",
        "subcategory",
        "description",
        "brand",
        "search_terms",
    ]

    combined_text = " ".join(
        field_text(offer, field_name)
        for field_name in all_fields
    )

    return any(
        alias_matches(str(word).lower(), combined_text)
        for word in exclude_words
    )


def get_package_size(description):
    """
    Udtrækker den første del af Nemligs produktbeskrivelse.
    Eksempel: "800 g / frost" bliver til "800 g".
    """
    description = str(description or "").strip()
    if not description:
        return None
    return description.split("/", 1)[0].strip() or None


def calculate_discount_percent(normal_price, campaign_price):
    """Beregner rabatprocent sikkert, hvis begge priser findes."""
    try:
        normal_price = float(normal_price)
        campaign_price = float(campaign_price)
    except (TypeError, ValueError):
        return None

    if normal_price <= 0 or campaign_price < 0:
        return None

    return round(((normal_price - campaign_price) / normal_price) * 100, 1)


def get_rule(ingredient, rules):
    """
    Finder regel efter mealie_food_name først.
    Fald tilbage til første alias eller vist ingrediensnavn.
    """
    lookup_names = [
        ingredient.get("mealie_food_name"),
        *(ingredient.get("aliases") or []),
        ingredient.get("name"),
    ]

    for name in lookup_names:
        if name and str(name).lower() in rules:
            return rules[str(name).lower()]

    return {}


offers_data = json.loads(OFFERS_FILE.read_text(encoding="utf-8"))
recipes_data = json.loads(RECIPES_FILE.read_text(encoding="utf-8"))
rules_data = json.loads(RULES_FILE.read_text(encoding="utf-8"))

matches = []

for recipe in recipes_data.get("recipes", []):
    recipe_matches = []
    score = 0
    potential_offer_savings = 0.0

    seen_food_names = set()
    for ingredient in recipe.get("main_ingredients", []):
        # Samme fødevare må kun tælle én gang pr. ret,
        # selv om den står på flere ingredienslinjer.
        food_key = str(
            ingredient.get("mealie_food_name") or ingredient.get("name") or ""
        ).strip().lower()
        if food_key in seen_food_names:
            continue
        seen_food_names.add(food_key)
        rule = get_rule(ingredient, rules_data)

        aliases = [
            str(alias).lower()
            for alias in rule.get(
                "aliases",
                ingredient.get("aliases") or [],
            )
            if alias
        ]

        # Standard: søg kun i produktnavnet.
        # Det forhindrer fx at "timian" matcher kyllingepålæg med timian.
        match_fields = rule.get("match_fields", ["name"])
        exclude_words = rule.get("exclude_words", [])
        allowed_categories = rule.get("allowed_categories", [])

        priority = rule.get(
            "priority",
            ingredient.get("priority", "secondary"),
        )
        selection = rule.get("selection", "highest_savings")

        if not aliases:
            continue

        matching_offers = []

        for offer in offers_data.get("offers", []):
            if (
                not offer_is_globally_excluded(offer)
                and offer_is_in_allowed_categories(offer, allowed_categories)
                and not offer_contains_excluded_word(offer, exclude_words)
                and offer_matches_aliases(offer, aliases, match_fields)
                and passes_generic_checks(
                    offer, aliases, match_fields, allowed_categories
                )
            ):
                campaign = offer.get("campaign") or {}

                matching_offers.append(
                    {
                        "ingredient": ingredient.get("name"),
                        "mealie_food_name": ingredient.get("mealie_food_name"),
                        "priority": priority,
                        "matched_on_fields": match_fields,
                        "offer_name": offer.get("name"),
                        "brand": offer.get("brand"),
                        "category": offer.get("category"),
                        "subcategory": offer.get("subcategory"),
                        "description": offer.get("description"),
                        "offer_types": offer.get("offer_types") or [],
                        "package_size": get_package_size(offer.get("description")),
                        "normal_price": offer.get("normal_price"),
                        "normal_unit_price": offer.get("normal_unit_price"),
                        "campaign_price": campaign.get("price"),
                        "campaign_unit_price": campaign.get("unit_price"),
                        "unit_label": offer.get("unit_label"),
                        "discount_percent": campaign.get("discount_percent"),
                        "campaign_condition": campaign.get("condition"),
                        "savings": campaign.get("savings"),
                        "url": offer.get("url"),
                    }
                )

        if matching_offers:
            if selection == "lowest_unit_price":
                offers_with_unit_price = [
                    item for item in matching_offers
                    if item.get("campaign_unit_price") is not None
                ]

                if offers_with_unit_price:
                    best_offer = min(
                        offers_with_unit_price,
                        key=lambda item: item["campaign_unit_price"],
                    )
                else:
                    best_offer = max(
                        matching_offers,
                        key=lambda item: item["savings"] or 0,
                    )
            else:
                best_offer = max(
                    matching_offers,
                    key=lambda item: item["savings"] or 0,
                )
            best_offer["alternative_offer_count"] = len(matching_offers) - 1

            recipe_matches.append(best_offer)

            if priority == "primary":
                score += 100
            else:
                score += 20

            # Dette er produktets samlede kampagnebesparelse,
            # ikke den præcise besparelse for rettens mængde.
            potential_offer_savings += best_offer["savings"] or 0

    if recipe_matches:
        matches.append(
            {
                "recipe": recipe.get("name"),
                "mealie_name": recipe.get("mealie_name"),
                "mealie_id": recipe.get("mealie_id"),
                "score": score,
                "matched_ingredient_count": len(recipe_matches),
                "potential_offer_savings": round(potential_offer_savings, 2),
                "matched_ingredients": recipe_matches,
            }
        )

matches.sort(key=lambda item: item["score"], reverse=True)

result = {
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "offers_updated_at": offers_data.get("updated_at"),
    "recipes_checked": len(recipes_data.get("recipes", [])),
    "recipes_with_matches": len(matches),
    "matches": matches,
}

OUTPUT_FILE.write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"Kontrollerede {result['recipes_checked']} retter.")
print(f"Fandt tilbudsmatch til {result['recipes_with_matches']} retter.")
print(f"Regelstyret Mealie-resultat gemt i {OUTPUT_FILE}.")

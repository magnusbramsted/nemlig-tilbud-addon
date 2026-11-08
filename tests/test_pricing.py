"""Tests af prisberegningen. Kør: python3 -m pytest tests/"""

import json
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "nemlig_tilbud" / "app"
sys.path.insert(0, str(APP))

import pricing  # noqa: E402
import price_recipes  # noqa: E402

# Varer i den nye Nemlig-sides format (priser i øre), modelleret efter
# søgningen "sukker" 8. nov. 2026.
SUGAR_RAW = [
    {"id": "908032", "title": "Sukker", "description": "1 kg", "price": 750,
     "pricePerUnit": {"value": 750, "unitType": "Weight"}, "labelHighlighted": {"type": "PriceMatch"},
     "availability": {"type": "Available"}, "url": "sukker-908032",
     "tracking": {"item_category": "Bagning", "item_category2": "Sukker-soedemiddel-og-sirup"}},
    {"id": "908035", "title": "Sukker", "description": "2 kg / Dansukker", "price": 2195,
     "pricePerUnit": {"value": 1098, "unitType": "Weight"}, "labelHighlighted": {"type": "PriceMatch"},
     "campaignLines": [{"text": "2 stk. 40,-"}], "availability": {"type": "Available"},
     "tracking": {"item_category": "Bagning"}},
    {"id": "5025056", "title": "Perlesukker", "description": "500 g / Dansukker", "price": 1450,
     "pricePerUnit": {"value": 2900, "unitType": "Weight"}, "availability": {"type": "Available"},
     "tracking": {"item_category": "Bagning"}},
    {"id": "5019651", "title": "Rørsukker øko.", "description": "1 kg / Urtekram", "price": 2517,
     "priceOriginal": 4195, "pricePerUnit": {"value": 2517, "unitType": "Weight"},
     "campaignLines": [{"text": "Skarp Pris"}], "availability": {"type": "Available"},
     "tracking": {"item_category": "Bagning"}},
    {"id": "5063200", "title": "Sukker Sticks hvid", "description": "4 g / 225 stk. / Dansukker",
     "price": 7575, "pricePerUnit": {"value": 34, "unitType": "Pieces"},
     "availability": {"type": "Available"}, "tracking": {"item_category": "Bagning"}},
    {"id": "1", "title": "Sukker", "description": "1 kg", "price": 500,
     "pricePerUnit": {"value": 500, "unitType": "Weight"}, "availability": {"type": "SoldOut"},
     "tracking": {"item_category": "Bagning"}},
]


@pytest.fixture
def sugar():
    import nemlig_soeg
    return [nemlig_soeg.normalise_product(p) for p in SUGAR_RAW]


@pytest.fixture
def catalog():
    return price_recipes.load_catalog(user_path="/nonexistent")


def test_parse_package():
    assert pricing.parse_package("300-350 g") == {"g": 325}
    assert pricing.parse_package("2 stk. / 350 g / Friland") == {"stk": 2, "g": 350}
    assert pricing.parse_package("Frankrig / 0,75 l / rødvin") == {"ml": 750}
    assert pricing.parse_package("1,50 kg / tilsat vand") == {"g": 1500}
    assert pricing.parse_package("390 g / 230 g / Urtekram") == {"g": 390}
    assert pricing.parse_package("4 g / 225 stk. / Dansukker") == {"g": 900, "stk": 225}


def test_normalise_offers_and_prismatch(sugar):
    by_id = {p["id"]: p for p in sugar}
    assert by_id["908032"]["prismatch"] is True
    assert by_id["908035"]["mix"] == {"min_qty": 2, "total": 40.0, "text": "2 stk. 40,-"}
    assert by_id["908035"]["price"] == 21.95
    assert by_id["908035"]["package"] == {"g": 2000}
    assert by_id["5019651"]["price"] == 25.17
    assert by_id["5019651"]["offer"] == "Skarp pris"
    assert by_id["5019651"]["prismatch"] is False
    assert by_id["1"]["available"] is False


def test_user_example_52g_sugar(sugar, catalog):
    """52 g sukker, når 1 kg koster 7,50 -> 0,39 kr. forbrug, 7,50 kr. i kurven."""
    entry = price_recipes.resolve_entry("sukker", catalog)
    need = {"dim": "g", "amount": 52}
    product, _ = pricing.choose_product(sugar, entry, "g")
    assert product["id"] == "908032"                   # billigst pr. kg, ikke perle/rør
    result = pricing.price_need(need, product, entry)
    assert result["basket"] == 7.5
    assert result["usage"] == 0.39


def test_mix_offer_buys_up_when_cheaper():
    product = {"price": 21.95, "mix": {"min_qty": 2, "total": 40}}
    assert pricing.cost_for_packages(product, 1) == (21.95, 1)
    assert pricing.cost_for_packages(product, 2) == (40.0, 2)
    assert pricing.cost_for_packages(product, 3) == (61.95, 3)


def test_partial_package_bacon():
    """100 g bacon af en 200 g pakke: hele pakken i kurven, halvdelen i forbrug."""
    product = {"price": 30.0, "package": {"g": 200}}
    result = pricing.price_need({"dim": "g", "amount": 100}, product, {})
    assert result == {"packages": 1, "basket": 30.0, "usage": 15.0, "approx": False}


def test_volume_to_weight_with_density():
    flour = {"price": 10.0, "package": {"g": 1000}}
    entry = {"density": 0.55}
    need, _ = pricing.resolve_need(2, "spiseske", entry)   # 30 ml
    result = pricing.price_need(need, flour, entry)
    assert result["usage"] == round(30 * 0.55 * 10 / 1000, 2)


def test_pieces_vs_weight():
    onions = {"price": 12.0, "package": {"g": 1000}}
    entry = {"g_per_piece": 130}
    need, _ = pricing.resolve_need(2, None, entry)
    result = pricing.price_need(need, onions, entry)
    assert result["basket"] == 12.0
    assert result["usage"] == round(260 * 12 / 1000, 2)


def test_garlic_cloves():
    garlic = {"price": 10.0, "package": {"stk": 3}}       # 3 hoveder
    entry = {"units": {"fed": {"g": 5}}, "g_per_piece": 50}
    need, _ = pricing.resolve_need(4, "fed", entry)
    result = pricing.price_need(need, garlic, entry)
    assert result["basket"] == 10.0
    assert result["usage"] == round(20 / 150 * 10, 2)


def test_exclusions(catalog):
    sugar_entry = price_recipes.resolve_entry("sukker", catalog)
    assert not pricing.product_matches({"name": "Rørsukker øko.", "price": 20, "package": {"g": 1000}}, sugar_entry)
    onion = price_recipes.resolve_entry("løg", catalog)
    assert pricing.product_matches({"name": "Løg gule", "price": 10, "package": {"g": 1000}}, onion)
    assert not pricing.product_matches({"name": "Rødløg", "price": 10, "package": {"g": 1000}}, onion)
    assert not pricing.product_matches({"name": "Ristede løg", "price": 10, "package": {"g": 100}}, onion)
    chicken = price_recipes.resolve_entry("kyllingefilet", catalog)  # same_as
    assert not pricing.product_matches({"name": "Kyllingebryst pålæg", "price": 20, "package": {"g": 100}}, chicken)


def test_recipe_totals_and_pantry(sugar, catalog):
    recipe = {
        "name": "Test", "servings": 2,
        "main_ingredients": [
            {"mealie_food_name": "sukker", "quantity": 52, "unit": "gram", "name": "52 g sukker"},
            {"mealie_food_name": "olivenolie", "quantity": 2, "unit": "spiseske", "name": "2 spsk olie"},
            {"mealie_food_name": "and", "quantity": 3, "unit": "deciliter", "name": "3 dl vand"},
            {"mealie_food_name": None, "name": "salt og peber"},
            {"mealie_food_name": None, "name": "1 bundt magi"},
        ],
    }
    result = price_recipes.price_recipe(recipe, catalog, {"sukker": sugar})
    assert result["basket_total"] == 7.5
    assert result["usage_total"] == 0.39
    assert result["basket_per_portion"] == 3.75
    assert result["missing"] == ["1 bundt magi"]
    statuses = [l["status"] for l in result["ingredients"]]
    assert statuses == ["ok", "basisvare", "udeladt", "basisvare", "mangler"]


def test_catalog_is_valid_json_and_same_as_resolves(catalog):
    for key, entry in catalog.items():
        if "same_as" in entry:
            assert entry["same_as"].lower() in catalog, key

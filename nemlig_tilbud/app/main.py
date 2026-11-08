"""Én kørsel: Mealie-import, tilbudsside og prisberegning af alle retter."""

import json
import logging
import sys
from pathlib import Path

import browser
import mealie_import
import nemlig_tilbudsside
import price_recipes

OPTIONS_FILE = Path("/data/options.json")
RECIPES_FILE = Path("/share/mealie_opskrifter.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
LOGGER = logging.getLogger("nemlig")


def load_options():
    try:
        return json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as err:
        LOGGER.warning("Kunne ikke læse %s (%s). Bruger standardværdier.", OPTIONS_FILE, err)
        return {}


def step(label, function):
    """Kør ét trin; en fejl stopper ikke de øvrige trin."""
    try:
        return function()
    except Exception:
        LOGGER.exception("%s fejlede.", label)
        return None


def load_recipes(options):
    """Hent friske opskrifter fra Mealie; fald tilbage til sidste import."""
    recipes = step(
        "Mealie-import",
        lambda: mealie_import.run(
            options.get("mealie_url") or "http://a0d7b954-mealie:9000",
            str(options.get("mealie_api_token") or "").strip(),
            RECIPES_FILE,
        ),
    )
    if recipes is not None:
        return {"recipes": recipes}
    if RECIPES_FILE.exists():
        LOGGER.warning("Bruger sidste Mealie-import fra %s.", RECIPES_FILE)
        return json.loads(RECIPES_FILE.read_text(encoding="utf-8"))
    return None


def main():
    options = load_options()
    threshold = float(options.get("high_discount_threshold_percent", 50))
    recipes_data = load_recipes(options)

    driver = browser.create_driver()
    try:
        step("Tilbudssiden", lambda: nemlig_tilbudsside.run(driver, threshold))
        if recipes_data and recipes_data.get("recipes"):
            step("Prisberegning", lambda: price_recipes.run(driver, recipes_data))
        else:
            LOGGER.warning("Ingen opskrifter fra Mealie. Springer prisberegning over.")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()

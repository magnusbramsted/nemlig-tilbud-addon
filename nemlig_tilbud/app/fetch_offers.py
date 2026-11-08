import subprocess
import sys
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service


OFFERS_URL = "https://www.nemlig.com/tilbud"
OUTPUT_FILE = Path("/share/nemlig_tilbud.json")
OPTIONS_FILE = Path("/data/options.json")
DEBUG_HTML_FILE = Path("/share/nemlig_debug.html")
DEBUG_SCREENSHOT_FILE = Path("/share/nemlig_debug.png")
DEBUG_NETWORK_FILE = Path("/share/nemlig_network_urls.txt")
TIMEOUT_SECONDS = 90

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",)
LOGGER = logging.getLogger("nemlig_tilbud")


def new_nemlig_source_enabled() -> bool:
    """Læs valg af ny Nemlig-kilde fra add-on'ens indstillinger."""
    try:
        options = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as err:
        LOGGER.warning(
            "Kunne ikke læse %s (%s). Bruger gammel Nemlig-kilde.",
            OPTIONS_FILE,
            err,
        )
        return False

    return bool(options.get("enable_new_nemlig_source", False))


def run_recipe_pipeline():
    """Importér Mealie-opskrifter og kør den valgte matchmotor."""
    if mealie_import_enabled():
        LOGGER.info("Mealie-import er aktiveret.")

        import_ok = run_script(
            "/app/import_mealie_opskrifter.py",
            "Mealie-import",
        )

        if import_ok:
            run_script(
                "/app/match_mealie_tilbud.py",
                "Regelstyret Mealie-opskriftsmatchning",
            )
        else:
            LOGGER.error(
                "Springer Mealie-opskriftsmatchning over, "
                "fordi Mealie-importen fejlede."
            )
    else:
        LOGGER.info(
            "Mealie-import er deaktiveret. "
            "Kører den gamle favoritretter-matcher."
        )
        run_script(
            "/share/match_favoritretter.py",
            "Opskriftsmatchning",
        )


def mealie_import_enabled() -> bool:
    """Læs feature-flag fra add-on'ens egne indstillinger."""
    try:
        options = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        LOGGER.warning(
            "Kunne ikke læse %s. Bruger den gamle favoritretter-matcher.",
            OPTIONS_FILE,
        )
        return False
    except json.JSONDecodeError as err:
        LOGGER.warning(
            "Ugyldig JSON i %s: %s. Bruger den gamle favoritretter-matcher.",
            OPTIONS_FILE,
            err,
        )
        return False

    return bool(options.get("enable_mealie_import", False))


def run_script(script_path: str, label: str) -> bool:
    """Kør et underscript og skriv resultatet til add-on-loggen."""
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as err:
        LOGGER.error("%s kunne ikke startes: %s", label, err)
        return False

    if result.returncode == 0:
        LOGGER.info("%s færdig: %s", label, result.stdout.strip())
        return True

    LOGGER.error(
        "%s fejlede (kode %s): %s",
        label,
        result.returncode,
        result.stderr.strip() or result.stdout.strip(),
    )
    return False


def normalize_product(product: dict) -> dict:
    campaign = product.get("Campaign") or {}
    availability = product.get("Availability") or {}

    return {
        "id": product.get("Id"),
        "name": product.get("Name"),
        "brand": product.get("Brand"),
        "category": product.get("Category"),
        "subcategory": product.get("SubCategory"),
        "description": product.get("Description"),
        "url": (
            f"https://www.nemlig.com/{product.get('Url')}"
            if product.get("Url")
            else None
        ),
        "image": product.get("PrimaryImage"),
        "search_terms": product.get("SearchDescription") or [],
        "labels": product.get("Labels") or [],
        "available_in_stock": availability.get("IsAvailableInStock", False),
        "available_few_in_stock": availability.get("IsAvailableFewInStock", False),
        "available_for_delivery": availability.get("IsDeliveryAvailable", False),
        "normal_price": product.get("Price"),
        "normal_unit_price": product.get("UnitPriceCalc"),
        "unit_label": product.get("UnitPriceLabel"),
        "discount_item": product.get("DiscountItem", False),
        "campaign": {
            "type": campaign.get("Type"),
            "code": campaign.get("Code"),
            "price": campaign.get("CampaignPrice"),
            "unit_price": campaign.get("CampaignUnitPrice"),
            "minimum_quantity": campaign.get("MinQuantity"),
            "maximum_quantity": campaign.get("MaxQuantity"),
            "total_price": campaign.get("TotalPrice"),
            "savings": campaign.get("DiscountSavings"),
            "starts": campaign.get("IntervalStart"),
            "ends": campaign.get("IntervalEnd"),
            "mix_offer": campaign.get("VariousPriceProductsCampaign", False),
        } if campaign else None,
    }


def create_driver():
    options = Options()
    options.binary_location = "/usr/bin/chromium-browser"

    for argument in [
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--window-size=1440,1000",
        "--lang=da-DK",
    ]:
        options.add_argument(argument)

    options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})

    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)


def save_debug_files(driver, urls):
    """Gemmer debug-information lokalt uden tokens, cookies eller headers."""
    try:
        DEBUG_HTML_FILE.write_text(driver.page_source, encoding="utf-8")
        LOGGER.info("Gemte debug-HTML i %s.", DEBUG_HTML_FILE)
    except Exception as err:
        LOGGER.warning("Kunne ikke gemme debug-HTML: %s", err)

    try:
        driver.save_screenshot(str(DEBUG_SCREENSHOT_FILE))
        LOGGER.info("Gemte debug-skærmbillede i %s.", DEBUG_SCREENSHOT_FILE)
    except Exception as err:
        LOGGER.warning("Kunne ikke gemme debug-skærmbillede: %s", err)

    try:
        DEBUG_NETWORK_FILE.write_text(
            "\n".join(sorted(urls)),
            encoding="utf-8",
        )
        LOGGER.info("Gemte %s netværks-URL'er i %s.", len(urls), DEBUG_NETWORK_FILE)
    except Exception as err:
        LOGGER.warning("Kunne ikke gemme netværks-URL'er: %s", err)


def fetch_offers() -> dict:
    driver = create_driver()
    seen_urls = set()

    try:
        # Aktiver CDP-netværksdomænet før siden indlæses.
        driver.execute_cdp_cmd("Network.enable", {})

        LOGGER.info("Åbner Nemligs tilbudsside.")
        driver.get(OFFERS_URL)
        time.sleep(2)

        try:
            cookie_button = driver.find_element(
                "xpath",
                "//*[self::button or self::a][contains(normalize-space(.), 'Kun nødvendige')]",
            )
            driver.execute_script("arguments[0].click();", cookie_button)
            LOGGER.info("Accepterede kun nødvendige cookies.")
            time.sleep(3)
        except Exception as err:
            LOGGER.info("Cookie-dialog blev ikke fundet eller var allerede håndteret: %s", err)

        deadline = time.time() + TIMEOUT_SECONDS
        checked_request_ids = set()

        request_ids = set()

        while time.time() < deadline:
            for entry in driver.get_log("performance"):
                try:
                    message = json.loads(entry["message"])["message"]
                    method = message.get("method")
                    params = message.get("params", {})

                    if method == "Network.responseReceived":
                        response = params.get("response", {})
                        response_url = response.get("url", "")
                        request_id = params.get("requestId")

                        if response_url:
                            seen_urls.add(response_url)

                        if "/Products/GetByProductGroupId" in response_url and request_id:
                            request_ids.add(request_id)
                            LOGGER.info(
                                "Fandt muligt tilbudskald: %s",
                                response_url,
                            )

                    elif (
                        method == "Network.loadingFinished"
                        and params.get("requestId") in request_ids
                    ):
                        request_id = params["requestId"]

                        body = driver.execute_cdp_cmd(
                            "Network.getResponseBody",
                            {"requestId": request_id},
                        ).get("body", "")

                        if not body:
                            LOGGER.warning(
                                "Tilbudskaldet blev færdigt, men returnerede tom body."
                            )
                            continue

                        data = json.loads(body)

                        LOGGER.info(
                            "Læste tilbudssvar med nøgler: %s",
                            list(data.keys()) if isinstance(data, dict) else type(data),
                        )

                        if (
                            isinstance(data, dict)
                            and isinstance(data.get("Products"), list)
                        ):
                            LOGGER.info(
                                "Tilbudslisten blev fanget: %s produkter.",
                                data.get("NumFound", len(data["Products"])),
                            )
                            return data

                except Exception as err:
                    LOGGER.warning(
                        "Netværksfejl under læsning af tilbudskald: %s",
                        err,
                    )
            time.sleep(1)

        save_debug_files(driver, seen_urls)
        raise RuntimeError(
            "Tilbudslisten blev ikke fundet i browserens netværkssvar. "
            "Debug-filer er gemt i /share."
        )

    finally:
        driver.quit()


def main():
    # Ny Nemlig-side er den foretrukne datakilde.
    # Ved fejl fortsætter vi automatisk med den gamle API-kilde.
    if new_nemlig_source_enabled():
        LOGGER.info("Ny Nemlig-kilde er aktiveret.")

        if run_script(
            "/app/fetch_nemlig_ny_side.py",
            "Ny Nemlig-tilbudshentning",
        ):
            run_recipe_pipeline()
            return

        LOGGER.warning(
            "Ny Nemlig-kilde fejlede. Falder tilbage til gammel Nemlig-kilde."
        )

    raw_data = fetch_offers()
    products = raw_data.get("Products", [])

    offers = [
        normalize_product(product)
        for product in products
        if product.get("Campaign")
    ]

    payload = {
        "source": "Nemlig.com",
        "source_url": OFFERS_URL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_products_found": raw_data.get("NumFound", len(products)),
        "offers_saved": len(offers),
        "offers": offers,
    }

    temporary_file = OUTPUT_FILE.with_suffix(".json.tmp")
    temporary_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(OUTPUT_FILE)

    LOGGER.info(
        "Gemte %s tilbud ud af %s fundne produkter i %s.",
        len(offers),
        raw_data.get("NumFound", len(products)),
        OUTPUT_FILE,
    )

    run_recipe_pipeline()

if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        LOGGER.exception("Kunne ikke hente Nemlig-tilbud: %s", err)
        raise

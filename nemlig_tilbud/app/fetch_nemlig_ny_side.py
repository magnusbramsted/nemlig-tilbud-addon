import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

OFFERS_URL = "https://www.nemlig.com/tilbud"

OPTIONS_FILE = Path("/data/options.json")

# Aktiv tilbudsfil til Mealie-matchning og hoveddashboard.
OUTPUT_FILE = Path("/share/nemlig_tilbud.json")

# Separate datakilder til særlige dashboardkort.
GRATIS_FILE = Path("/share/nemlig_gratis.json")
MADSPILD_FILE = Path("/share/nemlig_madspild.json")
HIGH_DISCOUNT_FILE = Path("/share/nemlig_store_rabatter.json")

DEBUG_HTML_FILE = Path("/share/nemlig_ny_side_debug.html")
DEBUG_SCREENSHOT_FILE = Path("/share/nemlig_ny_side_debug.png")

TIMEOUT_SECONDS = 45

OFFER_TYPE_MAP = {
    "skarp pris": "skarp_pris",
    "priskup": "priskup",
    "dagstilbud": "dagstilbud",
    "undgå madspild": "undgaa_madspild",
    "en gave til dig": "gratis",
    "gratis": "gratis",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
LOGGER = logging.getLogger("nemlig_ny_side")


def load_high_discount_threshold():
    """Læs grænsen for store rabatter fra add-on-indstillinger."""
    try:
        options = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise RuntimeError(
            f"Kunne ikke læse add-on-indstillinger: {OPTIONS_FILE}."
        ) from err
    except json.JSONDecodeError as err:
        raise RuntimeError(
            f"Ugyldig JSON i add-on-indstillinger: {OPTIONS_FILE}."
        ) from err

    value = options.get("high_discount_threshold_percent", 50)

    try:
        threshold = float(value)
    except (TypeError, ValueError) as err:
        raise RuntimeError(
            "high_discount_threshold_percent skal være et tal mellem 0 og 100."
        ) from err

    if threshold < 0 or threshold > 100:
        raise RuntimeError(
            "high_discount_threshold_percent skal være mellem 0 og 100."
        )

    return threshold


def write_json(path, payload):
    """Skriv JSON atomisk, så dashboardet aldrig læser en halv fil."""
    temporary_file = path.with_suffix(path.suffix + ".tmp")
    temporary_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(path)


def make_subset_payload(source_payload, offers, title, threshold=None):
    """Bygger ensartet format til separate tilbudsoversigter."""
    payload = {
        "source": source_payload["source"],
        "source_url": source_payload["source_url"],
        "updated_at": source_payload["updated_at"],
        "title": title,
        "offers_found": len(offers),
        "offers": offers,
    }

    if threshold is not None:
        payload["threshold_percent"] = threshold

    return payload


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

    service = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(45)
    return driver


def save_debug_files(driver):
    """Gemmer debugmateriale, hvis den nye datastruktur ikke findes."""
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


def extract_ng_state(html_text):
    """Finder og læser JSON fra den nye sides ng-state-script."""
    match = re.search(
        r'<script\s+id="ng-state"\s+type="application/json"\s*>(.*?)</script>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        raise RuntimeError(
            'Kunne ikke finde <script id="ng-state">. '
            "Headless-browseren fik muligvis den gamle Nemlig-side."
        )

    return json.loads(match.group(1).strip())


def find_page_content(value):
    """Finder rekursivt pageContent med ProductList-blokke."""
    if isinstance(value, dict):
        page_content = value.get("pageContent")

        if (
            isinstance(page_content, list)
            and any(
                isinstance(item, dict)
                and item.get("contentType") == "ProductList"
                for item in page_content
            )
        ):
            return page_content

        for child in value.values():
            result = find_page_content(child)
            if result is not None:
                return result

    elif isinstance(value, list):
        for child in value:
            result = find_page_content(child)
            if result is not None:
                return result

    return None


def to_kroner(value):
    """Den nye Nemlig-side angiver priser i øre."""
    if value is None:
        return None

    try:
        return round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return None


def normalise_offer_type(value):
    return OFFER_TYPE_MAP.get(str(value or "").strip().lower())


def get_offer_types(product):
    """Finder tilbudstyper fra kampagnelinje, badge og liste-navn."""
    offer_types = set()

    for campaign_line in product.get("campaignLines") or []:
        offer_type = normalise_offer_type(campaign_line.get("text"))
        if offer_type:
            offer_types.add(offer_type)

    badge = product.get("campaignBadge") or {}
    offer_type = normalise_offer_type(badge.get("primaryText"))
    if offer_type:
        offer_types.add(offer_type)

    tracking = product.get("dataLayerTracking") or {}
    offer_type = normalise_offer_type(tracking.get("item_list_name"))
    if offer_type:
        offer_types.add(offer_type)

    return offer_types


def parse_campaign_condition(product):
    """
    Udtrækker vilkår for Mix-/mængdetilbud fra Nemligs campaignLines.

    Eksempel:
    "Mix 2 stk. 95,-" bliver til:
    {
      "type": "mix",
      "text": "Mix 2 stk. 95,-",
      "minimum_quantity": 2,
      "total_price": 95.0
    }

    Vi beregner bevidst ikke en effektiv pris pr. vare, fordi Mix kan
    kombineres med forskellige produkter i samme kampagne.
    """
    for line in product.get("campaignLines") or []:
        text = str(line.get("text") or "").strip()
        campaign_type = str(line.get("campaignType") or "").strip().lower()

        if not text:
            continue

        is_mix = campaign_type == "mix" or text.lower().startswith("mix ")
        is_minimum = text.lower().startswith("min.")

        if not (is_mix or is_minimum):
            continue

        match = re.search(
            r"(?:mix|min\.)\s*(\d+)\s*stk\.?\s*(\d+(?:[,.]\d+)?)(?:\s*[-,])?",
            text,
            flags=re.IGNORECASE,
        )

        minimum_quantity = None
        total_price = None

        if match:
            minimum_quantity = int(match.group(1))
            total_price = float(match.group(2).replace(",", "."))

        return {
            "type": "mix" if is_mix else "minimum_quantity",
            "text": text,
            "minimum_quantity": minimum_quantity,
            "total_price": total_price,
        }

    return None


def get_unit_label(product):
    """Oversætter Nemligs enhedstyper til dashboard-venlige etiketter."""
    price_per_unit = product.get("pricePerUnit") or {}
    unit_type = str(price_per_unit.get("unitType") or "").lower()
    unit_of_measure = str(product.get("unitOfMeasure") or "").strip()

    if unit_type == "weight":
        return "kr./kg"
    if unit_type == "pieces":
        return "kr./stk."
    if unit_of_measure:
        return f"kr./{unit_of_measure.lower().rstrip('.')}"
    return None


def normalise_product(product, offer_types, source_lists):
    """Omsætter produkt fra ny side til vores fælles tilbudsformat."""
    tracking = product.get("tracking") or {}
    price_per_unit = product.get("pricePerUnit") or {}
    availability = product.get("availability") or {}

    campaign_price = to_kroner(product.get("price"))
    normal_price = to_kroner(product.get("priceOriginal"))
    savings = to_kroner(product.get("priceDiscount"))
    campaign_condition = parse_campaign_condition(product)

    discount_percent = None
    if normal_price and normal_price > 0 and campaign_price is not None:
        discount_percent = round(
            ((normal_price - campaign_price) / normal_price) * 100,
            1,
        )

    # Mix-tilbud kan kombineres med andre varer. Vi gemmer derfor
    # den effektive pris ved køb af det krævede antal ENS pakker,
    # men kalder den ikke automatisk den "bedste" pris.
    if campaign_condition:
        minimum_quantity = campaign_condition.get("minimum_quantity")
        total_price = campaign_condition.get("total_price")

        if (
            minimum_quantity
            and total_price is not None
            and campaign_price is not None
            and campaign_price > 0
        ):
            effective_price_per_item = round(
                total_price / minimum_quantity,
                2,
            )
            campaign_condition["effective_price_per_item"] = (
                effective_price_per_item
            )

            listed_unit_price = to_kroner(price_per_unit.get("value"))
            if listed_unit_price is not None:
                campaign_condition["effective_unit_price"] = round(
                    listed_unit_price
                    * effective_price_per_item
                    / campaign_price,
                    2,
                )

    product_path = str(product.get("url") or "").strip()

    return {
        "id": str(product.get("id") or ""),
        "name": product.get("title"),
        "brand": tracking.get("item_brand") or None,
        "category": tracking.get("item_category"),
        "subcategory": tracking.get("item_category2"),
        "subsubcategory": tracking.get("item_category3"),
        "description": product.get("description"),
        "url": (
            f"https://www.nemlig.com/{product_path}"
            if product_path
            else None
        ),
        "image": (product.get("image") or {}).get("source"),
        "search_terms": [],
        "labels": [
            certificate.get("text")
            for certificate in product.get("certificates") or []
            if certificate.get("text")
        ],
        "available_in_stock": availability.get("type") == "Available",
        "availability_status": availability.get("type"),
        "normal_price": normal_price,
        "normal_unit_price": None,
        "unit_label": get_unit_label(product),
        "discount_item": True,
        "offer_types": sorted(offer_types),
        "source_lists": sorted(source_lists),
        "freshness_days": product.get("labelFreshnessDays"),
        "maximum_quantity": product.get("labelMaxQuantity"),
        "campaign": {
            "type": "NewNemligOffer",
            "code": ",".join(sorted(offer_types)),
            "price": campaign_price,
            "unit_price": to_kroner(price_per_unit.get("value")),
            "minimum_quantity": None,
            "maximum_quantity": product.get("labelMaxQuantity"),
            "total_price": None,
            "savings": savings,
            "discount_percent": discount_percent,
            "starts": None,
            "ends": None,
            "mix_offer": bool(
                campaign_condition
                and campaign_condition.get("type") == "mix"
            ),
            "condition": campaign_condition,
        },
    }


def main():
    driver = create_driver()

    try:
        LOGGER.info("Åbner ny Nemlig-tilbudsside i testtilstand.")
        driver.get(OFFERS_URL)
        time.sleep(3)

        try:
            cookie_button = driver.find_element(
                "xpath",
                "//*[self::button or self::a]"
                "[contains(normalize-space(.), 'Kun nødvendige')]",
            )
            driver.execute_script("arguments[0].click();", cookie_button)
            LOGGER.info("Accepterede kun nødvendige cookies.")
            time.sleep(4)
        except Exception:
            LOGGER.info("Cookie-dialog blev ikke fundet eller var allerede håndteret.")

        # Headless-browseren starter på den kendte/ældre Nemlig-side.
        # Vælg bevidst den nye oplevelse i Nemligs overgangsdialog.
        try:
            new_site_button = driver.find_element(
                "xpath",
                "//button[normalize-space(.)='Prøv det nye']"
                " | //a[normalize-space(.)='Prøv det nye']",
            )
            driver.execute_script("arguments[0].click();", new_site_button)
            LOGGER.info("Valgte 'Prøv det nye' i Nemligs overgangsdialog.")
            time.sleep(6)
        except Exception:
            LOGGER.info(
                "Overgangsdialogen til den nye Nemlig-side blev ikke fundet "
                "eller var allerede håndteret."
            )

        deadline = time.time() + TIMEOUT_SECONDS

        while time.time() < deadline:
            html_text = driver.page_source

            if 'id="ng-state"' in html_text:
                LOGGER.info("Fandt ng-state-data fra den nye Nemlig-side.")
                break

            time.sleep(1)
        else:
            save_debug_files(driver)
            raise RuntimeError(
                "Ny Nemlig-side med ng-state blev ikke fundet inden timeout."
            )

        ng_state = extract_ng_state(html_text)
        page_content = find_page_content(ng_state)

        if page_content is None:
            save_debug_files(driver)
            raise RuntimeError(
                "Kunne ikke finde pageContent med ProductList-data i ng-state."
            )

        products_by_id = {}
        product_list_count = 0
        scanned_products = 0

        for block in page_content:
            if block.get("contentType") != "ProductList":
                continue

            product_list_count += 1

            for product in block.get("products") or []:
                scanned_products += 1
                offer_types = get_offer_types(product)

                if not offer_types:
                    continue

                product_id = str(product.get("id") or "")
                if not product_id:
                    continue

                source_list = (
                    (product.get("dataLayerTracking") or {}).get("item_list_name")
                    or "Ukendt liste"
                )

                if product_id not in products_by_id:
                    products_by_id[product_id] = {
                        "product": product,
                        "offer_types": set(),
                        "source_lists": set(),
                    }
                else:
                    # Samme vare kan forekomme i flere tilbuds-/kategorilister.
                    # Behold den rigeste forekomst: især den med Mix- eller
                    # mængdevilkår, så vilkåret ikke går tabt ved dubletfjernelse.
                    existing_product = products_by_id[product_id]["product"]
                    existing_condition = parse_campaign_condition(existing_product)
                    new_condition = parse_campaign_condition(product)

                    if new_condition and not existing_condition:
                        products_by_id[product_id]["product"] = product

                products_by_id[product_id]["offer_types"].update(offer_types)
                products_by_id[product_id]["source_lists"].add(source_list)

        offers = [
            normalise_product(
                item["product"],
                item["offer_types"],
                item["source_lists"],
            )
            for item in products_by_id.values()
        ]

        available_offers = [
            offer for offer in offers
            if offer.get("availability_status") == "Available"
        ]

        offers_by_type = {}
        available_offers_by_type = {}

        for offer in offers:
            for offer_type in offer.get("offer_types", []):
                offers_by_type[offer_type] = (
                    offers_by_type.get(offer_type, 0) + 1
                )

        for offer in available_offers:
            for offer_type in offer.get("offer_types", []):
                available_offers_by_type[offer_type] = (
                    available_offers_by_type.get(offer_type, 0) + 1
                )

        threshold = load_high_discount_threshold()

        # Kun tilgængelige varer må indgå i aktiv matchning og dashboard.
        active_offers = available_offers

        payload = {
            "source": "Nemlig.com — ny tilbudsside",
            "source_url": OFFERS_URL,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "product_lists_found": product_list_count,
            "products_scanned": scanned_products,
            "unique_relevant_offers": len(offers),
            "available_relevant_offers": len(active_offers),
            "offers_by_type": offers_by_type,
            "available_offers_by_type": available_offers_by_type,
            "offers_saved": len(active_offers),
            "offers": active_offers,
        }

        # Gratis vises altid separat, også selv om prisen er 0 kr.
        gratis_offers = [
            offer for offer in active_offers
            if "gratis" in offer.get("offer_types", [])
        ]

        # Madspild vises separat, uanset rabatprocent.
        madspild_offers = [
            offer for offer in active_offers
            if "undgaa_madspild" in offer.get("offer_types", [])
        ]

        # Store rabatter omfatter både mad og non-food.
        # Gratis holdes kun i sit eget kort og kommer derfor ikke med her.
        high_discount_offers = [
            offer for offer in active_offers
            if (
                "gratis" not in offer.get("offer_types", [])
                and (offer.get("campaign") or {}).get("discount_percent") is not None
                and (offer.get("campaign") or {}).get("discount_percent") >= threshold
            )
        ]

        high_discount_offers.sort(
            key=lambda offer: (offer.get("campaign") or {}).get(
                "discount_percent", 0
            ),
            reverse=True,
        )

        write_json(OUTPUT_FILE, payload)
        write_json(
            GRATIS_FILE,
            make_subset_payload(payload, gratis_offers, "Gratis lige nu"),
        )
        write_json(
            MADSPILD_FILE,
            make_subset_payload(payload, madspild_offers, "Undgå madspild"),
        )
        write_json(
            HIGH_DISCOUNT_FILE,
            make_subset_payload(
                payload,
                high_discount_offers,
                "Store rabatter",
                threshold=threshold,
            ),
        )

        print(f"Fandt {product_list_count} ProductList-blokke.")
        print(f"Gennemgik {scanned_products} produktforekomster.")
        print(f"Gemte {len(active_offers)} tilgængelige tilbud i {OUTPUT_FILE}.")
        print(f"Gemte {len(gratis_offers)} gratis varer i {GRATIS_FILE}.")
        print(f"Gemte {len(madspild_offers)} madspildsvarer i {MADSPILD_FILE}.")
        print(
            f"Gemte {len(high_discount_offers)} varer med mindst "
            f"{threshold:g} % rabat i {HIGH_DISCOUNT_FILE}."
        )

    except Exception:
        save_debug_files(driver)
        raise

    finally:
        driver.quit()


if __name__ == "__main__":
    main()

"""Henter Nemligs tilbudsside og gemmer oversigter over tilbud, gratis, madspild og store rabatter."""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import browser

LOGGER = logging.getLogger("nemlig.tilbud")

OFFERS_URL = "https://www.nemlig.com/tilbud"

OUTPUT_FILE = Path("/share/nemlig_tilbud.json")
GRATIS_FILE = Path("/share/nemlig_gratis.json")
MADSPILD_FILE = Path("/share/nemlig_madspild.json")
HIGH_DISCOUNT_FILE = Path("/share/nemlig_store_rabatter.json")
DEBUG_HTML_FILE = Path("/share/nemlig_tilbud_debug.html")
DEBUG_SCREENSHOT_FILE = Path("/share/nemlig_tilbud_debug.png")

TIMEOUT_SECONDS = 45

OFFER_TYPE_MAP = {
    "skarp pris": "skarp_pris",
    "priskup": "priskup",
    "dagstilbud": "dagstilbud",
    "undgå madspild": "undgaa_madspild",
    "en gave til dig": "gratis",
    "gratis": "gratis",
}


def write_json(path, payload):
    """Skriv JSON atomisk, så dashboardet aldrig læser en halv fil."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def subset(source, offers, title, threshold=None):
    payload = {
        "source": source["source"],
        "source_url": source["source_url"],
        "updated_at": source["updated_at"],
        "title": title,
        "offers_found": len(offers),
        "offers": offers,
    }
    if threshold is not None:
        payload["threshold_percent"] = threshold
    return payload


def save_debug_files(driver):
    try:
        DEBUG_HTML_FILE.write_text(driver.page_source, encoding="utf-8")
        driver.save_screenshot(str(DEBUG_SCREENSHOT_FILE))
        LOGGER.info("Gemte debug-filer i /share.")
    except Exception as err:
        LOGGER.warning("Kunne ikke gemme debug-filer: %s", err)


def extract_ng_state(html_text):
    match = re.search(
        r'<script\s+id="ng-state"\s+type="application/json"\s*>(.*?)</script>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise RuntimeError('Kunne ikke finde <script id="ng-state"> på tilbudssiden.')
    return json.loads(match.group(1).strip())


def find_page_content(value):
    """Finder rekursivt pageContent med ProductList-blokke."""
    if isinstance(value, dict):
        page_content = value.get("pageContent")
        if isinstance(page_content, list) and any(
            isinstance(item, dict) and item.get("contentType") == "ProductList"
            for item in page_content
        ):
            return page_content
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None

    for child in children:
        result = find_page_content(child)
        if result is not None:
            return result
    return None


def to_kroner(value):
    """Den nye Nemlig-side angiver priser i øre."""
    try:
        return None if value is None else round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return None


def normalise_offer_type(value):
    return OFFER_TYPE_MAP.get(str(value or "").strip().lower())


def get_offer_types(product):
    offer_types = set()
    for line in product.get("campaignLines") or []:
        offer_type = normalise_offer_type(line.get("text"))
        if offer_type:
            offer_types.add(offer_type)

    for value in (
        (product.get("campaignBadge") or {}).get("primaryText"),
        (product.get("dataLayerTracking") or {}).get("item_list_name"),
    ):
        offer_type = normalise_offer_type(value)
        if offer_type:
            offer_types.add(offer_type)

    if (product.get("labelHighlighted") or {}).get("type") == "PriceMatch":
        offer_types.add("prismatch")

    return offer_types


def parse_campaign_condition(product):
    """Udtrækker Mix-/mængdevilkår, fx 'Mix 2 stk. 95,-'."""
    for line in product.get("campaignLines") or []:
        text = str(line.get("text") or "").strip()
        campaign_type = str(line.get("campaignType") or "").strip().lower()
        if not text:
            continue

        lowered = text.lower()
        is_mix = campaign_type == "mix" or lowered.startswith("mix ")
        is_quantity = bool(re.match(r"(?:min\.\s*)?\d+\s*stk", lowered))
        if not (is_mix or is_quantity):
            continue

        match = re.search(r"(\d+)\s*stk\.?\s*(\d+(?:[,.]\d+)?)", lowered)
        return {
            "type": "mix" if is_mix else "minimum_quantity",
            "text": text,
            "minimum_quantity": int(match.group(1)) if match else None,
            "total_price": float(match.group(2).replace(",", ".")) if match else None,
        }
    return None


def get_unit_label(product):
    unit_type = str((product.get("pricePerUnit") or {}).get("unitType") or "").lower()
    unit_of_measure = str(product.get("unitOfMeasure") or "").strip()
    if unit_type == "weight":
        return "kr./kg"
    if unit_type == "pieces":
        return "kr./stk."
    if unit_type == "volume":
        return "kr./l"
    if unit_of_measure:
        return f"kr./{unit_of_measure.lower().rstrip('.')}"
    return None


def normalise_product(product, offer_types, source_lists):
    tracking = product.get("tracking") or {}
    price_per_unit = product.get("pricePerUnit") or {}
    availability = product.get("availability") or {}

    campaign_price = to_kroner(product.get("price"))
    normal_price = to_kroner(product.get("priceOriginal"))
    condition = parse_campaign_condition(product)

    discount_percent = None
    if normal_price and campaign_price is not None:
        discount_percent = round((normal_price - campaign_price) / normal_price * 100, 1)

    if condition and condition["minimum_quantity"] and condition["total_price"] is not None:
        condition["effective_price_per_item"] = round(
            condition["total_price"] / condition["minimum_quantity"], 2
        )

    path = str(product.get("url") or "").strip()
    return {
        "id": str(product.get("id") or ""),
        "name": product.get("title"),
        "brand": tracking.get("item_brand") or None,
        "category": tracking.get("item_category"),
        "subcategory": tracking.get("item_category2"),
        "subsubcategory": tracking.get("item_category3"),
        "description": product.get("description"),
        "url": f"https://www.nemlig.com/{path}" if path else None,
        "image": (product.get("image") or {}).get("source"),
        "labels": [c.get("text") for c in product.get("certificates") or [] if c.get("text")],
        "availability_status": availability.get("type"),
        "normal_price": normal_price,
        "unit_label": get_unit_label(product),
        "offer_types": sorted(offer_types),
        "source_lists": sorted(source_lists),
        "freshness_days": product.get("labelFreshnessDays"),
        "maximum_quantity": product.get("labelMaxQuantity"),
        "campaign": {
            "price": campaign_price,
            "unit_price": to_kroner(price_per_unit.get("value")),
            "savings": to_kroner(product.get("priceDiscount")),
            "discount_percent": discount_percent,
            "mix_offer": bool(condition and condition["type"] == "mix"),
            "condition": condition,
        },
    }


def collect_offers(page_content):
    products_by_id = {}
    list_count = 0
    scanned = 0

    for block in page_content:
        if block.get("contentType") != "ProductList":
            continue
        list_count += 1

        for product in block.get("products") or []:
            scanned += 1
            offer_types = get_offer_types(product)
            product_id = str(product.get("id") or "")
            if not offer_types or not product_id:
                continue

            source_list = (product.get("dataLayerTracking") or {}).get("item_list_name") or "Ukendt liste"
            entry = products_by_id.setdefault(
                product_id, {"product": product, "offer_types": set(), "source_lists": set()}
            )
            # Behold forekomsten med Mix-/mængdevilkår, så vilkåret ikke går tabt.
            if parse_campaign_condition(product) and not parse_campaign_condition(entry["product"]):
                entry["product"] = product
            entry["offer_types"].update(offer_types)
            entry["source_lists"].add(source_list)

    offers = [
        normalise_product(item["product"], item["offer_types"], item["source_lists"])
        for item in products_by_id.values()
    ]
    return offers, list_count, scanned


HIGH_DISCOUNT_EXCLUDED_CATEGORIES = {"Vin-og-spiritus"}


def savings_kr(offer):
    """Besparelse i kroner: Nemligs egen værdi, ellers normalpris minus tilbudspris."""
    campaign = offer.get("campaign") or {}
    if campaign.get("savings") is not None:
        return campaign["savings"]
    if offer.get("normal_price") is not None and campaign.get("price") is not None:
        return round(offer["normal_price"] - campaign["price"], 2)
    return 0


def run(driver, threshold):
    """Hent tilbudssiden med en åben browser og skriv JSON-filerne."""
    try:
        browser.open_nemlig(driver, OFFERS_URL)

        deadline = time.time() + TIMEOUT_SECONDS
        html_text = ""
        while time.time() < deadline:
            html_text = driver.page_source
            if 'id="ng-state"' in html_text:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Tilbudssiden med ng-state blev ikke fundet inden timeout.")

        page_content = find_page_content(extract_ng_state(html_text))
        if page_content is None:
            raise RuntimeError("Kunne ikke finde ProductList-data på tilbudssiden.")
    except Exception:
        save_debug_files(driver)
        raise

    offers, list_count, scanned = collect_offers(page_content)
    active = [o for o in offers if o.get("availability_status") == "Available"]

    by_type = {}
    for offer in active:
        for offer_type in offer["offer_types"]:
            by_type[offer_type] = by_type.get(offer_type, 0) + 1

    payload = {
        "source": "Nemlig.com",
        "source_url": OFFERS_URL,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "product_lists_found": list_count,
        "products_scanned": scanned,
        "offers_by_type": by_type,
        "offers_saved": len(active),
        "offers": active,
    }

    gratis = [o for o in active if "gratis" in o["offer_types"]]
    madspild = sorted(
        (o for o in active if "undgaa_madspild" in o["offer_types"]),
        key=savings_kr,
        reverse=True,
    )
    high = sorted(
        (
            o for o in active
            if "gratis" not in o["offer_types"]
            and o.get("category") not in HIGH_DISCOUNT_EXCLUDED_CATEGORIES
            and o["campaign"]["discount_percent"] is not None
            and o["campaign"]["discount_percent"] >= threshold
        ),
        key=lambda o: o["campaign"]["discount_percent"],
        reverse=True,
    )

    write_json(OUTPUT_FILE, payload)
    write_json(GRATIS_FILE, subset(payload, gratis, "Gratis lige nu"))
    write_json(MADSPILD_FILE, subset(payload, madspild, "Undgå madspild"))
    write_json(HIGH_DISCOUNT_FILE, subset(payload, high, "Store rabatter", threshold=threshold))

    LOGGER.info(
        "Tilbudssiden: %s tilbud (%s gratis, %s madspild, %s med mindst %g %% rabat).",
        len(active), len(gratis), len(madspild), len(high), threshold,
    )

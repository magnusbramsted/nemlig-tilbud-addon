"""Varesøgning på den nye Nemlig-side via sidens eget søge-API.

Kaldene køres som fetch() inde i den åbne nemlig.com-side (Cloudflare):
1. /webapi/Token giver et anonymt adgangstoken (gyldigt ca. 5 min.)
2. productbff/api/v2/web/search/products returnerer varer i samme format
   som tilbudssiden: priser i øre, pricePerUnit, campaignLines og
   labelHighlighted (PriceMatch = prismatch med REMA 1000).
"""

import logging
import re

from pricing import parse_package

LOGGER = logging.getLogger("nemlig.soeg")

BASE_URL = "https://www.nemlig.com"
DEFAULT_GATEWAY = "https://webapi.prod.knl.nemlig.it"
SEARCH_TAKE = 40

_SEARCH_JS = r"""
const query = arguments[0];
const take = arguments[1];
const fallbackGateway = arguments[2];
const done = arguments[arguments.length - 1];

(async () => {
  try {
    const gateway = (window.scom && window.scom.nemligGatewayApiUrl) || fallbackGateway;
    const getToken = async () => {
      const response = await fetch('/webapi/Token', {credentials: 'omit'});
      if (!response.ok) throw new Error('Token-kald fejlede: ' + response.status);
      window.__nlTok = (await response.json()).access_token;
      window.__nlTokAt = Date.now();
    };
    if (!window.__nlTok || Date.now() - window.__nlTokAt > 240000) await getToken();

    const url = gateway + '/productbff/api/v2/web/search/products?query='
      + encodeURIComponent(query) + '&take=' + take + '&skip=0';
    const get = () => fetch(url, {credentials: 'omit', headers: {Authorization: 'Bearer ' + window.__nlTok}});
    let response = await get();
    if (response.status === 401) { await getToken(); response = await get(); }
    if (!response.ok) { done({error: 'HTTP ' + response.status}); return; }

    const data = await response.json();
    const items = ((data.searchResult || {}).items || [])
      .filter(item => item.type === 'product' && item.product)
      .map(item => item.product);
    done({products: items});
  } catch (error) {
    done({error: String(error)});
  }
})();
"""


def search(driver, query, take=SEARCH_TAKE):
    """Søg efter én vare. Returnerer (normaliserede varer, fejltekst | None)."""
    result = driver.execute_async_script(_SEARCH_JS, query, take, DEFAULT_GATEWAY) or {}
    if result.get("error"):
        return [], result["error"]
    return [normalise_product(p) for p in result.get("products") or []], None


def search_many(driver, queries):
    """Søg efter flere ord. Returnerer ({søgeord: varer}, {søgeord: fejl})."""
    results, errors = {}, {}
    for query in queries:
        products, error = search(driver, query)
        if error:
            errors[query] = error
            LOGGER.warning("Søgning efter '%s' fejlede: %s", query, error)
        else:
            results[query] = products
    LOGGER.info("Søgte efter %s ord hos Nemlig (%s fejl).", len(queries), len(errors))
    return results, errors


# --- Normalisering --------------------------------------------------------------

def _kroner(value):
    """Den nye Nemlig-side angiver priser i øre."""
    try:
        return None if value is None else round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return None


_UNIT_TYPE_DIM = {"weight": "g", "volume": "ml", "pieces": "stk"}
_OFFER_LABELS = {"skarp pris": "Skarp pris", "priskup": "Priskup", "dagstilbud": "Dagstilbud",
                 "undgå madspild": "Undgå madspild"}


def parse_mix(product, price):
    """'2 stk. 38,-' / 'Mix 3 stk. 50,-' -> {'min_qty', 'total'}, hvis billigere."""
    for line in product.get("campaignLines") or []:
        text = str(line.get("text") or "").strip()
        match = re.search(r"(?:mix\s+|min\.\s*)?(\d+)\s*stk\.?\s*(\d+(?:[.,]\d+)?)", text.lower())
        if not match:
            continue
        quantity = int(match.group(1))
        total = float(match.group(2).replace(",", "."))
        if quantity > 1 and price and total / quantity < price:
            return {"min_qty": quantity, "total": total, "text": text}
    return None


def package_for(product, price):
    """Pakningsstørrelse fra beskrivelsen, kontrolleret mod Nemligs kilopris."""
    package = parse_package(product.get("description"))
    price_per_unit = product.get("pricePerUnit") or {}
    dim = _UNIT_TYPE_DIM.get(str(price_per_unit.get("unitType") or "").lower())
    ppu = _kroner(price_per_unit.get("value"))

    if dim and ppu and price:
        derived = price / ppu * (1000 if dim in ("g", "ml") else 1)
        stated = package.get(dim)
        # Stol på kiloprisen, hvis beskrivelsen mangler eller afviger meget
        # (fx '390 g / 230 g' for drænet vægt).
        if not stated or abs(stated - derived) / derived > 0.15:
            package[dim] = round(derived, 1) if dim != "stk" else max(1, round(derived))
    return package


def normalise_product(product):
    """Omsæt en vare fra den nye side til det fælles format, som pricing.py bruger."""
    tracking = product.get("tracking") or {}
    availability = product.get("availability") or {}

    price = _kroner(product.get("price"))
    original = _kroner(product.get("priceOriginal"))
    mix = parse_mix(product, price)

    offer = None
    for line in product.get("campaignLines") or []:
        label = _OFFER_LABELS.get(str(line.get("text") or "").strip().lower())
        if label:
            offer = label
            break
    if not offer and original and price and original > price:
        offer = "Tilbud"
    if not offer and mix:
        offer = mix["text"]

    path = str(product.get("url") or "").strip().lstrip("/")
    return {
        "id": str(product.get("id") or ""),
        "name": product.get("title"),
        "brand": tracking.get("item_brand") or None,
        "description": product.get("description"),
        "category": tracking.get("item_category"),
        "subcategory": tracking.get("item_category2"),
        "main_group": tracking.get("item_category3"),
        "price": price,
        "list_price": original or price,
        "mix": mix,
        "offer": offer,
        "prismatch": (product.get("labelHighlighted") or {}).get("type") == "PriceMatch",
        "package": package_for(product, price),
        "available": availability.get("type", "Available") == "Available",
        "url": f"{BASE_URL}/{path}" if path else None,
    }

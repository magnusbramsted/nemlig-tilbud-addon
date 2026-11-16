"""Nemlig-konto: login, kurv og leveringstider via sidens eget webapi.

Kaldene køres som fetch() inde i den åbne nemlig.com-side (Cloudflare), så
login-cookien (.ASPXAUTH) bliver i browseren:
1. /webapi/Token (Bearer, ca. 5 min.) og /webapi/AntiForgery (XSRF)
2. POST /webapi/login sætter cookien; derefter fornyes tokens
3. /webapi/basket/GetBasket og /webapi/basket/AddToBasket (sætter antal)
4. /webapi/v2/Delivery/GetDeliveryDays (ledige tider med pris og frist)

Programmet bestiller ALDRIG: der findes ingen ordre-, checkout- eller betalingskald her.
"""

import logging
import re
from datetime import datetime, timedelta

LOGGER = logging.getLogger("nemlig.konto")

APP_VERSION = "11.201.0"
_ID = re.compile(r"^\d{1,12}$")

_API_JS = r"""
const action = arguments[0];
const payload = arguments[1] || {};
const version = arguments[2];
const done = arguments[arguments.length - 1];

(async () => {
  const headers = (extra) => Object.assign({
    'Accept': 'application/json, text/plain, */*',
    'Platform': 'web', 'Device-Size': 'desktop', 'Version': version,
    'X-Correlation-Id': (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())),
  }, window.__nlAuth || {}, extra || {});
  const parse = async (r) => {
    const text = await r.text();
    try { return text ? JSON.parse(text) : null; }
    catch (e) { throw new Error('Svaret var ikke JSON (HTTP ' + r.status + ')'); }
  };
  const renew = async () => {
    window.__nlAuth = {};
    const t = await parse(await fetch('/webapi/Token', {credentials: 'include', headers: headers()}));
    if (!t || !t.access_token) throw new Error('Intet adgangstoken fra /webapi/Token');
    const auth = {Authorization: 'Bearer ' + t.access_token};
    let xsrf = '';
    try {
      const x = await parse(await fetch('/webapi/AntiForgery', {credentials: 'include', headers: headers(auth)}));
      xsrf = (x && x.Value) || '';
    } catch (e) { /* ikke alle kald kræver XSRF */ }
    window.__nlAuth = xsrf ? Object.assign(auth, {'X-XSRF-TOKEN': xsrf}) : auth;
  };
  const api = async (method, path, body) => {
    if (!window.__nlAuth || !window.__nlAuth.Authorization) await renew();
    for (let attempt = 0; attempt < 2; attempt++) {
      const r = await fetch(path, {
        method, credentials: 'include',
        headers: headers(body ? {'Content-Type': 'application/json'} : {}),
        body: body ? JSON.stringify(body) : undefined,
      });
      if (r.status === 401 && attempt === 0) { await renew(); continue; }
      if (!r.ok) throw new Error(method + ' ' + path.split('?')[0] + ' gav HTTP ' + r.status);
      return await parse(r);
    }
  };
  const slim = (b) => ({
    logged_in: !!(b && b.Email),
    lines: ((b && b.Lines) || []).filter(l => Number(l.Quantity) > 0)
      .map(l => ({id: String(l.Id), name: l.Name, qty: Number(l.Quantity)})),
    total: b ? (b.TotalPrice ?? null) : null,
    delivery: (b && b.FormattedDeliveryTime) || null,
    minimum_ok: b ? (b.IsMinTotalValid ?? null) : null,
  });

  try {
    if (action === 'login') {
      await renew();
      const res = await api('POST', '/webapi/login', {
        Username: payload.email, Password: payload.password,
        CheckForExistingProducts: true, DoMerge: true, AppInstalled: false, SaveExistingBasket: false,
      });
      const ok = res && typeof res === 'object' && !res.ErrorCode && (('RedirectUrl' in res) || res.MergeSuccessful);
      if (!ok) { done({error: 'Login afvist: ' + ((res && (res.ErrorMessage || res.ErrorCode)) || 'ukendt svar')}); return; }
      await renew();  // tokens skifter efter login
      done({basket: slim(await api('GET', '/webapi/basket/GetBasket'))});
    } else if (action === 'basket') {
      done({basket: slim(await api('GET', '/webapi/basket/GetBasket'))});
    } else if (action === 'set') {
      const b = await api('POST', '/webapi/basket/AddToBasket', {
        ProductId: payload.id, quantity: payload.qty,
        AffectPartialQuantity: false, disableQuantityValidation: false,
      });
      done({basket: slim(b)});
    } else if (action === 'delivery') {
      const days = [];
      let start = null, seen = 0;
      for (let page = 0; page < payload.days && seen < payload.days; page++) {
        const q = new URLSearchParams({days: String(payload.days - seen), showForSubscriptions: 'false'});
        if (start) q.set('startDate', start);
        const data = await api('GET', '/webapi/v2/Delivery/GetDeliveryDays?' + q.toString()) || {};
        const list = data.DayRangeHours || [];
        for (const d of list) days.push(...(d.DayHours || []));
        seen += list.length; start = data.NextRangeStart;
        if (!list.length || !start) break;
      }
      done({hours: days});
    } else {
      done({error: 'Ukendt handling: ' + action});
    }
  } catch (error) {
    done({error: String(error && error.message || error)});
  }
})();
"""


class NemligError(Exception):
    pass


def _call(driver, action, payload=None):
    result = driver.execute_async_script(_API_JS, action, payload or {}, APP_VERSION) or {}
    if result.get("error"):
        raise NemligError(result["error"])
    return result


def login(driver, email, password):
    """Log ind i den åbne browser. Returnerer kurven; fejler, hvis login ikke holder."""
    if not email or not password:
        raise NemligError("nemlig_email og nemlig_password skal udfyldes i appens indstillinger.")
    basket = _call(driver, "login", {"email": email, "password": password})["basket"]
    if not basket.get("logged_in"):
        raise NemligError("Login svarede OK, men kurven er anonym. Tjek email og kodeord.")
    LOGGER.info("Logget ind hos Nemlig.")
    return basket


def get_basket(driver):
    return _call(driver, "basket")["basket"]


def clean_items(items):
    """[{id, qty, name}] -> gyldige varer (samme id lægges sammen) og afviste."""
    merged, rejected = {}, []
    for item in items or []:
        pid = str((item or {}).get("id") or "").strip()
        name = str((item or {}).get("name") or pid)
        if not _ID.match(pid):
            rejected.append({"name": name, "reason": "mangler Nemlig-vare-id"})
            continue
        try:
            qty = max(1, min(50, int(item.get("qty") or 1)))
        except (TypeError, ValueError):
            qty = 1
        if pid in merged:
            merged[pid]["qty"] += qty
        else:
            merged[pid] = {"id": pid, "qty": qty, "name": name}
    return list(merged.values()), rejected


def add_items(driver, items):
    """Læg varer i kurven uden at fjerne noget.

    AddToBasket SÆTTER antallet på linjen. Derfor sendes max(i kurven, ønsket):
    står varen der allerede, røres den ikke, og to tryk på knappen giver ikke
    dobbelt op.
    """
    wanted, rejected = clean_items(items)
    basket = get_basket(driver)
    if not basket.get("logged_in"):
        raise NemligError("Kurven er anonym. Login holdt ikke.")
    have = {line["id"]: line["qty"] for line in basket["lines"]}

    added, already, failed = [], [], []
    for item in wanted:
        current = have.get(item["id"], 0)
        if current >= item["qty"]:
            already.append({**item, "in_basket": current})
            continue
        try:
            basket = _call(driver, "set", {"id": item["id"], "qty": item["qty"]})["basket"]
        except NemligError as err:
            failed.append({**item, "reason": str(err)})
            continue
        applied = next((l["qty"] for l in basket["lines"] if l["id"] == item["id"]), 0)
        if applied >= item["qty"]:
            added.append({**item, "in_basket": applied})
        elif applied > current:
            added.append({**item, "in_basket": applied, "note": f"Nemlig satte antallet til {applied}"})
        else:
            failed.append({**item, "reason": "Nemlig tog ikke varen (udsolgt eller ukendt id?)"})
    return {
        "added": added,
        "already": already,
        "failed": failed,
        "rejected": rejected,
        "basket_total": basket.get("total"),
        "basket_lines": len(basket.get("lines") or []),
        "delivery": basket.get("delivery"),
        "minimum_ok": basket.get("minimum_ok"),
    }


def get_delivery_hours(driver, days=7):
    return _call(driver, "delivery", {"days": days})["hours"]


# --- Leveringstider (ren databehandling, testbar uden browser) -------------------

KINDS = {0: "Personlig", 1: "Fleksibel"}


def _parse_time(value):
    if not value or str(value).startswith(("0001-", "9999-")):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Nemlig sender normalt lokal tid uden zone. Har tiden en zone, omregnes den til
    # containerens lokale tid (Supervisor sætter TZ som i HA).
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def parse_slot(hour):
    day = _parse_time(hour.get("Date"))
    start_h, end_h = hour.get("StartHour"), hour.get("EndHour")
    if day is None or start_h is None or end_h is None:
        return None
    day = day.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        price = None if hour.get("DeliveryPrice") is None else round(float(hour["DeliveryPrice"]), 2)
    except (TypeError, ValueError):
        price = None
    deadline = _parse_time(hour.get("Deadline"))
    return {
        "start": day + timedelta(hours=float(start_h)),
        "end": day + timedelta(hours=float(end_h)),
        "price": price,
        "deadline": deadline,
        "available": hour.get("Availability") == 0,  # 1 frist overskredet, 2 udsolgt, 3 lukket
        "kind": KINDS.get(hour.get("Type")),
    }


def _iso(value):
    return value.isoformat(timespec="minutes") if value else None


def _public(slot):
    return {
        "start": _iso(slot["start"]),
        "end": _iso(slot["end"]),
        "price": slot["price"],
        "deadline": _iso(slot["deadline"]),
        "kind": slot["kind"],
    }


def summarise_delivery(hours, today, max_free=21):
    """Gratis tider og seneste bestillingsfrist for levering senest i morgen.

    today: dansk dato (date). Returnerer et lille dict til HA.
    """
    slots = [s for s in (parse_slot(h) for h in hours or []) if s and s["available"]]
    slots.sort(key=lambda s: s["start"])
    free = [s for s in slots if s["price"] == 0]
    tomorrow = today + timedelta(days=1)

    def latest_deadline(candidates):
        candidates = [s for s in candidates if s["start"].date() <= tomorrow and s["deadline"]]
        if not candidates:
            return None
        best = max(candidates, key=lambda s: (s["deadline"], -s["start"].timestamp()))
        return _public(best)

    near = [s for s in slots if s["start"].date() <= tomorrow and s["price"] is not None]
    cheapest = min(near, key=lambda s: (s["price"], s["start"])) if near else None
    return {
        "slot_count": len(slots),
        "free_count": len(free),
        "next_free": _public(free[0]) if free else None,
        "cheapest_tomorrow": _public(cheapest) if cheapest else None,
        "deadline_tomorrow": latest_deadline(slots),
        "deadline_tomorrow_free": latest_deadline(free),
        "free_slots": [_public(s) for s in free[:max_free]],
    }

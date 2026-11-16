"""Lille HTTP-server til Home Assistant (kun på det interne hassio-netværk).

POST /kurv      Læg de ikke-afkrydsede punkter fra todo.indkobsliste i Nemlig-kurven.
                Valgfrit: {"items": [{"id": "5039806", "qty": 2, "name": "..."}]}
                eller {"entity_id": "todo.anden_liste"}. Bestiller aldrig.
POST /levering  Hent leveringstider nu.
GET  /status    Seneste resultater.

POST-kald svarer straks (202) og kører i baggrunden. Resultatet skrives til /share,
og HA bedes opdatere sensor.nemlig_kurv / sensor.nemlig_levering, når det er klar.
Leveringstiderne hentes også selv hvert DELIVERY_INTERVAL sekund.
"""

import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import browser
import ha_api
import nemlig_konto

PORT = 8099
OPTIONS_FILE = Path("/data/options.json")
KURV_FILE = Path("/share/nemlig_kurv.json")
LEVERING_FILE = Path("/share/nemlig_levering.json")
DELIVERY_INTERVAL = 3600
DELIVERY_DAYS = 7
MAX_BODY = 64 * 1024
TODO_ENTITY = "todo.indkobsliste"
KURV_SENSOR = "sensor.nemlig_kurv"
LEVERING_SENSOR = "sensor.nemlig_levering"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
LOGGER = logging.getLogger("nemlig.server")

# Én browser-session ad gangen (Chromium er tung på en Pi).
_SESSION_LOCK = threading.Lock()
_JOBS = {}  # navn -> kørende tråd
_JOBS_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _options():
    try:
        return json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write(path, payload):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _session(work):
    """Åbn nemlig.com, log ind og kør work(driver)."""
    options = _options()
    with _SESSION_LOCK:
        driver = browser.create_driver()
        try:
            browser.open_nemlig(driver)
            nemlig_konto.login(driver, options.get("nemlig_email"), options.get("nemlig_password"))
            return work(driver)
        finally:
            driver.quit()


def refresh_delivery():
    started = time.monotonic()
    try:
        hours = _session(lambda d: nemlig_konto.get_delivery_hours(d, DELIVERY_DAYS))
        summary = nemlig_konto.summarise_delivery(hours, datetime.now().date())
        payload = {"updated_at": _now(), "ok": True, "error": None, **summary}
        LOGGER.info("Leveringstider: %s ledige, %s gratis (%.0f s).",
                    summary["slot_count"], summary["free_count"], time.monotonic() - started)
    except Exception as err:  # noqa: BLE001 - fejlen skal vises i HA
        LOGGER.exception("Leveringstider fejlede.")
        old = {}
        if LEVERING_FILE.exists():
            try:
                old = json.loads(LEVERING_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                old = {}
        # Behold sidste gode tider, men vis fejlen.
        payload = {**old, "ok": False, "error": str(err), "error_at": _now()}
        payload.setdefault("updated_at", None)
        payload.setdefault("free_count", None)
    _write(LEVERING_FILE, payload)
    ha_api.update_entity(LEVERING_SENSOR)
    return payload


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def send_to_basket(items=None, entity_id=TODO_ENTITY):
    started = time.monotonic()
    _write(KURV_FILE, {**_read(KURV_FILE), "running": True, "started_at": _now()})
    ha_api.update_entity(KURV_SENSOR)
    count = 0
    try:
        if items is None:
            items = nemlig_konto.items_from_todo(ha_api.todo_items(entity_id))
        count = len(items)
        if not items:
            raise nemlig_konto.NemligError("Indkøbslisten har ingen ikke-afkrydsede varer.")
        result = _session(lambda d: nemlig_konto.add_items(d, items))
        payload = {"updated_at": _now(), "ok": not result["failed"], "error": None,
                   "requested": count, **result}
        LOGGER.info("Kurv: %s lagt i, %s fandtes, %s fejlede, %s uden id (%.0f s).",
                    len(result["added"]), len(result["already"]), len(result["failed"]),
                    len(result["rejected"]), time.monotonic() - started)
    except Exception as err:  # noqa: BLE001
        LOGGER.exception("Kurv fejlede.")
        payload = {"updated_at": _now(), "ok": False, "error": str(err), "requested": count,
                   "added": [], "already": [], "failed": [], "rejected": []}
    payload["running"] = False
    _write(KURV_FILE, payload)
    ha_api.update_entity(KURV_SENSOR)
    return payload


def _start(name, target, *args):
    """Start et job i baggrunden, medmindre det allerede kører."""
    with _JOBS_LOCK:
        job = _JOBS.get(name)
        if job and job.is_alive():
            return False
        job = threading.Thread(target=target, args=args, daemon=True, name=name)
        _JOBS[name] = job
        job.start()
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "NemligTilbud"

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("for stor forespørgsel")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("forventede et JSON-objekt")
        return data

    def do_POST(self):  # noqa: N802
        try:
            data = self._body()
        except (ValueError, json.JSONDecodeError) as err:
            self._reply(400, {"ok": False, "error": f"Ugyldig JSON: {err}"})
            return
        if self.path == "/kurv":
            items = data.get("items")
            if items is not None and not isinstance(items, list):
                self._reply(400, {"ok": False, "error": "items skal være en liste"})
                return
            entity_id = str(data.get("entity_id") or TODO_ENTITY)
            if not entity_id.startswith("todo."):
                self._reply(400, {"ok": False, "error": "entity_id skal være en todo-liste"})
                return
            started = _start("kurv", send_to_basket, items, entity_id)
            self._reply(202 if started else 409,
                        {"ok": started, "started": started,
                         "error": None if started else "Kører allerede"})
        elif self.path == "/levering":
            started = _start("levering", refresh_delivery)
            self._reply(202 if started else 409, {"ok": started, "started": started})
        else:
            self._reply(404, {"ok": False, "error": "ukendt sti"})

    def do_GET(self):  # noqa: N802
        if self.path == "/status":
            status = {}
            for key, path in (("kurv", KURV_FILE), ("levering", LEVERING_FILE)):
                try:
                    status[key] = json.loads(path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    status[key] = None
            self._reply(200, status)
        else:
            self._reply(404, {"ok": False, "error": "ukendt sti"})

    def log_message(self, fmt, *args):  # Kun til debug; varer og kodeord logges ikke.
        LOGGER.debug("%s %s", self.address_string(), fmt % args)


def _delivery_loop():
    time.sleep(90)  # lad main.py's første kørsel komme i gang først
    while True:
        refresh_delivery()
        time.sleep(DELIVERY_INTERVAL)


def main():
    threading.Thread(target=_delivery_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOGGER.info("Lytter på port %s (kurv og leveringstider).", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()

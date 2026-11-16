"""Kald til Home Assistant via Supervisor (kræver homeassistant_api: true)."""

import json
import logging
import os
import urllib.request

LOGGER = logging.getLogger("nemlig.ha")
BASE = "http://supervisor/core/api"


def _call(method, path, body=None, timeout=30):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN mangler (homeassistant_api skal være slået til).")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def todo_items(entity_id):
    """Ikke-afkrydsede punkter på en todo-liste."""
    result = _call("POST", "/services/todo/get_items?return_response",
                   {"entity_id": entity_id, "status": "needs_action"})
    response = (result or {}).get("service_response") or {}
    return (response.get(entity_id) or {}).get("items") or []


def update_entity(*entity_ids):
    """Bed HA læse sensorerne igen med det samme (fejl ignoreres)."""
    try:
        _call("POST", "/services/homeassistant/update_entity", {"entity_id": list(entity_ids)})
    except Exception as err:  # noqa: BLE001
        LOGGER.warning("Kunne ikke opdatere %s i HA: %s", ", ".join(entity_ids), err)


def complete_todo(entity_id, uids):
    """Kryds punkter af på en todo-liste. Returnerer antal afkrydsede."""
    done = 0
    for uid in uids:
        try:
            _call("POST", "/services/todo/update_item",
                  {"entity_id": entity_id, "item": uid, "status": "completed"})
            done += 1
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Kunne ikke krydse %s af: %s", uid, err)
    return done

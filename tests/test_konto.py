"""Tests af kurv- og leveringslogik uden browser. Kør: python3 -m pytest tests/"""

import sys
from datetime import date
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "nemlig_tilbud" / "app"
sys.path.insert(0, str(APP))

import nemlig_konto  # noqa: E402


def hour(day, start, end, price, deadline, availability=0, kind=0):
    return {"Date": f"{day}T00:00:00", "StartHour": start, "EndHour": end, "DeliveryPrice": price,
            "Deadline": deadline, "Availability": availability, "Type": kind}


HOURS = [
    hour("2026-11-16", 20, 22, 29.0, "2026-11-16T14:00:00", availability=1),  # frist overskredet
    hour("2026-11-17", 7, 9, 0, "2026-11-16T23:59:00"),
    hour("2026-11-17", 17, 19, 39.0, "2026-11-17T11:00:00"),
    hour("2026-11-17", 19, 21, 0, "2026-11-17T12:00:00"),
    hour("2026-11-18", 8, 10, 0, "2026-11-17T23:59:00"),
    hour("2026-11-18", 18, 20, 0, "2026-11-18T12:00:00", availability=2),  # udsolgt
]


def test_summary_free_slots_and_deadlines():
    s = nemlig_konto.summarise_delivery(HOURS, date(2026, 11, 16))
    assert s["slot_count"] == 4
    assert s["free_count"] == 3
    assert s["next_free"]["start"] == "2026-11-17T07:00"
    # Seneste frist for levering senest i morgen (17/11): 19-21 med frist kl. 12
    assert s["deadline_tomorrow"]["deadline"] == "2026-11-17T12:00"
    assert s["deadline_tomorrow"]["start"] == "2026-11-17T19:00"
    assert s["deadline_tomorrow_free"]["deadline"] == "2026-11-17T12:00"
    assert s["cheapest_tomorrow"]["price"] == 0
    assert [x["start"] for x in s["free_slots"]] == [
        "2026-11-17T07:00", "2026-11-17T19:00", "2026-11-18T08:00"]


def test_summary_no_free_tomorrow():
    hours = [hour("2026-11-17", 17, 19, 39.0, "2026-11-17T11:00:00"),
             hour("2026-11-20", 8, 10, 0, "2026-11-19T23:59:00")]
    s = nemlig_konto.summarise_delivery(hours, date(2026, 11, 16))
    assert s["deadline_tomorrow_free"] is None
    assert s["deadline_tomorrow"]["price"] == 39.0
    assert s["next_free"]["start"] == "2026-11-20T08:00"


def test_summary_empty():
    s = nemlig_konto.summarise_delivery([], date(2026, 11, 16))
    assert s["free_count"] == 0 and s["next_free"] is None and s["deadline_tomorrow"] is None


def test_clean_items_merges_and_rejects():
    items = [{"id": "5039806", "qty": 1, "name": "Mælk"}, {"id": "5039806", "qty": "2"},
             {"id": "", "name": "Brød"}, {"id": "abc", "name": "Ost"}, {"id": 701, "qty": 0}]
    ok, bad = nemlig_konto.clean_items(items)
    assert ok == [{"id": "5039806", "qty": 3, "name": "Mælk"}, {"id": "701", "qty": 1, "name": "701"}]
    assert [b["name"] for b in bad] == ["Brød", "Ost"]


class FakeDriver:
    """Svarer som _API_JS: kurven sætter antal (AddToBasket er absolut)."""

    def __init__(self, lines, refuse=()):
        self.lines = dict(lines)
        self.refuse = set(refuse)
        self.sets = []

    def _basket(self):
        return {"logged_in": True, "lines": [{"id": k, "name": k, "qty": v} for k, v in self.lines.items()],
                "total": 100.0, "delivery": "tirsdag 7-9", "minimum_ok": True}

    def execute_async_script(self, _js, action, payload, _version):
        if action == "basket":
            return {"basket": self._basket()}
        if action == "set":
            self.sets.append((payload["id"], payload["qty"]))
            if payload["id"] not in self.refuse:
                self.lines[payload["id"]] = payload["qty"]
            return {"basket": self._basket()}
        raise AssertionError(action)


def test_add_items_never_lowers_and_is_idempotent():
    driver = FakeDriver({"1": 3}, refuse={"9"})
    items = [{"id": "1", "qty": 2}, {"id": "2", "qty": 2}, {"id": "9", "qty": 1}, {"name": "Manuel"}]
    result = nemlig_konto.add_items(driver, items)
    assert driver.sets == [("2", 2), ("9", 1)]  # "1" har allerede 3 >= 2: røres ikke
    assert [a["id"] for a in result["added"]] == ["2"]
    assert [a["id"] for a in result["already"]] == ["1"]
    assert [f["id"] for f in result["failed"]] == ["9"]
    assert [r["name"] for r in result["rejected"]] == ["Manuel"]
    # Andet tryk ændrer intet
    again = nemlig_konto.add_items(driver, items[:2])
    assert driver.sets == [("2", 2), ("9", 1)]
    assert len(again["already"]) == 2


def test_js_has_no_order_endpoints():
    js = nemlig_konto._API_JS.lower()
    for word in ("checkout", "placeorder", "/order/", "payment", "confirm"):
        assert word not in js

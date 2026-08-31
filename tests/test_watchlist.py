"""Testy watchlisty (watchlist.py) na tymczasowej bazie SQLite, bez sieci —
provider jest dublerem podającym z góry przygotowane odpowiedzi API.

Uruchomienie: python3 -m unittest tests.test_watchlist -v
(ten plik sam dokłada src/ do sys.path, więc działa też bez PYTHONPATH=src).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from holiday_searcher import watchlist  # noqa: E402
from holiday_searcher.models import Destination, Offer, SearchProfile  # noqa: E402
from holiday_searcher.storage import Storage  # noqa: E402


def _profile(**overrides) -> SearchProfile:
    base = dict(
        name="test-profil", country="turcja",
        date_from=date(2026, 9, 19), date_to=date(2026, 9, 30),
        nights_min=5, nights_max=11, boards=["AI", "UAI"],
        adults=2, children_ages=[], stars_min=0, rating_min=0.0,
        max_price_pp=3600, departures=["WAW"], regions=[], vibe=None,
        destinations=[],
    )
    base.update(overrides)
    return SearchProfile(**base)


def _make_offer(hotel_id="42", hotel_name="Test Resort", price=1500,
                 departure_date=date(2026, 9, 20), nights=7,
                 room_type="Standard", tour_operator="TestOp",
                 provider="wakacje.pl") -> Offer:
    return Offer(
        provider=provider, hotel_name=hotel_name, hotel_id=hotel_id,
        tour_operator=tour_operator, country="Turcja", region="Riwiera Turecka",
        city="Alanya", stars=4.0, departure_date=departure_date,
        return_date=departure_date + timedelta(days=nights), nights=nights,
        board="AI", board_raw="All Inclusive", departure_place="Warszawa",
        departure_code="WAW", room_type=room_type, price=price, price_old=0,
        rating=9.0, rating_count=120,
        url=f"https://www.wakacje.pl/oferty/{hotel_name}-{hotel_id}.html",
        raw_id=hotel_id,
    )


def _api_item(hotel_id="42", hotel_name="Test Resort", price=1500,
              departure_date="2026-09-20", nights=7, room_type="Standard",
              tour_operator="TestOp", offer_id="999") -> dict:
    """Minimalny obiekt oferty w kształcie odpowiedzi wakacje.pl — na tyle
    kompletny, żeby przeszedł przez prawdziwy WakacjeProvider._map."""
    return {
        "hotelId": hotel_id, "id": offer_id, "offerId": offer_id,
        "name": hotel_name, "urlName": hotel_name.lower().replace(" ", "-"),
        "tourOperatorName": tour_operator,
        "place": {
            "country": {"name": "Turcja", "slug": "turcja"},
            "region": {"name": "Riwiera Turecka"},
            "city": {"name": "Alanya"},
        },
        "category": 4,
        "departureDate": departure_date,
        "returnDate": departure_date,
        "durationNights": nights,
        "service": 1, "serviceDesc": "All Inclusive",
        "departurePlace": "Warszawa", "departurePlaceCode": "WAW",
        "roomType": room_type,
        "price": price, "priceOld": 0,
        "ratingValue": 9.0, "ratingRecommends": 120,
    }


class FakeProvider:
    """Dubler `WakacjeProvider` — bez sieci. `_post` zwraca kolejne odpowiedzi
    z listy `responses` (albo powtarza ostatnią, gdy zabraknie)."""

    def __init__(self, responses: list[dict]):
        self.delay = 1.5
        self._responses = responses
        self.calls = 0
        self.payloads: list[list[dict]] = []

    def _post(self, payload):
        self.payloads.append(payload)
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class WatchlistTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test-offers.db"
        self.store = Storage(self.db_path)
        watchlist.ensure_schema(self.store.db)

    def tearDown(self):
        self.store.db.close()
        self._tmpdir.cleanup()

    # ---------- dopasowywanie i CRUD ----------

    def test_find_matches_by_hotel_id(self):
        run = self.store.start_run("p", "wakacje.pl")
        self.store.save([_make_offer(hotel_id="42", hotel_name="Sealine")], run)

        matches = watchlist.find_matches(self.store.db, "42")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["hotel_name"], "Sealine")

    def test_find_matches_by_name_fragment_case_insensitive(self):
        run = self.store.start_run("p", "wakacje.pl")
        self.store.save([_make_offer(hotel_id="42", hotel_name="Sunny Beach Resort")], run)

        matches = watchlist.find_matches(self.store.db, "sunny")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["hotel_id"], "42")

    def test_find_matches_ambiguous_name_returns_multiple(self):
        run = self.store.start_run("p", "wakacje.pl")
        self.store.save([
            _make_offer(hotel_id="1", hotel_name="Golden Beach"),
            _make_offer(hotel_id="2", hotel_name="Sunny Beach"),
        ], run)

        matches = watchlist.find_matches(self.store.db, "beach")
        self.assertEqual(len(matches), 2,
                          "dwa różne hotele z 'beach' w nazwie muszą wymagać doprecyzowania")

    def test_add_by_id_then_deactivate_keeps_history(self):
        watch_id = watchlist.add_watch(
            self.store.db, hotel_id="42", hotel_name="Sealine", provider="wakacje.pl",
            profile="test-profil", target_price_pp=1200, note="finalista",
        )
        active = watchlist.list_active(self.store.db)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], watch_id)

        watchlist.deactivate(self.store.db, watch_id)
        self.assertEqual(watchlist.list_active(self.store.db), [])

        # historia zostaje — wiersz wciąż istnieje, tylko active=0
        row = self.store.db.execute(
            "SELECT * FROM watchlist WHERE id=?", (watch_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["hotel_name"], "Sealine")

    def test_find_active_by_id_or_fragment(self):
        wid = watchlist.add_watch(
            self.store.db, hotel_id="42", hotel_name="Sealine", provider="wakacje.pl",
            profile=None, target_price_pp=None, note=None,
        )
        by_id = watchlist.find_active_by_id_or_fragment(self.store.db, str(wid))
        self.assertEqual(len(by_id), 1)
        self.assertEqual(by_id[0]["id"], wid)

        by_name = watchlist.find_active_by_id_or_fragment(self.store.db, "seal")
        self.assertEqual(len(by_name), 1)
        self.assertEqual(by_name[0]["id"], wid)

    # ---------- detekcja zdarzeń ----------

    def test_target_price_reached(self):
        watch_id = watchlist.add_watch(
            self.store.db, hotel_id="42", hotel_name="Test Resort", provider="wakacje.pl",
            profile="test-profil", target_price_pp=1600, note=None,
        )
        entry = watchlist.list_active(self.store.db)[0]
        self.assertEqual(entry["id"], watch_id)

        prov = FakeProvider([{"offers": [_api_item(price=1500)]}])
        events = watchlist.check_entry(self.store, prov, entry, _profile())

        types = [e.event_type for e in events]
        self.assertIn("WATCH_TARGET", types)
        target_event = next(e for e in events if e.event_type == "WATCH_TARGET")
        self.assertEqual(target_event.price, 1500)
        self.assertEqual(target_event.watch_id, watch_id)

        # oferta i snapshot muszą trafić do bazy (append-only historia)
        self.assertEqual(self.store.stats()["offers"], 1)

    def test_no_target_event_when_above_target(self):
        watchlist.add_watch(
            self.store.db, hotel_id="42", hotel_name="Test Resort", provider="wakacje.pl",
            profile="test-profil", target_price_pp=1000, note=None,
        )
        entry = watchlist.list_active(self.store.db)[0]

        prov = FakeProvider([{"offers": [_api_item(price=1500)]}])
        events = watchlist.check_entry(self.store, prov, entry, _profile())

        self.assertNotIn("WATCH_TARGET", [e.event_type for e in events])

    def test_historical_minimum_detected(self):
        """Pierwsze sprawdzenie nie odpala WATCH_ATH (brak historii do
        porównania). Dopiero gdy nowa cena jest niżej niż WSZYSTKO, co kiedyś
        zanotowano, zdarzenie się pojawia."""
        watch_id = watchlist.add_watch(
            self.store.db, hotel_id="42", hotel_name="Test Resort", provider="wakacje.pl",
            profile="test-profil", target_price_pp=None, note=None,
        )
        entry = watchlist.list_active(self.store.db)[0]

        # 1. sprawdzenie: cena 2000 — pierwszy zapis, brak historii do porównania
        prov1 = FakeProvider([{"offers": [_api_item(price=2000, offer_id="1")]}])
        events1 = watchlist.check_entry(self.store, prov1, entry, _profile())
        self.assertNotIn("WATCH_ATH", [e.event_type for e in events1])

        # 2. sprawdzenie: cena 1800 (ten sam wariant -> nowy snapshot, spadek)
        prov2 = FakeProvider([{"offers": [_api_item(price=1800, offer_id="1")]}])
        events2 = watchlist.check_entry(self.store, prov2, entry, _profile())
        self.assertIn("WATCH_ATH", [e.event_type for e in events2])

        # 3. sprawdzenie: cena wraca do 1900 — WYŻEJ niż historyczne minimum (1800)
        prov3 = FakeProvider([{"offers": [_api_item(price=1900, offer_id="1")]}])
        events3 = watchlist.check_entry(self.store, prov3, entry, _profile())
        self.assertNotIn("WATCH_ATH", [e.event_type for e in events3])

    def test_new_cheaper_variant_detected(self):
        """Nowy termin/wariant (inny offer.key) tańszy niż dotychczasowy
        najtańszy aktywny wariant hotelu -> WATCH_NEW_CHEAPEST."""
        watch_id = watchlist.add_watch(
            self.store.db, hotel_id="42", hotel_name="Test Resort", provider="wakacje.pl",
            profile="test-profil", target_price_pp=None, note=None,
        )
        entry = watchlist.list_active(self.store.db)[0]

        # znany wariant A: termin 20.09, cena 1200
        prov1 = FakeProvider([{"offers": [
            _api_item(price=1200, departure_date="2026-09-20", offer_id="A"),
        ]}])
        watchlist.check_entry(self.store, prov1, entry, _profile())

        # nowy wariant B: inny termin (25.09), cena 1000 — taniej niż dotychczasowy najtańszy
        prov2 = FakeProvider([{"offers": [
            _api_item(price=1200, departure_date="2026-09-20", offer_id="A"),
            _api_item(price=1000, departure_date="2026-09-25", offer_id="B"),
        ]}])
        events = watchlist.check_entry(self.store, prov2, entry, _profile())

        cheaper = [e for e in events if e.event_type == "WATCH_NEW_CHEAPEST"]
        self.assertEqual(len(cheaper), 1)
        self.assertEqual(cheaper[0].price, 1000)
        self.assertEqual(cheaper[0].departure_date, "2026-09-25")

    def test_new_variant_not_cheaper_is_not_flagged(self):
        """Nowy wariant, ale DROŻSZY od dotychczasowego najtańszego, nie jest
        zdarzeniem WATCH_NEW_CHEAPEST."""
        watchlist.add_watch(
            self.store.db, hotel_id="42", hotel_name="Test Resort", provider="wakacje.pl",
            profile="test-profil", target_price_pp=None, note=None,
        )
        entry = watchlist.list_active(self.store.db)[0]

        prov1 = FakeProvider([{"offers": [
            _api_item(price=1000, departure_date="2026-09-20", offer_id="A"),
        ]}])
        watchlist.check_entry(self.store, prov1, entry, _profile())

        prov2 = FakeProvider([{"offers": [
            _api_item(price=1000, departure_date="2026-09-20", offer_id="A"),
            _api_item(price=1400, departure_date="2026-09-25", offer_id="B"),
        ]}])
        events = watchlist.check_entry(self.store, prov2, entry, _profile())
        self.assertNotIn("WATCH_NEW_CHEAPEST", [e.event_type for e in events])

    # ---------- anti-spam ----------

    def test_cooldown_blocks_repeat_notification(self):
        watch_id = watchlist.add_watch(
            self.store.db, hotel_id="42", hotel_name="Test Resort", provider="wakacje.pl",
            profile="test-profil", target_price_pp=1600, note=None,
        )
        entry = watchlist.list_active(self.store.db)[0]

        prov = FakeProvider([{"offers": [_api_item(price=1500)]}])
        events = watchlist.check_entry(self.store, prov, entry, _profile())
        target_event = next(e for e in events if e.event_type == "WATCH_TARGET")

        first = watchlist.notifiable(self.store.db, [target_event], cooldown_days=2)
        self.assertEqual(len(first), 1)

        watchlist.mark_sent(self.store.db, target_event)

        second = watchlist.notifiable(self.store.db, [target_event], cooldown_days=2)
        self.assertEqual(len(second), 0, "cooldown powinien zablokować drugie powiadomienie")

    def test_dry_run_does_not_consume_cooldown(self):
        """Symulacja --dry-run: nigdy nie wołamy mark_sent, więc notifiable()
        wciąż zwraca zdarzenie przy kolejnym sprawdzeniu."""
        watch_id = watchlist.add_watch(
            self.store.db, hotel_id="42", hotel_name="Test Resort", provider="wakacje.pl",
            profile="test-profil", target_price_pp=1600, note=None,
        )
        entry = watchlist.list_active(self.store.db)[0]

        prov = FakeProvider([{"offers": [_api_item(price=1500)]}])
        events = watchlist.check_entry(self.store, prov, entry, _profile())
        target_event = next(e for e in events if e.event_type == "WATCH_TARGET")

        first = watchlist.notifiable(self.store.db, [target_event], cooldown_days=2)
        self.assertEqual(len(first), 1)
        # dry-run: BRAK mark_sent

        second = watchlist.notifiable(self.store.db, [target_event], cooldown_days=2)
        self.assertEqual(len(second), 1, "dry-run nie może zużywać cooldownu")

    def test_cooldown_is_per_watch_and_event_type(self):
        watch_id = watchlist.add_watch(
            self.store.db, hotel_id="42", hotel_name="Test Resort", provider="wakacje.pl",
            profile="test-profil", target_price_pp=1600, note=None,
        )
        entry = watchlist.list_active(self.store.db)[0]

        prov = FakeProvider([{"offers": [_api_item(price=1500)]}])
        events = watchlist.check_entry(self.store, prov, entry, _profile())
        target_event = next(e for e in events if e.event_type == "WATCH_TARGET")
        watchlist.mark_sent(self.store.db, target_event)

        other = watchlist.WatchEvent(
            event_type="WATCH_ATH", watch_id=watch_id, hotel_id="42",
            hotel_name="Test Resort", region="Riwiera Turecka", city="Alanya",
            price=1400, departure_date="2026-09-20", nights=7, board="AI",
            url="https://example.com",
        )
        still_notifiable = watchlist.notifiable(self.store.db, [other], cooldown_days=2)
        self.assertEqual(len(still_notifiable), 1,
                          "cooldown jednego typu zdarzenia nie może blokować innego typu")

    # ---------- formatowanie ----------

    def test_format_watch_event_contains_price_and_url(self):
        ev = watchlist.WatchEvent(
            event_type="WATCH_TARGET", watch_id=1, hotel_id="42",
            hotel_name="Test & Resort", region="Riwiera Turecka", city="Alanya",
            price=1500, departure_date="2026-09-20", nights=7, board="All Inclusive",
            url="https://www.wakacje.pl/oferty/test-42.html", extra="Cel: 1 600 zł/os",
        )
        text = watchlist.format_watch_event(ev)
        self.assertIn("1 500", text)
        self.assertIn("https://www.wakacje.pl/oferty/test-42.html", text)
        self.assertIn("Test &amp; Resort", text, "znaki HTML muszą być escapowane")


if __name__ == "__main__":
    unittest.main()

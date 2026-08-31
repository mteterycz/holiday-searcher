"""Testy detekcji okazji (deals.py) na tymczasowej bazie SQLite.

Uruchomienie: python3 -m unittest tests.test_deals -v
(uruchamiane też bez PYTHONPATH=src — ten plik sam dokłada src/ do sys.path).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from holiday_searcher import deals, notify  # noqa: E402
from holiday_searcher.models import Offer  # noqa: E402
from holiday_searcher.storage import Storage  # noqa: E402


def _make_offer(hotel_id="1", price=1000, hotel_name="Test Hotel") -> Offer:
    return Offer(
        provider="test",
        hotel_name=hotel_name,
        hotel_id=hotel_id,
        tour_operator="TestOp",
        country="Turcja",
        region="Riwiera Turecka",
        city="Alanya",
        stars=4.0,
        departure_date=date(2026, 9, 1),
        return_date=date(2026, 9, 8),
        nights=7,
        board="AI",
        board_raw="All Inclusive",
        departure_place="Warszawa",
        departure_code="WAW",
        room_type="Standard",
        price=price,
        price_old=0,
        rating=None,
        rating_count=None,
        url="https://example.com/hotel",
        raw_id=hotel_id,
    )


def _insert_snapshot(store: Storage, offer_key: str, price: int, ts: str | None = None) -> None:
    """Wstawia dodatkowy snapshot z pełną kontrolą nad ceną i znacznikiem
    czasu — do budowania historii cenowej bez czekania na prawdziwy czas."""
    ts = ts or datetime.now().isoformat(timespec="seconds")
    store.db.execute(
        "INSERT INTO price_snapshot(offer_key, ts, price, price_ppn, run_id) VALUES (?,?,?,?,?)",
        (offer_key, ts, price, round(price / 7, 2), None),
    )
    store.db.commit()


class DealsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test-offers.db"
        self.store = Storage(self.db_path)

    def tearDown(self):
        self.store.db.close()
        self._tmpdir.cleanup()

    def test_price_drop_detected(self):
        """Spadek >= progu (domyślnie 5%) przy <5 snapshotach (tryb
        last-minute) jest wykrywany bez potrzeby liczenia percentyla."""
        offer = _make_offer(hotel_id="drop", price=1000)
        run1 = self.store.start_run("p", "test")
        self.store.save([offer], run1)

        offer.price = 900  # -10%
        run2 = self.store.start_run("p", "test")
        self.store.save([offer], run2)

        event = deals.detect_price_events(self.store.db, offer.key)
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, "PRICE_DROP")
        self.assertEqual(event.price_old, 1000)
        self.assertEqual(event.price_new, 900)
        self.assertEqual(event.pct_change, -10.0)

    def test_small_drop_below_threshold_ignored(self):
        """Spadek mniejszy niż drop_pct (tu: 2% < domyślne 5%) nie jest
        zdarzeniem."""
        offer = _make_offer(hotel_id="small-drop", price=1000)
        run1 = self.store.start_run("p", "test")
        self.store.save([offer], run1)

        offer.price = 980  # -2%
        run2 = self.store.start_run("p", "test")
        self.store.save([offer], run2)

        event = deals.detect_price_events(self.store.db, offer.key)
        self.assertIsNone(event)

    def test_percentile_rule_with_five_or_more_snapshots(self):
        """Przy >=5 snapshotach w oknie 30 dni spadek procentowy nie
        wystarcza — cena musi też być poniżej 20. percentyla okna.

        Scenariusz A (odrzucony): cena spada dokładnie o próg (5%), ale w
        historii bywała dużo niżej, więc nowa cena wcale nie jest wyjątkowo
        niska -> None.
        Scenariusz B (zaakceptowany): ta sama logika, ale historia jest
        stabilna, więc nawet spadek dokładnie o próg ląduje w dolnym
        20% okna -> zdarzenie.
        """
        # --- A: odrzucone ---
        offer_a = _make_offer(hotel_id="pct-reject", price=800)
        run = self.store.start_run("p", "test")
        self.store.save([offer_a], run)
        for price in (850, 1300, 1300):
            _insert_snapshot(self.store, offer_a.key, price)
        # Ostatni punkt: spadek dokładnie 5% względem poprzedniego (1300 -> 1235),
        # ale 1235 wciąż wysoko na tle historii (800, 850 były dużo niżej).
        _insert_snapshot(self.store, offer_a.key, 1235)

        event_a = deals.detect_price_events(self.store.db, offer_a.key)
        self.assertIsNone(event_a, "spadek nie powinien przejść reguły percentylowej")

        # --- B: zaakceptowane ---
        offer_b = _make_offer(hotel_id="pct-accept", price=1000)
        run = self.store.start_run("p", "test")
        self.store.save([offer_b], run)
        for price in (1000, 1000, 1000):
            _insert_snapshot(self.store, offer_b.key, price)
        # Ostatni punkt: -5% względem poprzedniego i wyraźnie w dolnym
        # 20% stabilnego okna [1000,1000,1000,1000,950].
        _insert_snapshot(self.store, offer_b.key, 950)

        event_b = deals.detect_price_events(self.store.db, offer_b.key)
        self.assertIsNotNone(event_b, "spadek powinien przejść regułę percentylową")
        self.assertEqual(event_b.event_type, "PRICE_DROP")

    def test_cooldown_blocks_second_notification(self):
        """Po oznaczeniu zdarzenia jako wysłane, to samo zdarzenie nie
        przechodzi przez notifiable() w oknie cooldownu."""
        offer = _make_offer(hotel_id="cooldown", price=1000)
        run1 = self.store.start_run("p", "test")
        self.store.save([offer], run1)

        offer.price = 850  # -15%
        run2 = self.store.start_run("p", "test")
        self.store.save([offer], run2)

        event = deals.detect_price_events(self.store.db, offer.key)
        self.assertIsNotNone(event)

        first = deals.notifiable(self.store.db, [event], cooldown_days=3)
        self.assertEqual(len(first), 1)

        deals.mark_sent(self.store.db, first[0])

        second = deals.notifiable(self.store.db, [event], cooldown_days=3)
        self.assertEqual(len(second), 0, "cooldown powinien zablokować drugie powiadomienie")


class PriceFloorTestCase(unittest.TestCase):
    """PRICE_FLOOR — „najniżej w całej historii tej oferty"."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = Storage(Path(self._tmpdir.name) / "floor.db")

    def tearDown(self):
        self.store.db.close()
        self._tmpdir.cleanup()

    def _seed(self, hotel_id: str, prices: list[int]) -> str:
        """Pierwsza cena wchodzi przez Storage.save (żeby powstał wiersz
        offer), reszta jako kolejne snapshoty z rozsuniętymi znacznikami."""
        offer = _make_offer(hotel_id=hotel_id, price=prices[0])
        run = self.store.start_run("p", "test")
        self.store.save([offer], run)
        base = datetime.now() - timedelta(days=len(prices))
        # Oferta z historią nie jest nowa — inaczej scan_for_events uzna ją za
        # NEW_OFFER i w ogóle nie dojdzie do części cenowej.
        self.store.db.execute("UPDATE offer SET first_seen=? WHERE key=?",
                              (base.isoformat(timespec="seconds"), offer.key))
        self.store.db.commit()
        for i, price in enumerate(prices[1:], 1):
            _insert_snapshot(self.store, offer.key, price,
                             (base + timedelta(hours=i)).isoformat(timespec="seconds"))
        return offer.key

    def test_floor_requires_five_snapshots(self):
        """Nowe minimum przy 4 pomiarach to jeszcze nie „rekord historii"."""
        key = self._seed("floor-short", [1000, 1000, 1000, 800])
        self.assertIsNone(deals.detect_price_floor(self.store.db, key))

    def test_floor_detected_with_enough_history(self):
        key = self._seed("floor-ok", [1000, 1000, 980, 1000, 900])
        event = deals.detect_price_floor(self.store.db, key)
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, "PRICE_FLOOR")
        self.assertEqual(event.price_new, 900)
        self.assertEqual(event.price_old, 980, "price_old to POPRZEDNIE minimum, nie poprzedni punkt")
        self.assertIn("pomiarów", event.note)

    def test_flat_price_at_minimum_is_not_a_floor(self):
        """Cena stojąca na minimum nie jest nowiną — inaczej alert
        powtarzałby się w każdym przebiegu."""
        key = self._seed("floor-flat", [900, 1000, 1000, 1000, 900])
        self.assertIsNone(deals.detect_price_floor(self.store.db, key))

    def test_floor_supersedes_price_drop_in_scan(self):
        """scan_for_events nie może zgłosić jednocześnie PRICE_FLOOR i
        PRICE_DROP dla tej samej oferty."""
        key = self._seed("floor-scan", [1000, 1000, 1000, 1000, 850])
        types = [e.event_type for e in deals.scan_for_events(self.store.db, offer_keys=[key])
                 if e.offer_key == key]
        self.assertIn("PRICE_FLOOR", types)
        self.assertNotIn("PRICE_DROP", types)

    def test_floor_is_notifiable(self):
        key = self._seed("floor-notify", [1000, 1000, 1000, 1000, 850])
        event = deals.detect_price_floor(self.store.db, key)
        self.assertEqual(len(deals.notifiable(self.store.db, [event])), 1)


class VanishedOffersTestCase(unittest.TestCase):
    """OFFER_VANISHED — oferta była w poprzednim przebiegu, nie ma jej w bieżącym."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = Storage(Path(self._tmpdir.name) / "vanish.db")

    def tearDown(self):
        self.store.db.close()
        self._tmpdir.cleanup()

    def _two_runs(self, first: list, second: list) -> None:
        run1 = self.store.start_run("p", "test")
        self.store.save(first, run1)
        run2 = self.store.start_run("p", "test")
        self.store.save(second, run2)

    def test_vanished_after_drop_is_a_sellout_signal(self):
        staying = [_make_offer(hotel_id=f"stay{i}", price=1000) for i in range(4)]
        leaving = _make_offer(hotel_id="gone", price=1000, hotel_name="Znikający")
        self._two_runs(staying + [leaving], staying)
        # Obniżka odnotowana tuż przed zniknięciem.
        _insert_snapshot(self.store, leaving.key, 850)

        events = deals.detect_vanished_offers(self.store.db, "p")
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.event_type, "OFFER_VANISHED")
        self.assertEqual(ev.price_new, 850)
        self.assertEqual(ev.pct_change, -15.0)
        self.assertTrue(ev.is_sellout_signal)
        self.assertEqual(len(deals.notifiable(self.store.db, [ev])), 1)

    def test_plain_vanish_is_reported_but_not_notified(self):
        staying = [_make_offer(hotel_id=f"stay{i}", price=1000) for i in range(4)]
        leaving = _make_offer(hotel_id="gone-flat", price=1000)
        self._two_runs(staying + [leaving], staying)

        events = deals.detect_vanished_offers(self.store.db, "p")
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].is_sellout_signal)
        self.assertEqual(deals.notifiable(self.store.db, events), [],
                         "zwykłe zniknięcie ma być informacją, nie powiadomieniem")

    def test_mass_disappearance_is_treated_as_truncated_results(self):
        """Gdy znika ponad połowa ofert, to obcięty pobór (inny --limit,
        timeout), a nie wyprzedaż — nie zgłaszamy niczego."""
        many = [_make_offer(hotel_id=f"h{i}", price=1000) for i in range(10)]
        self._two_runs(many, many[:2])
        self.assertEqual(deals.detect_vanished_offers(self.store.db, "p"), [])

    def test_empty_current_run_reports_nothing(self):
        many = [_make_offer(hotel_id=f"e{i}", price=1000) for i in range(4)]
        run1 = self.store.start_run("p", "test")
        self.store.save(many, run1)
        self.store.start_run("p", "test")   # przebieg bez zapisów = awaria pobierania
        self.assertEqual(deals.detect_vanished_offers(self.store.db, "p"), [])

    def test_single_run_reports_nothing(self):
        run1 = self.store.start_run("p", "test")
        self.store.save([_make_offer(hotel_id="solo")], run1)
        self.assertEqual(deals.detect_vanished_offers(self.store.db, "p"), [])

    def test_vanished_offer_is_not_also_reported_as_a_deal(self):
        """Oferta, która zniknęła, nie może w tym samym skanie wyjść jako
        okazja — nie ma czego kupić."""
        staying = [_make_offer(hotel_id=f"d{i}", price=1000) for i in range(4)]
        leaving = _make_offer(hotel_id="deal-gone", price=1000)
        self._two_runs(staying + [leaving], staying)
        _insert_snapshot(self.store, leaving.key, 800)   # -20% tuż przed zniknięciem

        types = [e.event_type for e in
                 deals.scan_for_events(self.store.db, offer_keys=[leaving.key],
                                       new_offer_hours=0, profile="p")
                 if e.offer_key == leaving.key]
        self.assertIn("OFFER_VANISHED", types)
        self.assertNotIn("PRICE_DROP", types)
        self.assertNotIn("PRICE_FLOOR", types)

    def test_scan_skips_vanished_without_profile(self):
        """Wsteczna zgodność: bez `profile` scan_for_events działa jak dotąd."""
        staying = [_make_offer(hotel_id=f"s{i}", price=1000) for i in range(4)]
        leaving = _make_offer(hotel_id="v", price=1000)
        self._two_runs(staying + [leaving], staying)
        _insert_snapshot(self.store, leaving.key, 850)

        without = deals.scan_for_events(self.store.db, offer_keys=[], new_offer_hours=0)
        self.assertEqual([e for e in without if e.event_type == "OFFER_VANISHED"], [])

        with_profile = deals.scan_for_events(self.store.db, offer_keys=[],
                                             new_offer_hours=0, profile="p")
        self.assertEqual(len([e for e in with_profile if e.event_type == "OFFER_VANISHED"]), 1)


class EventFormattingTestCase(unittest.TestCase):
    """Nowe typy zdarzeń muszą mieć sensowną treść po polsku, nie surowy typ."""

    def _event(self, **kw):
        base = dict(event_type="PRICE_FLOOR", offer_key="k", hotel_name="Hotel <Test>",
                    region="Kreta", city="Chania", price_old=2500, price_new=2300,
                    pct_change=-8.0, url="https://example.com/h", note="najniżej z 7 pomiarów")
        base.update(kw)
        return deals.DealEvent(**base)

    def test_price_floor_message(self):
        text = notify.format_event(self._event())
        self.assertIn("Historyczne minimum", text)
        self.assertIn("2 300", text)
        self.assertIn("najniżej z 7 pomiarów", text)
        self.assertIn("Hotel &lt;Test&gt;", text, "nazwa musi być escapowana pod HTML Telegrama")

    def test_vanished_after_drop_message(self):
        text = notify.format_event(self._event(event_type="OFFER_VANISHED", pct_change=-12.0,
                                               note="zniknęła z wyników po spadku"))
        self.assertIn("zniknęła po obniżce", text.lower())

    def test_plain_vanished_message(self):
        text = notify.format_event(self._event(event_type="OFFER_VANISHED", pct_change=None,
                                               note="zniknęła z wyników"))
        self.assertIn("zniknęła z wyników", text.lower())
        self.assertNotIn("obniżce", text)

    def test_legacy_events_unchanged(self):
        """Stare typy formatują się dokładnie jak przed zmianą (bez `note`)."""
        drop = deals.DealEvent("PRICE_DROP", "k", "Hotel", "Kreta", "Chania",
                               1000, 900, -10.0, "https://example.com")
        text = notify.format_event(drop)
        self.assertIn("Spadek ceny", text)
        self.assertIn("1 000 → 900", text)
        self.assertEqual(drop.note, "")

    def test_vanished_digest_lists_all(self):
        events = [self._event(event_type="OFFER_VANISHED", offer_key=f"k{i}",
                              hotel_name=f"Hotel {i}", pct_change=-10.0 - i)
                  for i in range(3)]
        text = notify.format_vanished_digest(events, "wrzesien-okazje")
        self.assertIn("3 oferty zniknęły", text)
        for i in range(3):
            self.assertIn(f"Hotel {i}", text)


if __name__ == "__main__":
    unittest.main()

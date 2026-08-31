"""Testy indeksu cen hotelu (hotel_index.py).

Nacisk położony na to, co najłatwiej zepsuć: zachowanie przy KRÓTKIEJ
historii (1-4 pomiary) i agregację wariantów tego samego hotelu.

Uruchomienie: python3 -m unittest tests.test_hotel_index -v
(bez PYTHONPATH=src — plik sam dokłada src/ do sys.path).
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

from holiday_searcher import hotel_index  # noqa: E402
from holiday_searcher.models import Offer  # noqa: E402
from holiday_searcher.storage import Storage  # noqa: E402


def _offer(hotel_id="1", price=1400, nights=7, board="AI", room="Standard",
           provider="test", hotel_name="Test Hotel") -> Offer:
    return Offer(
        provider=provider, hotel_name=hotel_name, hotel_id=hotel_id,
        tour_operator="TestOp", country="Grecja", region="Kreta", city="Chania",
        stars=4.0, departure_date=date(2026, 9, 19), return_date=date(2026, 9, 26),
        nights=nights, board=board, board_raw="All Inclusive",
        departure_place="Warszawa", departure_code="WAW", room_type=room,
        price=price, price_old=0, rating=None, rating_count=None,
        url="https://example.com/hotel", raw_id=hotel_id,
    )


class HotelIndexTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = Storage(Path(self._tmpdir.name) / "index.db")

    def tearDown(self):
        self.store.db.close()
        self._tmpdir.cleanup()

    def _history(self, offer: Offer, prices: list[int], start_days_ago: int = 5) -> str:
        """Zapisuje ofertę i dokłada jej historię cen z rozsuniętymi w czasie
        znacznikami (co godzinę), żeby rozpiętość czasowa była realistyczna."""
        run = self.store.start_run("prof", "test")
        self.store.save([offer], run)
        base = datetime.now() - timedelta(days=start_days_ago)
        for i, price in enumerate(prices, 1):
            self.store.db.execute(
                "INSERT INTO price_snapshot(offer_key, ts, price, price_ppn, run_id) "
                "VALUES (?,?,?,?,?)",
                (offer.key, (base + timedelta(hours=i * 6)).isoformat(timespec="seconds"),
                 price, round(price / offer.nights, 2), run),
            )
        self.store.db.commit()
        return offer.key

    # ---------------------------------------------------------- krótka historia

    def test_single_snapshot_degrades_without_crashing(self):
        """Jeden pomiar: indeks powstaje, ale nie orzeka niczego."""
        offer = _offer(hotel_id="solo", price=1400)
        run = self.store.start_run("prof", "test")
        self.store.save([offer], run)

        idx = hotel_index.offer_index(self.store.db, offer.key)
        self.assertIsNotNone(idx)
        self.assertEqual(idx.samples, 1)
        self.assertEqual(idx.min_ppn, idx.max_ppn)
        self.assertIsNone(idx.percentile, "pozycji jednego punktu nie da się określić")
        self.assertEqual(idx.confidence, hotel_index.CONF_NONE)
        self.assertFalse(idx.at_historic_low)
        self.assertFalse(idx.in_bottom_zone)
        self.assertIn("pierwszy pomiar", idx.headline())

    def test_two_snapshots_never_claim_historic_low(self):
        """NAJWAŻNIEJSZY test modułu: przy 2 pomiarach nowe minimum nie może
        być zaraportowane jako „historyczne minimum"."""
        offer = _offer(hotel_id="two", price=1400)
        key = self._history(offer, [900])   # save() dał 1400, potem 900

        idx = hotel_index.offer_index(self.store.db, key)
        self.assertEqual(idx.samples, 2)
        self.assertTrue(idx.is_strict_low, "cena faktycznie jest najniższa z dwóch")
        self.assertFalse(idx.reliable)
        self.assertFalse(idx.at_historic_low, "2 punkty to za mało na taką tezę")
        self.assertEqual(idx.confidence, hotel_index.CONF_LOW)
        self.assertIn("za mało", idx.headline())

    def test_confidence_grows_with_history(self):
        cases = {1: hotel_index.CONF_NONE, 3: hotel_index.CONF_LOW,
                 6: hotel_index.CONF_MEDIUM, 14: hotel_index.CONF_HIGH}
        for n, expected in cases.items():
            offer = _offer(hotel_id=f"conf{n}", price=1400)
            key = self._history(offer, [1400] * (n - 1), start_days_ago=10)
            idx = hotel_index.offer_index(self.store.db, key)
            self.assertEqual(idx.samples, n)
            self.assertEqual(idx.confidence, expected, f"n={n}")

    def test_missing_snapshots_give_no_index(self):
        self.assertIsNone(hotel_index.offer_index(self.store.db, "nie-ma-takiej"))

    # ------------------------------------------------------------- statystyka

    def test_percentile_and_range(self):
        offer = _offer(hotel_id="pct", price=1400, nights=7)
        key = self._history(offer, [1400, 1400, 1400, 1050])

        idx = hotel_index.offer_index(self.store.db, key)
        self.assertEqual(idx.samples, 5)
        self.assertEqual(idx.current_price, 1050)
        self.assertEqual(idx.min_ppn, 150.0)
        self.assertEqual(idx.max_ppn, 200.0)
        self.assertEqual(idx.median_ppn, 200.0)
        # Jedyne minimum w pięciu punktach: (0 + 0.5) / 5 = 0.1
        self.assertAlmostEqual(idx.percentile, 0.1)
        self.assertTrue(idx.in_bottom_zone)
        self.assertTrue(idx.at_historic_low)
        self.assertEqual(idx.previous_min_price, 1400)

    def test_flat_history_sits_in_the_middle(self):
        """Płaska cena nie jest ani okazją, ani ostrzeżeniem — percentyl 0.5."""
        offer = _offer(hotel_id="flat", price=1400)
        key = self._history(offer, [1400] * 5)
        idx = hotel_index.offer_index(self.store.db, key)
        self.assertAlmostEqual(idx.percentile, 0.5)
        self.assertFalse(idx.in_bottom_zone)
        self.assertFalse(idx.is_strict_low, "równe minimum to nie NOWE minimum")

    def test_highest_price_lands_at_the_top(self):
        offer = _offer(hotel_id="peak", price=1000)
        key = self._history(offer, [1000, 1000, 1000, 1600])
        idx = hotel_index.offer_index(self.store.db, key)
        self.assertGreater(idx.percentile, 0.8)
        self.assertFalse(idx.in_bottom_zone)

    def test_vs_median_and_spread(self):
        offer = _offer(hotel_id="med", price=1400, nights=7)
        key = self._history(offer, [1400, 1400, 1400, 700])
        idx = hotel_index.offer_index(self.store.db, key)
        self.assertEqual(idx.vs_median_pct, -50.0)
        self.assertEqual(idx.spread_pct, 100.0)

    # ------------------------------------------------------ agregacja hotelu

    def test_variants_of_one_hotel_share_history(self):
        """Dwa warianty tego samego hotelu (inne wyżywienie/pokój) to jeden
        hotel — historia ma się zsumować, a nie rozjechać na dwie."""
        a = _offer(hotel_id="H1", price=1400, board="AI", room="Standard")
        b = _offer(hotel_id="H1", price=1750, board="HB", room="Sea View")
        self.assertNotEqual(a.key, b.key)
        self._history(a, [1400, 1400])
        self._history(b, [1750, 1750])

        idx = hotel_index.hotel_index(self.store.db, "test", "H1")
        self.assertEqual(idx.variants, 2)
        self.assertEqual(idx.samples, 6, "3 pomiary na wariant")
        self.assertEqual(idx.min_ppn, 200.0)
        self.assertEqual(idx.max_ppn, 250.0)

    def test_many_variants_in_one_run_are_not_history(self):
        """Pięć wariantów hotelu w JEDNYM przebiegu to pięć snapshotów, ale
        zero historii — indeks nie może uznać tego za wiarygodną podstawę.
        Bez tego rozróżnienia hotel sprzedawany w wielu wariantach
        dostawałby etykietę „historyczne minimum" po pierwszym pobraniu."""
        variants = [_offer(hotel_id="wide", price=p, room=f"R{i}")
                    for i, p in enumerate([1400, 1500, 1600, 1700, 1800])]
        run = self.store.start_run("prof", "test")
        self.store.save(variants, run)

        idx = hotel_index.hotel_index(self.store.db, "test", "wide")
        self.assertEqual(idx.samples, 5)
        self.assertEqual(idx.time_points, 1, "wszystkie z jednego przebiegu")
        self.assertFalse(idx.reliable)
        self.assertFalse(idx.at_historic_low)
        self.assertFalse(idx.in_bottom_zone)
        self.assertEqual(idx.confidence, hotel_index.CONF_NONE)
        self.assertIn("rozrzut wariantów", idx.headline())

    def test_different_night_counts_compared_per_night(self):
        """Wariant 5-nocny jest droższy za noc, choć pakiet jest tańszy —
        indeks musi to widzieć w zł/os/noc, inaczej „minimum" oznaczałoby
        po prostu krótszy wyjazd."""
        long_stay = _offer(hotel_id="H2", price=1400, nights=7, room="A")
        short_stay = _offer(hotel_id="H2", price=1100, nights=5, room="B")
        self._history(long_stay, [1400])
        self._history(short_stay, [1100])

        idx = hotel_index.hotel_index(self.store.db, "test", "H2")
        self.assertEqual(idx.min_ppn, 200.0, "tańszy ZA NOC jest pobyt 7-nocny")
        self.assertEqual(idx.max_ppn, 220.0)

    def test_same_hotel_id_across_providers_is_not_merged(self):
        """Numeracja hoteli u dostawców jest niezależna — te same cyfry to
        dwa różne obiekty, nie jeden z podwójną historią."""
        self._history(_offer(hotel_id="42", provider="wakacje.pl", price=1400), [1400])
        self._history(_offer(hotel_id="42", provider="r.pl", price=2100), [2100])

        rows = hotel_index.build_all(self.store.db)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.provider for r in rows}, {"wakacje.pl", "r.pl"})
        for r in rows:
            self.assertEqual(r.variants, 1)

    def test_offer_without_hotel_id_falls_back_to_own_history(self):
        a = _offer(hotel_id="", price=1400, room="A", hotel_name="Bez ID")
        b = _offer(hotel_id="", price=1900, room="B", hotel_name="Bez ID")
        self._history(a, [1400])
        self._history(b, [1900])

        rows = hotel_index.build_all(self.store.db)
        self.assertEqual(len(rows), 2, "bez hotel_id każda oferta jest osobno")
        for r in rows:
            self.assertEqual(r.scope, "oferta")

    def test_index_for_offer_uses_hotel_scope(self):
        a = _offer(hotel_id="H3", price=1400, room="A")
        b = _offer(hotel_id="H3", price=1750, room="B")
        self._history(a, [1400])
        self._history(b, [1750])
        idx = hotel_index.index_for_offer(self.store.db, a.key)
        self.assertEqual(idx.scope, "hotel")
        self.assertEqual(idx.variants, 2)

    # ------------------------------------------------------------- build_all

    def test_build_all_scopes_to_profile(self):
        keep = _offer(hotel_id="in", price=1400)
        run = self.store.start_run("prof-a", "test")
        self.store.save([keep], run)
        other = _offer(hotel_id="out", price=1400)
        run2 = self.store.start_run("prof-b", "test")
        self.store.save([other], run2)

        rows = hotel_index.build_all(self.store.db, profile="prof-a")
        self.assertEqual([r.hotel_id for r in rows], ["in"])
        self.assertEqual(len(hotel_index.build_all(self.store.db)), 2)

    def test_build_all_puts_history_backed_deals_first(self):
        """Hotel bez historii nie może wyprzedzić hotelu, o którym coś wiemy —
        nawet jeśli jest tańszy."""
        cheap_unknown = _offer(hotel_id="unknown", price=700)
        run = self.store.start_run("prof", "test")
        self.store.save([cheap_unknown], run)
        self._history(_offer(hotel_id="known", price=1400), [1400, 1400, 1400, 1000])

        rows = hotel_index.build_all(self.store.db, profile="prof")
        self.assertEqual(rows[0].hotel_id, "known")
        self.assertTrue(rows[0].at_historic_low)
        self.assertFalse(rows[1].reliable)

    def test_build_all_on_empty_database(self):
        self.assertEqual(hotel_index.build_all(self.store.db), [])
        self.assertEqual(hotel_index.build_all(self.store.db, profile="nie-ma"), [])


if __name__ == "__main__":
    unittest.main()

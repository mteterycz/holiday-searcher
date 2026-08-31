"""Testy kalendarza cen (price_calendar.py) — BEZ sieci.

Provider jest zamockowany (deterministyczny generator ofert), baza to
tymczasowy plik SQLite. Zakres: budowanie okna dat i wariantów profilu,
agregacja min po (data, noce), wykrycie minimum i strefy 5%, zapis/odczyt
tabeli price_calendar.

Uruchomienie: python3 -m unittest tests.test_price_calendar -v
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

from holiday_searcher import price_calendar as pc  # noqa: E402
from holiday_searcher.models import Destination, Offer, SearchProfile  # noqa: E402


def _profile(**kw) -> SearchProfile:
    base = dict(
        name="test-profil",
        country="turcja",
        date_from=date(2026, 9, 19),
        date_to=date(2026, 9, 23),
        nights_min=5,
        nights_max=7,
        boards=["AI"],
        adults=2,
        departures=["WAW"],
    )
    base.update(kw)
    return SearchProfile(**base)


def _offer(dep: date, nights: int, price: int, hotel_id: str = "1",
           hotel_name: str = "Hotel Testowy") -> Offer:
    return Offer(
        provider="test", hotel_name=hotel_name, hotel_id=hotel_id,
        tour_operator="TestOp", country="Turcja", region="Riwiera", city="Alanya",
        stars=4.0, departure_date=dep, return_date=dep + timedelta(days=nights),
        nights=nights, board="AI", board_raw="All Inclusive",
        departure_place="Warszawa", departure_code="WAW", room_type="Standard",
        price=price, price_old=0, rating=8.5, rating_count=100,
        url=f"https://example.com/{hotel_id}", raw_id=hotel_id,
    )


class FakeProvider:
    """Zastępuje WakacjeProvider: zapamiętuje, o jakie warianty profilu został
    poproszony, i oddaje wcześniej przygotowane oferty dla danej daty."""

    def __init__(self, by_date: dict[date, list[Offer]]):
        self.by_date = by_date
        self.calls: list[SearchProfile] = []

    def search(self, profile: SearchProfile, limit: int | None = None) -> list[Offer]:
        self.calls.append(profile)
        return list(self.by_date.get(profile.date_from, []))[: limit or None]


# ---------------------------------------------------------------- okno dat

class DepartureDatesTestCase(unittest.TestCase):
    def test_window_is_profile_widened_both_ways(self):
        p = _profile()   # 19.09 – 23.09 = 5 dni
        days = pc.departure_dates(p, spread=2, max_dates=None, today=None)
        self.assertEqual(days[0], date(2026, 9, 17))
        self.assertEqual(days[-1], date(2026, 9, 25))
        self.assertEqual(len(days), 9)                 # 5 + 2 + 2
        self.assertEqual(len(set(days)), len(days))    # bez duplikatów
        self.assertEqual(days, sorted(days))

    def test_spread_zero_gives_exactly_profile_window(self):
        p = _profile()
        days = pc.departure_dates(p, spread=0, max_dates=None, today=None)
        self.assertEqual(days, [date(2026, 9, 19) + timedelta(days=i) for i in range(5)])

    def test_max_dates_trims_symmetrically_around_centre(self):
        """Sufit kosztu nie może przesunąć kalendarza na jeden koniec okna —
        obcinamy tyle samo z każdej strony."""
        p = _profile()
        days = pc.departure_dates(p, spread=5, max_dates=5, today=None)
        self.assertEqual(len(days), 5)
        full = pc.departure_dates(p, spread=5, max_dates=None, today=None)
        centre = full[len(full) // 2]
        self.assertIn(centre, days)
        self.assertEqual(days[0], date(2026, 9, 19))   # (15 - 5)//2 = 5 dni z przodu

    def test_past_dates_are_dropped(self):
        p = _profile()
        days = pc.departure_dates(p, spread=5, max_dates=None, today=date(2026, 9, 20))
        self.assertEqual(days[0], date(2026, 9, 20))
        self.assertTrue(all(d >= date(2026, 9, 20) for d in days))

    def test_negative_spread_rejected(self):
        with self.assertRaises(ValueError):
            pc.departure_dates(_profile(), spread=-1)

    def test_window_profile_keeps_everything_but_dates(self):
        """SearchProfile jest frozen — wariant musi powstać przez replace
        i zachować kierunki, wyżywienie oraz skład osobowy."""
        p = _profile(destinations=[Destination(country="turcja", regions=["312009"])],
                     children_ages=[7])
        v = pc.window_profile(p, date(2026, 9, 25))
        self.assertEqual(v.date_from, date(2026, 9, 25))
        # date_to musi zostawić miejsce na najdłuższy pobyt (7 nocy) + 1 dzień luzu
        self.assertEqual(v.date_to, date(2026, 10, 3))
        self.assertEqual(v.nights_min, p.nights_min)
        self.assertEqual(v.nights_max, p.nights_max)
        self.assertEqual(v.children_ages, [7])
        self.assertEqual([d.country for d in v.legs()], ["turcja"])
        self.assertIsNot(v, p)
        self.assertEqual(p.date_from, date(2026, 9, 19), "oryginał nie może się zmienić")

    def test_in_profile_window(self):
        p = _profile()
        self.assertTrue(pc.in_profile_window(p, date(2026, 9, 19)))
        self.assertTrue(pc.in_profile_window(p, date(2026, 9, 23)))
        self.assertFalse(pc.in_profile_window(p, date(2026, 9, 18)))
        self.assertFalse(pc.in_profile_window(p, date(2026, 9, 24)))


# --------------------------------------------------------------- agregacja

class AggregateTestCase(unittest.TestCase):
    def test_keeps_cheapest_per_date_and_nights(self):
        offers = [
            _offer(date(2026, 9, 19), 7, 3000, hotel_id="drogi"),
            _offer(date(2026, 9, 19), 7, 2100, hotel_id="tani"),
            _offer(date(2026, 9, 19), 5, 1800, hotel_id="krotki"),
            _offer(date(2026, 9, 20), 7, 2500, hotel_id="inny-dzien"),
        ]
        grid = pc.aggregate(offers)
        self.assertEqual(len(grid), 3)
        self.assertEqual(grid[(date(2026, 9, 19), 7)].price_pp, 2100)
        self.assertEqual(grid[(date(2026, 9, 19), 7)].hotel_id, "tani")
        self.assertEqual(grid[(date(2026, 9, 19), 5)].price_pp, 1800)
        self.assertEqual(grid[(date(2026, 9, 20), 7)].price_pp, 2500)

    def test_price_ppn_is_derived_from_the_same_offer(self):
        grid = pc.aggregate([_offer(date(2026, 9, 19), 7, 2100)])
        cell = grid[(date(2026, 9, 19), 7)]
        self.assertAlmostEqual(cell.price_ppn, 2100 / 7, places=2)

    def test_filters_dates_nights_and_broken_offers(self):
        offers = [
            _offer(date(2026, 9, 19), 7, 2100),
            _offer(date(2026, 9, 30), 7, 900),    # data spoza okna
            _offer(date(2026, 9, 19), 12, 800),   # za długo
            _offer(date(2026, 9, 19), 3, 500),    # za krótko
            _offer(date(2026, 9, 20), 7, 0),      # brak ceny = brak informacji
        ]
        grid = pc.aggregate(offers, dates=[date(2026, 9, 19), date(2026, 9, 20)],
                            nights_range=(5, 7))
        self.assertEqual(list(grid), [(date(2026, 9, 19), 7)])

    def test_empty_input_gives_empty_grid(self):
        self.assertEqual(pc.aggregate([]), {})
        self.assertIsNone(pc.best_cell({}))
        self.assertEqual(pc.near_minimum_keys({}), set())


# ----------------------------------------------------------------- minimum

class MinimumTestCase(unittest.TestCase):
    def setUp(self):
        # 7 nocy: 19.09 drogo, 24.09 tanio. 5 nocy: niższa cena całkowita,
        # ale WYŻSZA cena za osobę za noc — minimum musi to rozróżnić.
        self.offers = [
            _offer(date(2026, 9, 19), 7, 2610),   # 372,86/noc
            _offer(date(2026, 9, 20), 7, 2550),   # 364,29/noc
            _offer(date(2026, 9, 24), 7, 2130),   # 304,29/noc  <- minimum
            _offer(date(2026, 9, 25), 7, 2200),   # 314,29/noc  <- w granicach 5%
            _offer(date(2026, 9, 24), 5, 1900),   # 380,00/noc  (tańsze, ale droższe/noc)
        ]
        self.grid = pc.aggregate(self.offers)

    def test_minimum_is_measured_per_person_per_night(self):
        best = pc.best_cell(self.grid)
        self.assertEqual(best.departure_date, date(2026, 9, 24))
        self.assertEqual(best.nights, 7)
        self.assertEqual(best.price_pp, 2130)

    def test_near_minimum_band_is_five_percent_and_excludes_the_minimum(self):
        near = pc.near_minimum_keys(self.grid, pct=0.05)
        # 314,29 / 304,29 = 1.033 -> mieści się w 5%
        self.assertIn((date(2026, 9, 25), 7), near)
        self.assertNotIn((date(2026, 9, 24), 7), near, "minimum nie należy do strefy 5%")
        self.assertNotIn((date(2026, 9, 19), 7), near)
        self.assertNotIn((date(2026, 9, 24), 5), near)

    def test_column_minimums_compare_only_within_the_same_length(self):
        cols = pc.column_minimums(self.grid)
        self.assertEqual(cols[7].departure_date, date(2026, 9, 24))
        self.assertEqual(cols[5].price_pp, 1900)

    def test_summary_names_the_saving_against_the_profile_window(self):
        p = _profile()   # okno 19.09 – 23.09, więc 24.09 jest już „poza"
        text = pc.summarize(self.grid, p)
        self.assertIn("24.09", text)
        self.assertIn("7 nocy", text)
        self.assertIn("2 130", text)
        # Punktem odniesienia jest NAJTAŃSZY termin z okna profilu (20.09 = 2550),
        # a nie pierwszy z brzegu — inaczej oszczędność byłaby zawyżona.
        self.assertIn("420", text)
        self.assertIn("20.09", text)

    def test_summary_when_minimum_lands_inside_the_profile_window(self):
        p = _profile(date_from=date(2026, 9, 19), date_to=date(2026, 9, 30))
        text = pc.summarize(self.grid, p)
        self.assertIn("mieści się w oknie profilu", text)

    def test_summary_does_not_advertise_a_zero_saving(self):
        """Gdy w oknie profilu da się kupić to samo za tę samą cenę, komunikat
        nie może brzmieć „o 0 zł taniej"."""
        grid = pc.aggregate([
            _offer(date(2026, 9, 18), 7, 2300),   # poza oknem, minimum
            _offer(date(2026, 9, 22), 7, 2300),   # w oknie, ta sama cena
        ])
        text = pc.summarize(grid, _profile(date_from=date(2026, 9, 19),
                                           date_to=date(2026, 9, 23)))
        self.assertNotIn("o 0 zł", text)
        self.assertIn("Tyle samo kosztuje", text)
        self.assertIn("22.09", text)

    def test_summary_without_data(self):
        self.assertIn("Brak danych", pc.summarize({}, _profile()))

    def test_spread_report_ranks_by_relative_gap(self):
        rows = pc.spread_report(self.grid)
        nights, cheap, pricey, pct = rows[0]
        self.assertEqual(nights, 7)
        self.assertEqual(cheap.price_pp, 2130)
        self.assertEqual(pricey.price_pp, 2610)
        self.assertAlmostEqual(pct, (2610 - 2130) / 2610 * 100, places=2)
        # kolumna z jedną komórką (5 nocy) nie ma rozrzutu i nie trafia do raportu
        self.assertTrue(all(r[0] != 5 for r in rows))


# ------------------------------------------------------- pobieranie (mock)

class CollectTestCase(unittest.TestCase):
    def test_one_search_per_date_with_replaced_window(self):
        p = _profile()
        dates = [date(2026, 9, 19), date(2026, 9, 20)]
        prov = FakeProvider({
            dates[0]: [_offer(dates[0], 7, 2600)],
            dates[1]: [_offer(dates[1], 7, 2200), _offer(dates[1], 5, 1700)],
        })
        seen: list[tuple[date, int]] = []
        offers = pc.collect_profile(prov, p, dates, limit=10, delay=0,
                                    progress=lambda d, n: seen.append((d, n)))

        self.assertEqual(len(prov.calls), 2)
        self.assertEqual([c.date_from for c in prov.calls], dates)
        self.assertEqual(seen, [(dates[0], 1), (dates[1], 2)])
        self.assertEqual(len(offers), 3)

        grid = pc.aggregate(offers, dates=dates, nights_range=(p.nights_min, p.nights_max))
        self.assertEqual(pc.best_cell(grid).departure_date, dates[1])


class HotelPayloadTestCase(unittest.TestCase):
    def test_payload_carries_hotel_id_and_drops_country_filter(self):
        p = _profile()
        payload = pc.hotel_payload(p, date(2026, 9, 21), "23141", page=2, limit=30)
        params = payload[0]["params"]
        self.assertEqual(payload[0]["method"], "search.tripsSearch")
        self.assertEqual(params["hotelId"], ["23141"])
        self.assertEqual(params["countryId"], [])
        self.assertEqual(params["query"]["departureDate"], "2026-09-21")
        self.assertEqual(params["query"]["pageNumber"], 2)
        self.assertEqual(params["query"]["duration"], {"min": 5, "max": 7})
        self.assertEqual(params["query"]["rooms"][0]["adult"], 2)


# ------------------------------------------------------------ zapis/odczyt

class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "kalendarz.db"
        self.grid = pc.aggregate([
            _offer(date(2026, 9, 24), 7, 2130),
            _offer(date(2026, 9, 25), 7, 2200),
            _offer(date(2026, 9, 24), 5, 1900),
        ])

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_roundtrip(self):
        stamp = pc.save_calendar(self.db_path, "test-profil", self.grid)
        read_stamp, grid = pc.load_calendar(self.db_path, "test-profil")
        self.assertEqual(read_stamp, stamp)
        self.assertEqual(set(grid), set(self.grid))
        cell = grid[(date(2026, 9, 24), 7)]
        self.assertEqual(cell.price_pp, 2130)
        self.assertAlmostEqual(cell.price_ppn, 2130 / 7, places=2)
        self.assertEqual(pc.best_cell(grid).departure_date, date(2026, 9, 24))

    def test_schema_is_idempotent_and_has_the_agreed_columns(self):
        pc.save_calendar(self.db_path, "test-profil", self.grid)
        pc.save_calendar(self.db_path, "test-profil", self.grid,
                         checked_at="2026-08-31T10:00:00")
        with pc._connect(self.db_path) as db:
            pc.ensure_schema(db)          # drugi (i trzeci) raz — musi przejść
            cols = [r[1] for r in db.execute("PRAGMA table_info(price_calendar)")]
        self.assertEqual(cols, ["profile", "hotel_id", "departure_date", "nights",
                                "price_pp", "price_ppn", "checked_at"])

    def test_runs_are_append_only_and_latest_wins(self):
        """Kolejny przebieg nie kasuje poprzedniego — historia zostaje,
        a domyślny odczyt bierze najświeższy `checked_at`."""
        old = pc.save_calendar(self.db_path, "test-profil", self.grid,
                               checked_at="2026-08-30T09:00:00")
        cheaper = pc.aggregate([_offer(date(2026, 9, 24), 7, 1990)])
        new = pc.save_calendar(self.db_path, "test-profil", cheaper,
                               checked_at="2026-08-31T09:00:00")

        stamp, grid = pc.load_calendar(self.db_path, "test-profil")
        self.assertEqual(stamp, new)
        self.assertEqual(grid[(date(2026, 9, 24), 7)].price_pp, 1990)

        _, old_grid = pc.load_calendar(self.db_path, "test-profil", checked_at=old)
        self.assertEqual(old_grid[(date(2026, 9, 24), 7)].price_pp, 2130)
        self.assertEqual(len(old_grid), 3)

    def test_hotel_rows_do_not_mix_with_profile_rows(self):
        pc.save_calendar(self.db_path, "test-profil", self.grid)
        hotel_grid = pc.aggregate([_offer(date(2026, 9, 24), 7, 3300, hotel_id="23141")])
        pc.save_calendar(self.db_path, "test-profil", hotel_grid, hotel_id="23141")

        _, all_hotels = pc.load_calendar(self.db_path, "test-profil")
        _, one_hotel = pc.load_calendar(self.db_path, "test-profil", hotel_id="23141")
        self.assertEqual(len(all_hotels), 3)
        self.assertEqual(len(one_hotel), 1)
        self.assertEqual(one_hotel[(date(2026, 9, 24), 7)].price_pp, 3300)

    def test_load_of_unknown_profile_is_empty_not_an_error(self):
        self.assertEqual(pc.load_calendar(self.db_path, "nie-ma-takiego"), (None, {}))


if __name__ == "__main__":
    unittest.main()

"""Testy deduplikacji hoteli. Bez sieci i bez plików na dysku."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from holiday_searcher.dedup import (  # noqa: E402
    AMBIGUOUS_THRESHOLD, AUTO_THRESHOLD, AliasStore, Hotel, canonical_name,
    hotels_from_offers, match_hotels, similarity, strip_diacritics,
)
from holiday_searcher.models import Offer  # noqa: E402


def offer(provider: str, hotel_id: str, name: str, region: str = "Riwiera Turecka",
          country: str = "Turcja", price: int = 3000) -> Offer:
    return Offer(
        provider=provider, hotel_name=name, hotel_id=hotel_id, tour_operator="X",
        country=country, region=region, city=region, stars=4.0,
        departure_date=date(2026, 9, 1), return_date=date(2026, 9, 8), nights=7,
        board="AI", board_raw="All inclusive", departure_place="Warszawa",
        departure_code="WAW", room_type="", price=price, price_old=0,
        rating=None, rating_count=None, url="", raw_id=hotel_id,
    )


class TestKanonizacja(unittest.TestCase):
    def test_usuwa_diakrytyki(self):
        self.assertEqual(strip_diacritics("Żółć Łódź ĄĘŚĆ"), "Zolc Lodz AESC")

    def test_lowercase_i_interpunkcja(self):
        self.assertEqual(canonical_name("Grand-Bali, Kleopatra!"), "grand bali kleopatra")

    def test_usuwa_gwiazdki(self):
        self.assertEqual(canonical_name("Asrin Beach 4*"), "asrin beach")
        self.assertEqual(canonical_name("Asrin Beach *****"), "asrin beach")
        self.assertEqual(canonical_name("Asrin Beach ★★★★"), "asrin beach")

    def test_usuwa_slowa_szumowe(self):
        self.assertEqual(canonical_name("Sey Beach Hotel & SPA"), "sey beach")
        self.assertEqual(canonical_name("Hotel Club Sey Beach Resort"), "sey beach")

    def test_rozne_zapisy_daja_te_sama_postac(self):
        self.assertEqual(canonical_name("Sey Beach Hotel & SPA"),
                         canonical_name("HOTEL Sey-Beach Spa 4*"))

    def test_nie_zjada_calej_nazwy(self):
        # gdyby wyciąć wszystko, każdy "Hotel Spa" byłby tym samym obiektem
        self.assertEqual(canonical_name("Hotel Spa"), "hotel spa")

    def test_pusta_nazwa(self):
        self.assertEqual(canonical_name(""), "")
        self.assertEqual(similarity("", "cokolwiek"), 0.0)


class TestProgi(unittest.TestCase):
    def _match(self, a: str, b: str, region_b: str = "Riwiera Turecka"):
        left = [Hotel("wakacje.pl", "1", a, "Turcja", "Riwiera Turecka")]
        right = [Hotel("r.pl", "2", b, "Turcja", region_b)]
        return match_hotels(left, right)

    def test_identyczne_to_auto(self):
        m = self._match("Sey Beach Hotel & SPA", "Hotel Sey Beach Spa 4*")
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0].status, "auto")
        self.assertEqual(m[0].ratio, 1.0)

    def test_powyzej_progu_to_auto(self):
        m = self._match("Xperia Grand Bali Hotel", "Xperia Grand Bali")
        self.assertEqual(m[0].status, "auto")
        self.assertGreaterEqual(m[0].confidence, AUTO_THRESHOLD)

    def test_srednie_podobienstwo_to_ambiguous(self):
        m = self._match("Kleopatra Micador", "Kleopatra Ada Beach")
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0].status, "ambiguous")
        self.assertGreaterEqual(m[0].confidence, AMBIGUOUS_THRESHOLD)
        self.assertLess(m[0].confidence, AUTO_THRESHOLD)

    def test_ponizej_progu_brak_pary(self):
        self.assertEqual(self._match("Sey Beach", "Rixos Premium Belek"), [])

    def test_rozne_kraje_nigdy_sie_nie_lacza(self):
        left = [Hotel("wakacje.pl", "1", "Sey Beach", "Turcja", "Riwiera Turecka")]
        right = [Hotel("r.pl", "2", "Sey Beach", "Egipt", "Hurghada")]
        self.assertEqual(match_hotels(left, right), [])

    def test_inny_region_ten_sam_kraj_obniza_pewnosc(self):
        same = self._match("Sey Beach Hotel", "Sey Beach Hotel")[0]
        other = self._match("Sey Beach Hotel", "Sey Beach Hotel", region_b="Marmaris")[0]
        self.assertTrue(same.same_region)
        self.assertFalse(other.same_region)
        self.assertLess(other.confidence, same.confidence)
        self.assertEqual(other.ratio, same.ratio)

    def test_kara_za_region_moze_zepchnac_do_ambiguous(self):
        m = self._match("Club Hotel Falcon", "Falcon Hotel Club Aqua",
                        region_b="Wybrzeże Egejskie")[0]
        self.assertGreaterEqual(m.ratio, AMBIGUOUS_THRESHOLD)
        self.assertEqual(m.status, "ambiguous")

    def test_jeden_hotel_tylko_w_jednej_parze(self):
        left = [Hotel("wakacje.pl", "1", "Sey Beach Hotel", "Turcja", "Riwiera Turecka")]
        right = [Hotel("r.pl", "2", "Sey Beach Hotel", "Turcja", "Riwiera Turecka"),
                 Hotel("r.pl", "3", "Sey Beach Hotel", "Turcja", "Riwiera Turecka")]
        m = match_hotels(left, right)
        self.assertEqual(len(m), 1)


class TestHotelsFromOffers(unittest.TestCase):
    def test_zwija_oferty_do_hoteli(self):
        offers = [offer("r.pl", "7451", "Supreme Beach"),
                  offer("r.pl", "7451", "Supreme Beach", price=2900),
                  offer("r.pl", "8260", "Kaya Maris")]
        hotels = hotels_from_offers(offers)
        self.assertEqual(len(hotels), 2)
        self.assertEqual({h.hotel_id for h in hotels}, {"7451", "8260"})


class TestAliasStore(unittest.TestCase):
    def setUp(self):
        self.store = AliasStore(sqlite3.connect(":memory:"))

    def tearDown(self):
        self.store.close()

    def test_tabela_powstaje_sama(self):
        row = self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hotel_alias'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_zapisuje_obie_strony_pod_wspolnym_id(self):
        m = match_hotels(
            [Hotel("wakacje.pl", "1", "Sey Beach Hotel & SPA", "Turcja", "Riwiera Turecka")],
            [Hotel("r.pl", "2", "Hotel Sey Beach Spa", "Turcja", "Riwiera Turecka")],
        )
        self.assertEqual(self.store.save_matches(m), 2)
        rows = self.store.aliases()
        self.assertEqual(len({r["canonical_id"] for r in rows}), 1)
        self.assertEqual({r["provider"] for r in rows}, {"wakacje.pl", "r.pl"})
        self.assertEqual({r["status"] for r in rows}, {"auto"})

    def test_ambiguous_ma_wlasny_status(self):
        m = match_hotels(
            [Hotel("wakacje.pl", "1", "Kleopatra Micador", "Turcja", "Riwiera Turecka")],
            [Hotel("r.pl", "2", "Kleopatra Ada Beach", "Turcja", "Riwiera Turecka")],
        )
        self.store.save_matches(m)
        self.assertEqual(len(self.store.aliases(status="ambiguous")), 2)
        self.assertEqual(len(self.store.aliases(status="auto")), 0)

    def test_ponowny_zapis_nie_duplikuje(self):
        m = match_hotels(
            [Hotel("wakacje.pl", "1", "Supreme Beach", "Turcja", "Riwiera Turecka")],
            [Hotel("r.pl", "2", "Supreme Beach Hotel", "Turcja", "Riwiera Turecka")],
        )
        self.store.save_matches(m)
        self.store.save_matches(m)
        self.assertEqual(len(self.store.aliases()), 2)

    def test_status_ograniczony_do_dwoch_wartosci(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute(
                """INSERT INTO hotel_alias VALUES
                   ('id','p','1','n','n','Turcja','R',0.9,'pewniak','2026-01-01')""")


if __name__ == "__main__":
    unittest.main(verbosity=2)

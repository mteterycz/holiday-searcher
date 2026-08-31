"""Mapowanie odpowiedzi r.pl na model kanoniczny. Bez sieci — na zapisanej próbce
tests/data/rpl_sample.json (dwa warianty tego samego zapytania: z ceną za osobę
i z ceną za całą grupę)."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from holiday_searcher.models import BOARD_TIERS, SearchProfile  # noqa: E402
from holiday_searcher.providers.rpl import RplProvider, board_code  # noqa: E402

SAMPLE = json.loads((ROOT / "tests" / "data" / "rpl_sample.json").read_text(encoding="utf-8"))

PROFILE = SearchProfile(
    name="test", country="turcja",
    date_from=date(2026, 8, 31), date_to=date(2026, 9, 11),
    nights_min=7, nights_max=8, boards=["AI", "UAI", "AI_PLUS", "AI_SOFT"],
    adults=2, children_ages=[], stars_min=4, max_price_pp=None,
    departures=[], regions=[],
)


def map_sample(variant: str, persons: int = 2, profile: SearchProfile = PROFILE):
    by_date = {r["Id"]: r["TerminWyjazdu"] for r in SAMPLE[f"search_{variant}"]["Wynik"]}
    return [RplProvider._map(b, by_date.get(b["Klucz"]), persons, profile)
            for b in SAMPLE[f"bloczki_{variant}"]]


class TestMapowanie(unittest.TestCase):
    def test_wszystkie_bloczki_sie_mapuja(self):
        offers = map_sample("avg")
        self.assertTrue(offers)
        self.assertTrue(all(o is not None for o in offers))

    def test_pola_podstawowe(self):
        o = map_sample("avg")[0]
        self.assertEqual(o.provider, "r.pl")
        self.assertEqual(o.tour_operator, "Rainbow")
        self.assertEqual(o.country, "Turcja")
        self.assertTrue(o.hotel_name)
        self.assertTrue(o.hotel_id)
        self.assertTrue(o.region)
        self.assertGreaterEqual(o.stars, 4)
        self.assertTrue(o.url.startswith("https://r.pl/"))

    def test_daty_sa_datami_i_trzymaja_sie_liczby_nocy(self):
        for o in map_sample("avg"):
            self.assertIsInstance(o.departure_date, date)
            self.assertIsInstance(o.return_date, date)
            self.assertEqual((o.return_date - o.departure_date).days, o.nights)

    def test_wyzywienie_w_kodach_kanonicznych(self):
        for o in map_sample("avg"):
            self.assertIn(o.board, BOARD_TIERS)
            self.assertTrue(o.board_raw)

    def test_cena_za_osobe_zostaje_bez_zmian(self):
        offers = map_sample("avg")
        surowe = [b["Cena"]["Cena"] for b in SAMPLE["bloczki_avg"]]
        self.assertEqual([o.price for o in offers], surowe)

    def test_cena_za_grupe_jest_dzielona_przez_liczbe_osob(self):
        """Sedno normalizacji: bez tego r.pl wyglądałby na dwa razy droższy."""
        per_person = {o.hotel_id: o.price for o in map_sample("avg")}
        total = {o.hotel_id: o.price for o in map_sample("total", persons=2)}
        wspolne = set(per_person) & set(total)
        self.assertTrue(wspolne, "próbki muszą zawierać te same hotele")
        for hid in wspolne:
            # dzielenie i zaokrąglenie mogą dać ±1 zł różnicy
            self.assertLessEqual(abs(per_person[hid] - total[hid]), 1)

    def test_ocena_przeskalowana_z_szostkowej_na_dziesietna(self):
        for o, b in zip(map_sample("avg"), SAMPLE["bloczki_avg"]):
            raw = (b.get("Ocena") or {}).get("Ocena")
            if o.rating is None:
                continue
            self.assertLessEqual(o.rating, 10.0)
            self.assertAlmostEqual(o.rating, round(raw * 10 / 6, 2), places=2)

    def test_brak_ocen_to_none_a_nie_zero(self):
        o = RplProvider._map(
            {"Klucz": "1_2:3:4", "TerminWyjazdu": "2026-09-01T00:00:00Z",
             "BazoweInformacje": {"HotelId": 1, "NazwaHoteluWWW": "Testowy",
                                  "LiczbaNocy": 7, "GwiazdkiHotelu": 4,
                                  "Panstwa": ["Turcja"], "Regiony": ["Riwiera Turecka"],
                                  "Lokalizacje": "Turcja: Alanya", "OfertaUrl": "/x"},
             "Cena": {"Cena": 3000, "CenaPrzedPromocja": 3000, "CzyCenaZaOsobe": True},
             "Ocena": {"Ocena": 0.0, "IloscOcen": 0, "CzyPokazywac": False},
             "Wyzywienia": [{"Nazwa": "All inclusive"}], "Przystanki": []},
            "2026-09-01T00:00:00Z", 2, PROFILE)
        self.assertIsNone(o.rating)
        self.assertIsNone(o.rating_count)
        self.assertEqual(o.city, "Alanya")

    def test_klucz_oferty_jest_stabilny(self):
        a, b = map_sample("avg")[0], map_sample("avg")[0]
        self.assertEqual(a.key, b.key)
        self.assertNotEqual(a.key, map_sample("avg")[1].key)


class TestWyzywienie(unittest.TestCase):
    def test_mapowanie_nazw(self):
        self.assertEqual(board_code("All inclusive"), "AI")
        self.assertEqual(board_code("Ultra All Inclusive"), "UAI")
        self.assertEqual(board_code("All inclusive plus"), "AI_PLUS")
        self.assertEqual(board_code("Soft All Inclusive"), "AI_SOFT")
        self.assertEqual(board_code("3 posiłki"), "FB")
        self.assertEqual(board_code("2 posiłki"), "HB")
        self.assertEqual(board_code("Śniadania"), "BB")
        self.assertEqual(board_code("Bez wyżywienia"), "OTHER")
        self.assertEqual(board_code(""), "OTHER")

    def test_wszystkie_kody_istnieja_w_modelu(self):
        for _, code in [("x", board_code(n)) for n in
                        ("All inclusive", "Ultra all inclusive", "3 posiłki", "Śniadania")]:
            self.assertIn(code, BOARD_TIERS)


class TestBudowaZapytania(unittest.TestCase):
    def setUp(self):
        self.prov = RplProvider.__new__(RplProvider)   # bez otwierania klienta HTTP

    def test_kraj_tlumaczony_na_slug(self):
        body = self.prov._payload(PROFILE, 1, 30, "cena-asc")
        self.assertEqual(body["Atrybuty"]["Lokalizacje_HoteloProdukt"], ["europa:turcja"])

    def test_cena_za_osobe_jest_wymuszona(self):
        body = self.prov._payload(PROFILE, 1, 30, "cena-asc")
        self.assertEqual(body["Atrybuty"]["Cena"][0], "avg")

    def test_gwiazdki_i_dlugosc_pobytu(self):
        body = self.prov._payload(PROFILE, 1, 30, "cena-asc")
        self.assertEqual(body["Atrybuty"]["StandardHotelu"], ["8", "10"])
        self.assertEqual(body["Atrybuty"]["DlugoscPobytu"], ["8-9"])  # noce + 1 = dni

    def test_wyzywienie_zwijane_do_koszyka_rpl(self):
        body = self.prov._payload(PROFILE, 1, 30, "cena-asc")
        self.assertEqual(body["Atrybuty"]["Wyzywienia"], ["all-inclusive"])

    def test_nieznany_kraj_konczy_sie_bledem(self):
        zly = SearchProfile(**{**PROFILE.__dict__, "country": "atlantyda"})
        with self.assertRaises(ValueError):
            self.prov._payload(zly, 1, 30, "cena-asc")

    def test_daty_urodzenia_odpowiadaja_liczbie_osob(self):
        rodzina = SearchProfile(**{**PROFILE.__dict__, "adults": 2, "children_ages": [7]})
        self.assertEqual(len(self.prov._payload(rodzina, 1, 30, "cena-asc")["DatyUrodzenia"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

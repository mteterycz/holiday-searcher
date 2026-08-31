"""Testy drugiego źródła opinii (HolidayCheck) — BEZ SIECI.

Wszystko stoi na zapisanych próbkach w `tests/data/`:
  * `holidaycheck_hotel.html`            — hotel z oceną (JSON-LD + recenzje),
  * `holidaycheck_hotel_bez_opinii.html` — hotel BEZ ani jednej opinii,
  * `holidaycheck_suggest.json`          — odpowiedź `suggestionSearch`.
Próbki to realne odpowiedzi serwisu przycięte do bloku JSON-LD (patrz
docs/opinie-zewnetrzne.md). Baza SQLite jest tworzona w katalogu tymczasowym.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from holiday_searcher.external_ratings import (
    ST_AMBIGUOUS, ST_ERROR, ST_NO_MATCH, ST_NO_RATING, ST_OK,
    ExternalRating, ExternalRatingStore, HolidayCheckRatings,
    get_or_fetch, name_similarity, normalize_name, normalize_to_10,
    parse_hotel_page, parse_suggestions, pick_match, place_agreement, reliability,
    search_query,
)

DATA = Path(__file__).parent / "data"


def sample(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


# --------------------------------------------------------------- parsowanie HTML

class TestParsowanieStrony(unittest.TestCase):
    def test_wyciaga_ocene_i_liczbe_opinii(self):
        r = parse_hotel_page(sample("holidaycheck_hotel.html"))
        self.assertEqual(r.status, ST_OK)
        self.assertEqual(r.rating, 6.2)
        self.assertEqual(r.review_count, 4)
        self.assertEqual(r.matched_name, "Alkyonides Boutique Hotel")

    def test_url_rozkodowany_z_escapowanych_ukosnikow(self):
        """JSON-LD serwuje ukośniki jako \\u002F — bez podmiany URL jest bezużyteczny."""
        r = parse_hotel_page(sample("holidaycheck_hotel.html"))
        self.assertTrue(r.url.startswith("https://www.holidaycheck.de/hi/"))
        self.assertNotIn("u002F", r.url)

    def test_wyciaga_teksty_opinii(self):
        r = parse_hotel_page(sample("holidaycheck_hotel.html"))
        self.assertTrue(r.reviews)
        self.assertLessEqual(len(r.reviews), 5)
        self.assertIn("dreckig", " ".join(r.reviews))

    def test_limit_liczby_opinii(self):
        r = parse_hotel_page(sample("holidaycheck_hotel.html"), max_reviews=2)
        self.assertLessEqual(len(r.reviews), 2)

    def test_hotel_bez_opinii_to_nie_awaria(self):
        """Brak `aggregateRating` przy poprawnym JSON-LD = normalny stan."""
        r = parse_hotel_page(sample("holidaycheck_hotel_bez_opinii.html"))
        self.assertEqual(r.status, ST_NO_RATING)
        self.assertIsNone(r.rating)
        self.assertEqual(r.matched_name, "Hotel Olympia")

    def test_smiec_zamiast_strony_daje_error(self):
        r = parse_hotel_page("<html><body>nic tu nie ma</body></html>")
        self.assertEqual(r.status, ST_ERROR)
        self.assertIsNone(r.rating)

    def test_pusty_html_nie_wybucha(self):
        self.assertEqual(parse_hotel_page("").status, ST_ERROR)


# ------------------------------------------------------------------ normalizacja

class TestNormalizacjaSkali(unittest.TestCase):
    def test_skala_10_jest_tozsamoscia(self):
        self.assertEqual(normalize_to_10("6.2", "10", "1"), 6.2)

    def test_stara_skala_1_6_przeliczana(self):
        """HolidayCheck historycznie używał skali 1-6 — dzielimy przez realne
        `bestRating`, a nie przez zaszytą dziesiątkę."""
        self.assertEqual(normalize_to_10(5.4, 6), 9.0)
        self.assertEqual(normalize_to_10(3.0, 6), 5.0)

    def test_skala_5_gwiazdkowa(self):
        self.assertEqual(normalize_to_10(4.0, 5), 8.0)

    def test_przecinek_dziesietny(self):
        self.assertEqual(normalize_to_10("7,5", "10"), 7.5)

    def test_wynik_zawsze_w_zakresie(self):
        self.assertEqual(normalize_to_10(99, 10), 10.0)
        self.assertEqual(normalize_to_10(-5, 10), 0.0)

    def test_smieci_daja_none(self):
        self.assertIsNone(normalize_to_10(None))
        self.assertIsNone(normalize_to_10("brak"))
        self.assertIsNone(normalize_to_10(5, 0))

    def test_ocena_z_probki_jest_w_skali_0_10(self):
        r = parse_hotel_page(sample("holidaycheck_hotel.html"))
        self.assertGreaterEqual(r.rating, 0.0)
        self.assertLessEqual(r.rating, 10.0)


# --------------------------------------------------------------- dopasowanie nazw

class TestNormalizacjaNazw(unittest.TestCase):
    def test_wycina_nawiasy_i_slowa_generyczne(self):
        self.assertEqual(normalize_name("Olympia (Pefkohori)"), "olympia")
        self.assertEqual(normalize_name("Hotel Venus Beach"), "venus beach")

    def test_wycina_ogon_po_ex(self):
        self.assertEqual(normalize_name("Kirbiyik Resort (ex. Dinler)"), "kirbiyik")

    def test_splaszcza_znaki_diakrytyczne(self):
        self.assertEqual(normalize_name("Marmárion"), "marmarion")
        self.assertEqual(normalize_name("Türkei"), "turkei")


class TestPodobienstwoNazw(unittest.TestCase):
    def test_identyczne_po_normalizacji(self):
        self.assertEqual(name_similarity("Venus Beach", "Hotel Venus Beach"), 1.0)

    def test_nazwa_dluzsza_o_dopisek_przechodzi_prog(self):
        """`Alkyonides` vs `Alkyonides Boutique Hotel` — sam SequenceMatcher daje
        0.69, ale zawieranie się kompletu słów podnosi wynik ponad próg."""
        s = name_similarity("Alkyonides (Kremasti)", "Alkyonides Boutique Hotel")
        self.assertGreaterEqual(s, 0.80)
        self.assertLess(s, 1.0)

    def test_rozne_hotele_nie_przechodza_progu(self):
        self.assertLess(name_similarity("Alkyonides", "Kremasti Memories"), 0.80)
        self.assertLess(name_similarity("Ionian View Studios", "Olive Grove Studios"), 0.80)

    def test_beach_i_garden_to_rozne_hotele(self):
        """`beach`/`garden` NIE są słowami generycznymi — rozróżniają obiekty."""
        self.assertLess(name_similarity("Venus Beach", "Venus Garden"), 0.80)

    def test_pusta_nazwa_daje_zero(self):
        self.assertEqual(name_similarity("", "Cokolwiek"), 0.0)
        self.assertEqual(name_similarity("Hotel", "Resort"), 0.0)


class TestFrazaWyszukiwania(unittest.TestCase):
    """Realny błąd wyłapany na żywych danych: `Alkyonides (Kremasti)` + miasto
    daje frazę, w której miasto waży dwa razy więcej niż nazwa hotelu, i
    HolidayCheck NIE zwraca wtedy szukanego obiektu w ogóle."""

    def test_miasto_z_nawiasu_nie_jest_dublowane(self):
        self.assertEqual(search_query("Alkyonides (Kremasti)", "Kremasti", "Rodos"),
                         "Alkyonides Kremasti")

    def test_miasto_juz_w_nazwie_nie_jest_doklejane(self):
        self.assertEqual(search_query("Novotel Malta Sliema", "Sliema", ""),
                         "Novotel Malta Sliema")

    def test_ogon_po_ex_wycinany(self):
        self.assertEqual(search_query("Kirbiyik Resort (ex. Dinler)", "Mahmutlar", ""),
                         "Kirbiyik Resort Mahmutlar")

    def test_region_gdy_brak_miasta(self):
        self.assertEqual(search_query("Marmari Bay", "", "Evia"), "Marmari Bay Evia")

    def test_sama_nazwa_gdy_brak_miejsca(self):
        self.assertEqual(search_query("Brancamaria", "", ""), "Brancamaria")

    def test_pusta_nazwa_daje_pusta_fraze(self):
        self.assertEqual(search_query("", "", ""), "")


class TestZgodnoscMiejsca(unittest.TestCase):
    PLACE = "Hotel in Kremasti, Rhodos, Griechenland"

    def test_kraj_i_miasto_sie_zgadzaja(self):
        kraj, miejsce = place_agreement("Grecja", "Kremasti", "Rodos", self.PLACE)
        self.assertTrue(kraj)
        self.assertTrue(miejsce)

    def test_inny_kraj_nie_przechodzi(self):
        kraj, _ = place_agreement("Turcja", "Kremasti", "", self.PLACE)
        self.assertFalse(kraj)

    def test_region_ratuje_gdy_miasta_brak(self):
        kraj, miejsce = place_agreement(
            "Grecja", "", "Evia", "Hotel in Marmárion, Evia / Euböa, Griechenland")
        self.assertTrue(kraj)
        self.assertTrue(miejsce)

    def test_sasiednie_miasto_nie_potwierdza_miejsca(self):
        kraj, miejsce = place_agreement(
            "Malta", "Sliema", "Wyspa Malta", "Hotel in Gzira, Majjistral, Malta")
        self.assertTrue(kraj)
        self.assertFalse(miejsce)


class TestWyborKandydata(unittest.TestCase):
    def kandydaci(self):
        return parse_suggestions(json.loads(sample("holidaycheck_suggest.json")))

    def test_probka_ma_kandydatow(self):
        c = self.kandydaci()
        self.assertTrue(c)
        self.assertTrue(all({"id", "name", "place"} <= set(x) for x in c))

    def test_wybiera_wlasciwy_hotel_z_probki(self):
        best, conf, status = pick_match(
            "Alkyonides (Kremasti)", "Grecja", "Kremasti", "Rodos", self.kandydaci())
        self.assertEqual(status, ST_OK)
        self.assertIn("Alkyonides", best["name"])
        self.assertGreaterEqual(conf, 0.80)

    def test_inny_kraj_odrzucany_mimo_idealnej_nazwy(self):
        """Realny fałszywy trop: `Ambrosia (Athens)` dostaje z HolidayCheck
        `Hotel Ambrosia` w Turcji z podobieństwem nazwy 1.00."""
        cands = [{"id": "x", "name": "Hotel Ambrosia",
                  "place": "Hotel in Bitez, Türkische Ägäis, Türkei"}]
        best, conf, status = pick_match("Ambrosia (Athens)", "Grecja", "Athens", "", cands)
        self.assertIsNone(best)
        self.assertEqual(status, ST_NO_MATCH)

    def test_slaba_nazwa_daje_ambiguous(self):
        cands = [{"id": "x", "name": "Studios & Apartments Veronica",
                  "place": "Hotel in Moraitika, Korfu, Griechenland"}]
        best, conf, status = pick_match(
            "Ionian View Studios", "Grecja", "Moraitika", "Korfu", cands)
        self.assertEqual(status, ST_AMBIGUOUS)
        self.assertIsNotNone(best)
        self.assertLess(conf, 0.80)

    def test_idealna_nazwa_bez_miasta_wciaz_przechodzi(self):
        cands = [{"id": "x", "name": "Novotel Malta Sliema",
                  "place": "Hotel in Gzira, Majjistral, Malta"}]
        _, conf, status = pick_match(
            "Novotel Malta Sliema", "Malta", "Sliema", "Wyspa Malta", cands)
        self.assertEqual(status, ST_OK)
        self.assertGreaterEqual(conf, 0.97)

    def test_brak_kandydatow(self):
        best, conf, status = pick_match("Cokolwiek", "Grecja", "", "", [])
        self.assertIsNone(best)
        self.assertEqual(status, ST_NO_MATCH)
        self.assertEqual(conf, 0.0)

    def test_blizniak_w_tej_samej_miejscowosci_daje_ambiguous(self):
        """W Platamonas stoją obok siebie dwa różne hotele „Sun Beach".
        Baza ofert zna tylko `Sun Beach (Platamonas)` — nie ma czym wybrać."""
        cands = [
            {"id": "a", "name": "Hotel Sun Beach",
             "place": "Hotel in Platamonas, Griechisches Festland, Griechenland"},
            {"id": "b", "name": "Sun Beach Platamon Resort",
             "place": "Hotel in Platamonas, Griechisches Festland, Griechenland"},
        ]
        _, _, status = pick_match(
            "Sun Beach (Platamonas)", "Grecja", "Platamonas", "Riwiera Olimpijska", cands)
        self.assertEqual(status, ST_AMBIGUOUS)

    def test_rodzina_hoteli_o_wspolnej_nazwie_daje_ambiguous(self):
        cands = [
            {"id": "a", "name": "Hotel Karbel", "place": "Hotel in Ölüdeniz, Türkische Ägäis, Türkei"},
            {"id": "b", "name": "Hotel Karbel Sun", "place": "Hotel in Ölüdeniz, Türkische Ägäis, Türkei"},
            {"id": "c", "name": "Hotel Karbel Beach", "place": "Hotel in Ölüdeniz, Türkische Ägäis, Türkei"},
        ]
        _, _, status = pick_match("Karbel", "Turcja", "Oludeniz", "Wybrzeże Likijskie", cands)
        self.assertEqual(status, ST_AMBIGUOUS)

    def test_slaby_rywal_nie_psuje_pewnego_trafienia(self):
        cands = [
            {"id": "a", "name": "Hotel Grand Zaman Garden",
             "place": "Hotel in Alanya, Türkische Riviera, Türkei"},
            {"id": "b", "name": "Kleopatra Celine",
             "place": "Hotel in Alanya, Türkische Riviera, Türkei"},
        ]
        best, _, status = pick_match(
            "Grand Zaman Garden", "Turcja", "Alanya", "Riwiera Turecka", cands)
        self.assertEqual(status, ST_OK)
        self.assertEqual(best["id"], "a")

    def test_imiennik_z_innej_miejscowosci_nie_jest_rywalem(self):
        """`Alkyonides Boutique Hotel` (Kremasti, Rodos) ma imiennika
        `Hotel Alcionides / Alkyonides` w Stalis na Krecie. Nazwy nie do
        odróżnienia, ale miejscowość owszem — więc trafienie zostaje pewne."""
        best, _, status = pick_match(
            "Alkyonides (Kremasti)", "Grecja", "Kremasti", "Rodos",
            parse_suggestions(json.loads(sample("holidaycheck_suggest.json"))))
        self.assertEqual(status, ST_OK)
        self.assertEqual(best["name"], "Alkyonides Boutique Hotel")

    def test_rywal_z_innego_kraju_sie_nie_liczy(self):
        """`Coral Sun Beach` w Egipcie odpada na filtrze kraju, więc nie może
        zepsuć trafienia w Grecji."""
        cands = [
            {"id": "a", "name": "Hotel Sun Beach",
             "place": "Hotel in Platamonas, Griechisches Festland, Griechenland"},
            {"id": "b", "name": "Coral Sun Beach", "place": "Hotel in Safaga, Ägypten"},
        ]
        _, _, status = pick_match(
            "Sun Beach (Platamonas)", "Grecja", "Platamonas", "Riwiera Olimpijska", cands)
        self.assertEqual(status, ST_OK)


# ---------------------------------------------------------------- wiarygodność

class TestWiarygodnosc(unittest.TestCase):
    @staticmethod
    def zew(rating, count, status=ST_OK):
        return ExternalRating(rating=rating, review_count=count, status=status)

    def test_rozjazd_powyzej_progu_to_flaga_i_niska_pewnosc(self):
        """Kanoniczny przypadek: 10.0 z 1 opinii vs 6.2 z 4."""
        r = reliability(10.0, 1, self.zew(6.2, 4))
        self.assertTrue(r.divergent)
        self.assertTrue(r.thin)
        self.assertEqual(r.level, "niska")
        self.assertAlmostEqual(r.diff, 3.8, places=2)

    def test_rozjazd_dokladnie_na_progu_nie_jest_flaga(self):
        r = reliability(8.7, 42, self.zew(7.2, 42))
        self.assertAlmostEqual(r.diff, 1.5, places=2)
        self.assertFalse(r.divergent)

    def test_zgodne_oceny_i_duzo_opinii_daja_wysoka(self):
        r = reliability(9.2, 1, self.zew(9.4, 46))
        self.assertEqual(r.level, "wysoka")
        self.assertFalse(r.divergent)
        self.assertTrue(r.thin)          # lokalnie wciąż 1 opinia — to zostaje widoczne

    def test_zgodne_ale_malo_opinii_lacznie(self):
        r = reliability(8.6, 1, self.zew(8.6, 1))
        self.assertEqual(r.level, "niska")
        self.assertFalse(r.divergent)

    def test_srednia_przy_umiarkowanej_liczbie_opinii(self):
        r = reliability(8.4, 5, self.zew(8.2, 12))
        self.assertEqual(r.level, "średnia")

    def test_rozjazd_bije_liczbe_opinii(self):
        """Dwa źródła z setkami opinii, które się kłócą, to nadal 'nie wiadomo'."""
        r = reliability(8.9, 300, self.zew(5.0, 335))
        self.assertEqual(r.level, "niska")
        self.assertTrue(r.divergent)

    def test_bez_drugiego_zrodla_sufitem_jest_srednia(self):
        r = reliability(8.8, 151, None)
        self.assertEqual(r.level, "średnia")
        self.assertIsNone(r.diff)
        self.assertFalse(r.divergent)

    def test_ambiguous_nie_jest_uzywane_do_oceny(self):
        r = reliability(10.0, 1, self.zew(6.2, 4, status=ST_AMBIGUOUS))
        self.assertIsNone(r.diff)
        self.assertFalse(r.divergent)
        self.assertEqual(r.level, "niska")
        self.assertIn("niepewne", r.reason)

    def test_hotel_bez_opinii_w_drugim_zrodle(self):
        r = reliability(10.0, 1, self.zew(None, None, status=ST_NO_RATING))
        self.assertEqual(r.level, "niska")
        self.assertIn("bez opinii", r.reason)

    def test_brak_oceny_lokalnej_nie_wybucha(self):
        r = reliability(None, None, self.zew(8.0, 100))
        self.assertIsNone(r.diff)
        self.assertEqual(r.level, "niska")


# ----------------------------------------------------------------------- cache

class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.store = ExternalRatingStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schemat_zakladany_idempotentnie(self):
        ExternalRatingStore(self.path)
        ExternalRatingStore(self.path)
        db = sqlite3.connect(self.path)
        kols = {r[1] for r in db.execute("PRAGMA table_info(hotel_external_rating)")}
        self.assertEqual(kols, {"hotel_id", "source", "matched_name", "rating_0_10",
                                "review_count", "url", "confidence", "status",
                                "fetched_at"})

    def test_zapis_i_odczyt(self):
        self.store.put(ExternalRating(hotel_id="35113", matched_name="Alkyonides Boutique Hotel",
                                      rating=6.2, review_count=4, url="https://x",
                                      confidence=0.93, status=ST_OK))
        got = self.store.get("35113")
        self.assertEqual(got.rating, 6.2)
        self.assertEqual(got.review_count, 4)
        self.assertEqual(got.confidence, 0.93)
        self.assertTrue(got.usable)
        self.assertTrue(got.fetched_at)

    def test_nadpisanie_tego_samego_hotelu(self):
        self.store.put(ExternalRating(hotel_id="1", rating=5.0, status=ST_OK))
        self.store.put(ExternalRating(hotel_id="1", rating=8.0, status=ST_OK))
        self.assertEqual(self.store.get("1").rating, 8.0)
        self.assertEqual(self.store.count(), 1)

    def test_porazki_sa_cache_owane(self):
        """`no_match` to trwały fakt — hotel nie pojawi się na HolidayCheck do jutra."""
        self.store.put(ExternalRating(hotel_id="9", status=ST_NO_MATCH))
        got = self.store.get("9")
        self.assertIsNotNone(got)
        self.assertEqual(got.status, ST_NO_MATCH)
        self.assertFalse(got.usable)

    def test_ambiguous_zapisane_ale_nieuzywalne(self):
        self.store.put(ExternalRating(hotel_id="7", rating=8.2, review_count=1,
                                      status=ST_AMBIGUOUS, confidence=0.47))
        got = self.store.get("7")
        self.assertEqual(got.status, ST_AMBIGUOUS)
        self.assertFalse(got.usable)

    def test_error_traktowany_jak_brak_wpisu(self):
        """Padnięta sieć to stan chwilowy — ma się ponowić przy następnym przebiegu."""
        self.store.put(ExternalRating(hotel_id="5", status=ST_ERROR))
        self.assertIsNone(self.store.get("5"))

    def test_zrodla_nie_mieszaja_sie(self):
        self.store.put(ExternalRating(hotel_id="1", source="holidaycheck",
                                      rating=6.2, status=ST_OK))
        self.store.put(ExternalRating(hotel_id="1", source="inne",
                                      rating=9.9, status=ST_OK))
        self.assertEqual(self.store.get("1", "holidaycheck").rating, 6.2)
        self.assertEqual(self.store.get("1", "inne").rating, 9.9)

    def test_wspoldzielenie_polaczenia(self):
        """Store musi umieć siedzieć na połączeniu Storage z fazy 1."""
        db = sqlite3.connect(self.path)
        s = ExternalRatingStore(db)
        s.put(ExternalRating(hotel_id="2", rating=7.0, status=ST_OK))
        self.assertEqual(s.get("2").rating, 7.0)


class TestGetOrFetch(unittest.TestCase):
    """Przepływ cache -> sieć -> cache, z klientem podmienionym na atrapę."""

    class FakeClient:
        def __init__(self, wynik):
            self.wynik = wynik
            self.wywolania = 0

        def fetch(self, hotel_id, hotel_name, country="", city="", region=""):
            self.wywolania += 1
            out = ExternalRating(**{**self.wynik.__dict__})
            out.hotel_id = str(hotel_id)
            return out

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ExternalRatingStore(Path(self.tmp.name) / "t.db")
        self.client = self.FakeClient(
            ExternalRating(rating=6.2, review_count=4, status=ST_OK,
                           matched_name="Alkyonides Boutique Hotel", confidence=0.93))

    def tearDown(self):
        self.tmp.cleanup()

    def test_drugie_wywolanie_leci_z_cache(self):
        a = get_or_fetch(self.store, self.client, "1", "Alkyonides", "Grecja", "Kremasti")
        b = get_or_fetch(self.store, self.client, "1", "Alkyonides", "Grecja", "Kremasti")
        self.assertEqual(self.client.wywolania, 1)
        self.assertEqual(a.rating, b.rating)

    def test_refresh_omija_cache(self):
        get_or_fetch(self.store, self.client, "1", "Alkyonides", "Grecja", "Kremasti")
        get_or_fetch(self.store, self.client, "1", "Alkyonides", "Grecja", "Kremasti",
                     refresh=True)
        self.assertEqual(self.client.wywolania, 2)

    def test_blad_sieci_ponawiany_przy_kolejnym_przebiegu(self):
        self.client.wynik = ExternalRating(status=ST_ERROR, error="sieć padła")
        get_or_fetch(self.store, self.client, "1", "X", "Grecja")
        get_or_fetch(self.store, self.client, "1", "X", "Grecja")
        self.assertEqual(self.client.wywolania, 2)


class TestDegradacja(unittest.TestCase):
    """Żadna ścieżka błędu nie ma prawa rzucić wyjątku."""

    def test_pusta_nazwa_hotelu(self):
        r = HolidayCheckRatings().fetch("1", "", country="Grecja")
        self.assertEqual(r.status, ST_NO_MATCH)
        self.assertEqual(r.hotel_id, "1")

    def test_parsowanie_pustej_odpowiedzi_suggest(self):
        self.assertEqual(parse_suggestions({}), [])
        self.assertEqual(parse_suggestions({"data": {"suggestionSearch": None}}), [])
        self.assertEqual(parse_suggestions({"errors": [{"message": "boom"}]}), [])

    def test_kandydat_bez_id_pomijany(self):
        payload = {"data": {"suggestionSearch": {"hotels": {"entities": [
            {"name": "Bez id"}, {"id": "a", "name": "Z id"}]}}}}
        self.assertEqual([c["id"] for c in parse_suggestions(payload)], ["a"])

    def test_padnieta_siec_daje_error_a_nie_no_match(self):
        """Kluczowe rozróżnienie: awaria źródła NIE może zapisać się jako trwałe
        „nie ma takiego hotelu", bo jedno padnięcie sieci wyłączyłoby drugie
        źródło dla całego rankingu na stałe."""
        class PadnietyKlient(HolidayCheckRatings):
            def suggest(self, query, limit=8):
                return [], "sieć: ConnectTimeout"

        r = PadnietyKlient(delay=0).fetch("1", "Alkyonides", "Grecja", "Kremasti")
        self.assertEqual(r.status, ST_ERROR)
        self.assertIn("sieć", r.error)

    def test_pusta_lista_bez_bledu_to_no_match(self):
        class PustyKlient(HolidayCheckRatings):
            def suggest(self, query, limit=8):
                return [], ""

        r = PustyKlient(delay=0).fetch("1", "Nieistniejacy", "Grecja", "Gdzies")
        self.assertEqual(r.status, ST_NO_MATCH)

    def test_blad_sieci_nie_zostaje_w_cache(self):
        """Domknięcie: `error` z `fetch` musi być ponawiany przez `get_or_fetch`."""
        class PadnietyKlient(HolidayCheckRatings):
            wywolania = 0

            def suggest(self, query, limit=8):
                type(self).wywolania += 1
                return [], "sieć: padło"

        import tempfile as _tf
        with _tf.TemporaryDirectory() as d:
            store = ExternalRatingStore(Path(d) / "t.db")
            klient = PadnietyKlient(delay=0)
            get_or_fetch(store, klient, "1", "X", "Grecja", "Y")
            get_or_fetch(store, klient, "1", "X", "Grecja", "Y")
            self.assertEqual(PadnietyKlient.wywolania, 2)


if __name__ == "__main__":
    unittest.main()

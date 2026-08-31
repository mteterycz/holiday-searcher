"""Testy źródła Google Places API (New) — BEZ SIECI i BEZ KLUCZA.

Wszystko stoi na próbkach w `tests/data/google_places_*.json`. Próbki są
**SYNTETYCZNE**: w chwili pisania tego kodu klucza API nie było, więc nie dało
się zapisać realnej odpowiedzi. Kształt (nazwy pól, zagnieżdżenia, format
błędu) pochodzi wprost z dokumentacji Google:

  * .../places/web-service/text-search   — POST places:searchText,
  * .../places/web-service/place-details — GET places/{id},
  * .../reference/rest/v1/places[.searchText] — pełny schemat odpowiedzi,

a wartości dobrano tak, by odtworzyć realne pułapki dopasowania opisane
w `docs/opinie-zewnetrzne.md`. Każdy plik ma to zaznaczone w kluczu `_probka`.
Po wgraniu prawdziwego klucza warto podmienić próbki na zapis realnej
odpowiedzi — testy nie powinny wtedy zmienić wyniku.

Warstwa HTTP jest testowana przez `httpx.MockTransport`, więc żaden test
nie dotyka sieci ani nie potrzebuje `GOOGLE_PLACES_API_KEY`.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx

from holiday_searcher.external_google import (
    API_KEY_NAME, DETAILS_FIELD_MASK, GOOGLE_BEST_RATING, MAX_REVIEWS,
    SEARCH_FIELD_MASK, SEARCH_URL, SOURCE, GooglePlacesRatings, _query,
    country_matches, is_lodging, parse_error, parse_places, parse_reviews,
    pick_place, place_matches, to_rating,
)
from holiday_searcher.external_ratings import (
    MIN_CALIBRATION_PAIRS, ST_AMBIGUOUS, ST_ERROR, ST_NO_KEY, ST_NO_MATCH,
    ST_NO_RATING, ST_OK, ExternalRating, ExternalRatingStore, calibrate,
    get_or_fetch, offsets_map, reliability_multi,
)

DATA = Path(__file__).parent / "data"


def sample(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def client(handler, **kw) -> GooglePlacesRatings:
    """Klient z podstawioną warstwą transportu — zero sieci, zero czekania."""
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return GooglePlacesRatings(api_key="TESTOWY", delay=0, http=http, **kw)


# ------------------------------------------------------------ parsowanie

class TestParsowanieWynikow(unittest.TestCase):
    def setUp(self):
        self.places = parse_places(sample("google_places_searchtext.json"))

    def test_probka_ma_kandydatow_ze_spodziewanymi_polami(self):
        self.assertEqual(len(self.places), 5)
        for p in self.places:
            self.assertTrue({"id", "name", "address", "types", "rating",
                             "review_count", "lat", "lng"} <= set(p))

    def test_displayname_jest_splaszczany(self):
        """API zwraca `displayName: {text, languageCode}` — dalej chcemy string."""
        self.assertEqual(self.places[0]["name"], "Alkyonides Boutique Hotel")

    def test_ocena_przeliczona_ze_skali_1_5_na_0_10(self):
        """Cały projekt żyje w skali 0-10. 4.2/5 to 8.4/10, nie 4.2."""
        self.assertEqual(self.places[0]["rating"], 8.4)
        self.assertEqual(self.places[0]["rating_raw"], 4.2)
        self.assertEqual(self.places[0]["review_count"], 812)

    def test_wspolrzedne_splaszczane(self):
        self.assertAlmostEqual(self.places[0]["lat"], 36.409312, places=5)
        self.assertAlmostEqual(self.places[0]["lng"], 28.118974, places=5)

    def test_skala_google_to_piatka(self):
        self.assertEqual(GOOGLE_BEST_RATING, 5)

    def test_pusta_odpowiedz_nie_wybucha(self):
        self.assertEqual(parse_places({}), [])
        self.assertEqual(parse_places({"places": []}), [])
        self.assertEqual(parse_places({"places": None}), [])

    def test_kandydat_bez_id_pomijany(self):
        payload = {"places": [{"displayName": {"text": "Bez id"}},
                              {"id": "a", "displayName": {"text": "Z id"}}]}
        self.assertEqual([p["id"] for p in parse_places(payload)], ["a"])

    def test_brak_oceny_daje_none_a_nie_zero(self):
        """Hotel bez ani jednej opinii to nie hotel z oceną 0.0."""
        payload = {"places": [{"id": "a", "displayName": {"text": "X"}}]}
        p = parse_places(payload)[0]
        self.assertIsNone(p["rating"])
        self.assertIsNone(p["review_count"])


class TestParsowanieOpinii(unittest.TestCase):
    def setUp(self):
        self.details = sample("google_places_details.json")

    def test_limit_pieciu_opinii_to_limit_api(self):
        """Google oddaje najwyżej 5 opinii i nie ma paginacji — to ograniczenie
        API, nie brak uprawnień i nie błąd."""
        self.assertEqual(MAX_REVIEWS, 5)
        self.assertEqual(len(self.details["reviews"]), 5)

    def test_bierze_tekst_przetlumaczony(self):
        r = parse_reviews(self.details)
        self.assertIn("Świetna lokalizacja", r[0])

    def test_originaltext_gdy_brak_text(self):
        r = parse_reviews(self.details)
        self.assertTrue(any("freundliches Personal" in x for x in r))

    def test_opinia_bez_tresci_pomijana(self):
        """Sama gwiazdka bez tekstu do niczego się nie nadaje."""
        r = parse_reviews(self.details)
        self.assertEqual(len(r), 4)
        self.assertTrue(all(x.strip() for x in r))

    def test_zlamania_wiersza_spłaszczane(self):
        self.assertNotIn("\n", " ".join(parse_reviews(self.details)))

    def test_wlasny_limit_dziala(self):
        self.assertEqual(len(parse_reviews(self.details, limit=2)), 2)

    def test_brak_opinii_nie_wybucha(self):
        self.assertEqual(parse_reviews({}), [])
        self.assertEqual(parse_reviews({"reviews": None}), [])


class TestParsowanieBledu(unittest.TestCase):
    def test_komunikat_google_trafia_do_bledu(self):
        """Najczęstszy realny błąd tego API to brak `X-Goog-FieldMask` —
        samo „HTTP 400" nie powiedziałoby użytkownikowi nic."""
        msg = parse_error(sample("google_places_error.json"), 400)
        self.assertIn("INVALID_ARGUMENT", msg)
        self.assertIn("field mask", msg)

    def test_bez_bloku_error_zostaje_kod(self):
        self.assertEqual(parse_error({}, 403), "HTTP 403")


# ------------------------------------------------------------ dopasowanie

class TestFiltrTypu(unittest.TestCase):
    """Sito, którego HolidayCheck nie miał: `types` odsiewa obiekty o nazwie
    hotelu, które hotelem nie są."""

    def test_hotel_przechodzi(self):
        self.assertTrue(is_lodging(["hotel", "point_of_interest", "establishment"]))
        self.assertTrue(is_lodging(["lodging"]))
        self.assertTrue(is_lodging(["resort_hotel", "establishment"]))

    def test_restauracja_odpada(self):
        self.assertFalse(is_lodging(["greek_restaurant", "restaurant", "food"]))

    def test_biuro_podrozy_odpada(self):
        self.assertFalse(is_lodging(["travel_agency", "point_of_interest"]))

    def test_brak_typow_nie_odsiewa(self):
        """Okrojony field mask nie ma prawa cicho wyzerować całego źródła."""
        self.assertTrue(is_lodging([]))
        self.assertTrue(is_lodging(None))


class TestZgodnoscKraju(unittest.TestCase):
    def test_polska_i_angielska_nazwa_kraju(self):
        self.assertTrue(country_matches("Grecja", "Kremasti 851 04, Grecja"))
        self.assertTrue(country_matches("Grecja", "Kremasti 851 04, Greece"))

    def test_inny_kraj_odrzucany(self):
        """Lekcja z HolidayCheck powtórzona 1:1: ateński `Ambrosia` dostawał
        `Hotel Ambrosia` w tureckim Bitez z podobieństwem nazwy 1.00."""
        self.assertFalse(country_matches("Grecja", "Bitez, 48400 Bodrum/Muğla, Turcja"))
        self.assertFalse(country_matches("Turcja", "Kremasti 851 04, Grecja"))

    def test_znaki_diakrytyczne_nie_psuja_dopasowania(self):
        self.assertTrue(country_matches("Turcja", "Bitez, Bodrum/Muğla, Türkiye"))
        self.assertTrue(country_matches("Czarnogóra", "Budva, Crna Gora"))

    def test_kraj_szukany_tylko_w_ogonie_adresu(self):
        """Ulica „Grecka" w Polsce nie czyni z hotelu greckiego hotelu —
        dlatego patrzymy na dwa ostatnie segmenty adresu, nie na cały ciąg."""
        self.assertFalse(country_matches("Grecja", "ul. Grecka 4, 61-001 Poznań, Polska"))

    def test_nieznany_kraj_porownywany_doslownie(self):
        self.assertTrue(country_matches("Islandia", "Reykjavik, Islandia"))
        self.assertFalse(country_matches("Islandia", "Reykjavik, Iceland"))


class TestZgodnoscMiejsca(unittest.TestCase):
    def test_miasto_w_adresie(self):
        self.assertTrue(place_matches("Kremasti", "Rodos", "Kremasti 851 04, Grecja"))

    def test_region_ratuje_gdy_miasta_brak(self):
        self.assertTrue(place_matches("", "Oludeniz", "Ölüdeniz, Fethiye, Turcja"))

    def test_sasiednie_miasto_nie_potwierdza(self):
        self.assertFalse(place_matches("Sliema", "", "Gzira, Malta"))

    def test_za_krotka_nazwa_ignorowana(self):
        self.assertFalse(place_matches("Os", "", "Oslo, Norwegia"))


class TestWyborHotelu(unittest.TestCase):
    def kandydaci(self):
        return parse_places(sample("google_places_searchtext.json"))

    def test_wybiera_wlasciwy_hotel(self):
        best, conf, status = pick_place(
            "Alkyonides (Kremasti)", "Grecja", "Kremasti", "Rodos", self.kandydaci())
        self.assertEqual(status, ST_OK)
        self.assertEqual(best["name"], "Alkyonides Boutique Hotel")
        self.assertEqual(best["rating"], 8.4)
        self.assertGreaterEqual(conf, 0.80)

    def test_restauracja_o_tej_samej_nazwie_odsiana(self):
        """Taverna „Alkyonides" w tej samej miejscowości ma nazwę identyczną
        (1.00) i wyższą ocenę — bez filtra `types` wygrałaby."""
        best, _, _ = pick_place(
            "Alkyonides (Kremasti)", "Grecja", "Kremasti", "Rodos", self.kandydaci())
        self.assertNotIn("restaurant", best["types"])

    def test_imiennik_z_innej_wyspy_nie_wygrywa(self):
        """`Alkyonides Hotel Apartments` w Stalidzie na Krecie ma po
        normalizacji nazwę IDENTYCZNĄ z naszą (bo `hotel` i `apartments` to
        słowa generyczne), a właściwy hotel tylko 0.88. Rozstrzyga miejscowość."""
        best, _, status = pick_place(
            "Alkyonides (Kremasti)", "Grecja", "Kremasti", "Rodos", self.kandydaci())
        self.assertEqual(status, ST_OK)
        self.assertNotIn("Stalida", best["address"])

    def test_imiennik_z_innego_miasta_nie_jest_rywalem(self):
        """Rywal liczy się tylko wtedy, gdy jest RÓWNIE dobrze ulokowany."""
        _, _, status = pick_place(
            "Alkyonides (Kremasti)", "Grecja", "Kremasti", "Rodos", self.kandydaci())
        self.assertEqual(status, ST_OK)

    def test_kraj_jest_warunkiem_koniecznym(self):
        """Sam turecki imiennik i nic więcej -> brak trafienia, nie trafienie."""
        tureckie = [p for p in self.kandydaci() if "Turcja" in p["address"]]
        best, _, status = pick_place(
            "Alkyonides (Kremasti)", "Grecja", "Kremasti", "Rodos", tureckie)
        self.assertIsNone(best)
        self.assertEqual(status, ST_NO_MATCH)

    def test_rodzina_hoteli_daje_ambiguous(self):
        """Trzy hotele „Karbel" w Ölüdeniz, a baza zna tylko „Karbel".
        Lepszy brak danych niż ocena obcego hotelu i fałszywa rozbieżność."""
        _, _, status = pick_place(
            "Karbel", "Turcja", "Oludeniz", "Wybrzeże Likijskie",
            parse_places(sample("google_places_rywale.json")))
        self.assertEqual(status, ST_AMBIGUOUS)

    def test_slaba_nazwa_daje_ambiguous(self):
        cands = [{"id": "x", "name": "Studios Veronica", "types": ["hotel"],
                  "address": "Moraitika 490 84, Grecja", "rating": 8.0,
                  "review_count": 30}]
        best, conf, status = pick_place(
            "Ionian View Studios", "Grecja", "Moraitika", "Korfu", cands)
        self.assertEqual(status, ST_AMBIGUOUS)
        self.assertIsNotNone(best)
        self.assertLess(conf, 0.80)

    def test_idealna_nazwa_bez_potwierdzenia_miasta_przechodzi(self):
        """Novotel Malta *Sliema* stoi administracyjnie w Gzirze."""
        cands = [{"id": "x", "name": "Novotel Malta", "types": ["hotel"],
                  "address": "Triq ix-Xatt, Gzira GZR 1021, Malta",
                  "rating": 8.2, "review_count": 900}]
        _, conf, status = pick_place("Novotel Malta", "Malta", "Sliema",
                                     "Wyspa Malta", cands)
        self.assertEqual(status, ST_OK)
        self.assertGreaterEqual(conf, 0.97)

    def test_brak_kandydatow(self):
        best, conf, status = pick_place("Cokolwiek", "Grecja", "", "", [])
        self.assertIsNone(best)
        self.assertEqual(status, ST_NO_MATCH)
        self.assertEqual(conf, 0.0)

    def test_premia_za_miejsce_nie_wchodzi_do_pewnosci(self):
        """Premia porządkuje kandydatów, ale zwracana pewność to nadal samo
        podobieństwo nazwy — inaczej `ambiguous` przestałoby działać."""
        cands = [{"id": "x", "name": "Zupelnie Inna Nazwa", "types": ["hotel"],
                  "address": "Kremasti 851 04, Grecja"}]
        _, conf, status = pick_place("Alkyonides", "Grecja", "Kremasti", "", cands)
        self.assertLess(conf, 0.80)
        self.assertEqual(status, ST_AMBIGUOUS)


class TestFrazaWyszukiwania(unittest.TestCase):
    def test_nazwa_miasto_kraj(self):
        self.assertEqual(_query("Alkyonides (Kremasti)", "Grecja", "Kremasti", "Rodos"),
                         "Alkyonides Kremasti Grecja")

    def test_nawiasy_zdejmowane(self):
        """Bez tego wychodzi „Alkyonides (Kremasti) Kremasti", czyli fraza,
        w której miasto waży dwa razy więcej niż nazwa hotelu."""
        self.assertNotIn("(", _query("Olympia (Pefkohori)", "Grecja", "Pefkohori", ""))

    def test_ogon_po_ex_wycinany(self):
        self.assertEqual(_query("Kirbiyik Resort (ex. Dinler)", "Turcja", "Mahmutlar", ""),
                         "Kirbiyik Resort Mahmutlar Turcja")

    def test_kraj_juz_w_nazwie_nie_dublowany(self):
        self.assertEqual(_query("Malta Marriott", "Malta", "", ""), "Malta Marriott")

    def test_region_gdy_brak_miasta(self):
        self.assertEqual(_query("Marmari Bay", "Grecja", "", "Evia"),
                         "Marmari Bay Evia Grecja")

    def test_pusta_nazwa_daje_pusta_fraze(self):
        self.assertEqual(_query("", "Grecja", "Kremasti", ""), "")


class TestKonwersjaNaOcene(unittest.TestCase):
    def test_ocena_i_link_do_map(self):
        p = parse_places(sample("google_places_searchtext.json"))[0]
        r = to_rating("35113", p, 0.88)
        self.assertEqual(r.source, SOURCE)
        self.assertEqual(r.hotel_id, "35113")
        self.assertEqual(r.rating, 8.4)
        self.assertEqual(r.review_count, 812)
        self.assertEqual(r.status, ST_OK)
        self.assertTrue(r.usable)
        self.assertIn("place_id:", r.url)

    def test_hotel_bez_opinii_to_no_rating(self):
        r = to_rating("1", {"id": "a", "name": "X", "rating": None}, 0.9)
        self.assertEqual(r.status, ST_NO_RATING)
        self.assertFalse(r.usable)


# ------------------------------------------------------- warstwa HTTP i klucz

class TestBrakKlucza(unittest.TestCase):
    """Bez klucza źródło ma degradować się CICHO: komunikat, nie wyjątek —
    i ma ruszyć samo w dniu, w którym klucz się pojawi."""

    def setUp(self):
        self.klient = GooglePlacesRatings(api_key="")

    def test_available_falsz(self):
        self.assertFalse(self.klient.available)

    def test_fetch_daje_no_key_bez_wyjatku(self):
        r = self.klient.fetch("1", "Alkyonides", "Grecja", "Kremasti")
        self.assertEqual(r.status, ST_NO_KEY)
        self.assertEqual(r.source, SOURCE)
        self.assertIn(API_KEY_NAME, r.error)
        self.assertFalse(r.usable)

    def test_bez_klucza_nie_ma_ruchu_sieciowego(self):
        def wybuchowy(request):
            raise AssertionError("bez klucza nie wolno dotknąć sieci")

        k = GooglePlacesRatings(api_key="", delay=0,
                                http=httpx.Client(transport=httpx.MockTransport(wybuchowy)))
        self.assertEqual(k.fetch("1", "X", "Grecja").status, ST_NO_KEY)
        self.assertEqual(k.search_text("X")[0], [])
        self.assertEqual(k.details("abc")[0], {})

    def test_no_key_nie_zostaje_w_cache(self):
        """Kluczowe dla wymagania „zadziała automatycznie, gdy klucz się
        pojawi": gdyby `no_key` był cache'owany jak trwały fakt, wgranie
        klucza nic by nie zmieniło bez ręcznego `--refresh`."""
        with tempfile.TemporaryDirectory() as d:
            store = ExternalRatingStore(Path(d) / "t.db")
            store.put(ExternalRating(hotel_id="1", source=SOURCE, status=ST_NO_KEY))
            self.assertIsNone(store.get("1", SOURCE))


class TestWarstwaHttp(unittest.TestCase):
    def test_wysyla_klucz_i_field_mask(self):
        """Field mask jest OBOWIĄZKOWY — bez niego API zwraca błąd, bo nie ma
        listy pól domyślnych."""
        zapamietane = {}

        def handler(request):
            zapamietane["url"] = str(request.url)
            zapamietane["headers"] = dict(request.headers)
            zapamietane["body"] = json.loads(request.content)
            return httpx.Response(200, json={"places": []})

        client(handler).search_text("Alkyonides Kremasti Grecja")
        self.assertEqual(zapamietane["url"], SEARCH_URL)
        self.assertEqual(zapamietane["headers"]["x-goog-api-key"], "TESTOWY")
        self.assertEqual(zapamietane["headers"]["x-goog-fieldmask"], SEARCH_FIELD_MASK)
        self.assertNotIn(" ", zapamietane["headers"]["x-goog-fieldmask"])

    def test_field_mask_zawiera_komplet_potrzebnych_pol(self):
        for pole in ("places.id", "places.displayName", "places.rating",
                     "places.userRatingCount", "places.formattedAddress",
                     "places.location", "places.types"):
            self.assertIn(pole, SEARCH_FIELD_MASK)

    def test_cialo_zapytania_wg_dokumentacji(self):
        zapamietane = {}

        def handler(request):
            zapamietane.update(json.loads(request.content))
            return httpx.Response(200, json={"places": []})

        client(handler).search_text("Alkyonides Kremasti Grecja", max_results=5)
        self.assertEqual(zapamietane["textQuery"], "Alkyonides Kremasti Grecja")
        self.assertEqual(zapamietane["maxResultCount"], 5)
        self.assertNotIn("locationBias", zapamietane)

    def test_location_bias_tylko_gdy_sa_wspolrzedne(self):
        zapamietane = {}

        def handler(request):
            zapamietane.update(json.loads(request.content))
            return httpx.Response(200, json={"places": []})

        client(handler).search_text("X", lat=36.4, lng=28.1, radius_m=15000)
        bias = zapamietane["locationBias"]["circle"]
        self.assertEqual(bias["center"], {"latitude": 36.4, "longitude": 28.1})
        self.assertEqual(bias["radius"], 15000)

    def test_blad_api_wraca_jako_komunikat_a_nie_wyjatek(self):
        def handler(request):
            return httpx.Response(400, json=sample("google_places_error.json"))

        places, blad = client(handler).search_text("X")
        self.assertEqual(places, [])
        self.assertIn("field mask", blad)

    def test_padnieta_siec_wraca_jako_blad(self):
        def handler(request):
            raise httpx.ConnectTimeout("boom")

        places, blad = client(handler).search_text("X")
        self.assertEqual(places, [])
        self.assertIn("sieć", blad)

    def test_details_uzywa_maski_z_reviews(self):
        zapamietane = {}

        def handler(request):
            zapamietane["url"] = str(request.url)
            zapamietane["mask"] = request.headers["x-goog-fieldmask"]
            return httpx.Response(200, json=sample("google_places_details.json"))

        dane, blad = client(handler).details("ChIJq6qq6koLnRQRxKZ0mQ2wPQ4")
        self.assertEqual(blad, "")
        self.assertTrue(zapamietane["url"].endswith("/places/ChIJq6qq6koLnRQRxKZ0mQ2wPQ4"))
        self.assertIn("reviews", zapamietane["mask"])
        self.assertEqual(zapamietane["mask"], DETAILS_FIELD_MASK)
        self.assertEqual(dane["userRatingCount"], 812)


class TestPelnaSciezka(unittest.TestCase):
    """`fetch` od nazwy hotelu do oceny w skali 0-10."""

    def szukaj(self, plik):
        def handler(request):
            if request.url.path.endswith("places:searchText"):
                return httpx.Response(200, json=sample(plik))
            return httpx.Response(200, json=sample("google_places_details.json"))
        return handler

    def test_trafienie_z_ocena(self):
        r = client(self.szukaj("google_places_searchtext.json")).fetch(
            "35113", "Alkyonides (Kremasti)", "Grecja", "Kremasti", "Rodos")
        self.assertEqual(r.status, ST_OK)
        self.assertEqual(r.source, SOURCE)
        self.assertEqual(r.hotel_id, "35113")
        self.assertEqual(r.rating, 8.4)
        self.assertEqual(r.review_count, 812)
        self.assertEqual(r.matched_name, "Alkyonides Boutique Hotel")
        self.assertTrue(r.fetched_at)

    def test_opinie_dociagane_tylko_na_zadanie(self):
        """Domyślnie to JEDNO żądanie na hotel — `reviews` przenosi zapytanie
        do najdroższego SKU (Enterprise + Atmosphere)."""
        licznik = {"n": 0}

        def handler(request):
            licznik["n"] += 1
            if request.url.path.endswith("places:searchText"):
                return httpx.Response(200, json=sample("google_places_searchtext.json"))
            return httpx.Response(200, json=sample("google_places_details.json"))

        k = client(handler)
        r = k.fetch("1", "Alkyonides (Kremasti)", "Grecja", "Kremasti")
        self.assertEqual(licznik["n"], 1)
        self.assertEqual(r.reviews, [])

        r = k.fetch("1", "Alkyonides (Kremasti)", "Grecja", "Kremasti", with_reviews=True)
        self.assertEqual(licznik["n"], 3)
        self.assertTrue(r.reviews)
        self.assertLessEqual(len(r.reviews), MAX_REVIEWS)

    def test_rywale_daja_ambiguous_bez_oceny(self):
        r = client(self.szukaj("google_places_rywale.json")).fetch(
            "2", "Karbel", "Turcja", "Oludeniz", "Wybrzeże Likijskie")
        self.assertEqual(r.status, ST_AMBIGUOUS)
        self.assertIsNone(r.rating)
        self.assertFalse(r.usable)

    def test_brak_wynikow_to_no_match(self):
        def handler(request):
            return httpx.Response(200, json={"places": []})

        r = client(handler).fetch("3", "Nieistniejacy", "Grecja", "Gdzies")
        self.assertEqual(r.status, ST_NO_MATCH)

    def test_awaria_daje_error_a_nie_no_match(self):
        """Awaria źródła NIE może zapisać się jako trwałe „nie ma takiego
        hotelu" — jedno padnięcie wyłączyłoby Google dla całego rankingu."""
        def handler(request):
            raise httpx.ConnectTimeout("boom")

        r = client(handler).fetch("4", "Alkyonides", "Grecja", "Kremasti")
        self.assertEqual(r.status, ST_ERROR)
        self.assertIn("sieć", r.error)

    def test_pusta_nazwa_hotelu(self):
        def handler(request):
            raise AssertionError("pusta nazwa nie ma prawa dotknąć sieci")

        r = client(handler).fetch("5", "", "Grecja")
        self.assertEqual(r.status, ST_NO_MATCH)


class TestCacheDwochZrodel(unittest.TestCase):
    """Klucz główny to `(hotel_id, source)` — źródła nie mogą się nadpisywać."""

    class Atrapa:
        def __init__(self, name, wynik):
            self.name = name
            self.wynik = wynik
            self.wywolania = 0

        def fetch(self, hotel_id, hotel_name, country="", city="", region=""):
            self.wywolania += 1
            out = ExternalRating(**dict(self.wynik.__dict__))
            out.hotel_id = str(hotel_id)
            return out

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ExternalRatingStore(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_google_nie_nadpisuje_holidaycheck(self):
        hc = self.Atrapa("holidaycheck",
                         ExternalRating(source="holidaycheck", rating=6.2,
                                        review_count=4, status=ST_OK))
        gg = self.Atrapa(SOURCE, ExternalRating(source=SOURCE, rating=8.4,
                                                review_count=812, status=ST_OK))
        get_or_fetch(self.store, hc, "1", "Alkyonides", "Grecja", "Kremasti")
        get_or_fetch(self.store, gg, "1", "Alkyonides", "Grecja", "Kremasti")
        self.assertEqual(self.store.get("1", "holidaycheck").rating, 6.2)
        self.assertEqual(self.store.get("1", SOURCE).rating, 8.4)
        self.assertEqual(self.store.count("holidaycheck"), 1)
        self.assertEqual(self.store.count(SOURCE), 1)

    def test_cache_dziala_per_zrodlo(self):
        gg = self.Atrapa(SOURCE, ExternalRating(source=SOURCE, rating=8.4,
                                                review_count=812, status=ST_OK))
        get_or_fetch(self.store, gg, "1", "Alkyonides", "Grecja")
        get_or_fetch(self.store, gg, "1", "Alkyonides", "Grecja")
        self.assertEqual(gg.wywolania, 1)

    def test_brak_klucza_ponawiany_przy_kolejnym_przebiegu(self):
        gg = self.Atrapa(SOURCE, ExternalRating(source=SOURCE, status=ST_NO_KEY))
        get_or_fetch(self.store, gg, "1", "Alkyonides", "Grecja")
        get_or_fetch(self.store, gg, "1", "Alkyonides", "Grecja")
        self.assertEqual(gg.wywolania, 2)


# ------------------------------------------------------------- kalibracja

class TestKalibracja(unittest.TestCase):
    """Systematyka liczona z DANYCH, nie wpisana na sztywno."""

    @staticmethod
    def para(local, source, rating, count=100):
        return local, ExternalRating(source=source, rating=rating,
                                     review_count=count, status=ST_OK)

    def test_mediana_per_zrodlo_osobno(self):
        """HolidayCheck ocenia surowiej (+ po naszej stronie), Google
        łagodniej (−). Zmieszanie ich w jedną liczbę zatarłoby oba efekty."""
        cal = calibrate([
            self.para(8.6, "holidaycheck", 8.0),
            self.para(8.4, "holidaycheck", 7.8),
            self.para(9.0, "holidaycheck", 8.4),
            self.para(8.6, "google", 9.2),
            self.para(8.4, "google", 9.0),
            self.para(9.0, "google", 9.6),
        ])
        self.assertEqual(cal["holidaycheck"].median, 0.6)
        self.assertEqual(cal["google"].median, -0.6)
        self.assertTrue(cal["holidaycheck"].enough)
        self.assertTrue(cal["google"].enough)

    def test_za_mala_probka_nie_jest_stosowana(self):
        cal = calibrate([self.para(8.6, "google", 9.2), self.para(8.4, "google", 9.0)])
        self.assertEqual(cal["google"].n, 2)
        self.assertFalse(cal["google"].enough)
        self.assertEqual(offsets_map(cal), {})

    def test_prog_probki(self):
        cal = calibrate([self.para(8.6, "google", 9.2)] * MIN_CALIBRATION_PAIRS)
        self.assertTrue(cal["google"].enough)
        self.assertIn("google", offsets_map(cal))

    def test_mediana_odporna_na_pojedynczy_skrajny_przypadek(self):
        """Jeden hotel z rozjazdem 4 pkt — a takie są, to cała wartość tego
        narzędzia — nie ma prawa skalibrować systemu tak, by przestał go widzieć."""
        cal = calibrate([
            self.para(8.6, "holidaycheck", 8.4),
            self.para(8.4, "holidaycheck", 8.2),
            self.para(8.6, "holidaycheck", 4.6),
        ])
        self.assertEqual(cal["holidaycheck"].median, 0.2)

    def test_korekta_przycinana(self):
        """Korekta większa niż próg rozbieżności zamiotłaby problem pod dywan."""
        cal = calibrate([self.para(9.0, "dziwne", 3.0)] * 5)
        self.assertEqual(cal["dziwne"].median, 6.0)
        self.assertEqual(cal["dziwne"].applied, 1.5)

    def test_ambiguous_i_no_rating_nie_wchodza_do_kalibracji(self):
        cal = calibrate([
            (8.6, ExternalRating(source="google", rating=9.2, status=ST_AMBIGUOUS)),
            (8.6, ExternalRating(source="google", rating=None, status=ST_NO_RATING)),
            (None, ExternalRating(source="google", rating=9.2, status=ST_OK)),
        ])
        self.assertEqual(cal, {})

    def test_etykieta_mowi_o_kierunku(self):
        cal = calibrate([self.para(8.0, "google", 9.0)] * 3)
        self.assertIn("łagodniej", cal["google"].label)
        self.assertIn("na 3 parach", cal["google"].label)


# --------------------------------------------------- agregacja wielu źródeł

class TestAgregacjaWieluZrodel(unittest.TestCase):
    @staticmethod
    def zew(source, rating, count, status=ST_OK):
        return ExternalRating(source=source, rating=rating, review_count=count,
                              status=status)

    def test_zgodne_zrodla_demaskuja_ocene_lokalna(self):
        """Przypadek z zadania: 10.0 z JEDNEJ opinii na wakacje.pl kontra
        4.2/5 z 800 opinii Google. Werdykt ma być jednoznaczny."""
        r = reliability_multi(10.0, 1, [
            self.zew("holidaycheck", 7.9, 46),
            self.zew("google", 8.4, 812),
        ])
        self.assertTrue(r.divergent)
        self.assertTrue(r.agreement)
        self.assertTrue(r.thin)
        self.assertEqual(r.level, "niska")
        self.assertIn("odstaje", r.reason)
        self.assertEqual(set(r.sources), {"holidaycheck", "google"})

    def test_zrodla_kloca_sie_ze_soba_to_nie_wiadomo(self):
        """Dwa niezależne serwisy, które się rozjeżdżają, nie dają średniej —
        dają brak rozstrzygnięcia."""
        r = reliability_multi(8.5, 100, [
            self.zew("holidaycheck", 5.5, 300),
            self.zew("google", 9.0, 800),
        ])
        self.assertTrue(r.divergent)
        self.assertFalse(r.agreement)
        self.assertEqual(r.level, "niska")
        self.assertIn("nie zgadzają się ze sobą", r.reason)

    def test_dwa_zgodne_zrodla_podnosza_pewnosc(self):
        """Dwa niezależne potwierdzenia są warte więcej niż jedno — próg
        „wysokiej" spada z 30 opinii do 20."""
        r = reliability_multi(8.6, 4, [
            self.zew("holidaycheck", 8.4, 8),
            self.zew("google", 8.7, 9),
        ])
        self.assertEqual(r.level, "wysoka")
        self.assertTrue(r.agreement)
        self.assertFalse(r.divergent)

    def test_jedno_zrodlo_przy_tej_samej_liczbie_opinii_daje_mniej(self):
        """Kontrola do testu wyżej: 21 opinii z JEDNEGO źródła to wciąż
        tylko „średnia" — po to jest drugie źródło."""
        r = reliability_multi(8.6, 4, [self.zew("google", 8.5, 17)])
        self.assertEqual(r.level, "średnia")
        self.assertFalse(r.agreement)

    def test_srednia_wazona_liczba_opinii(self):
        """Konsensus dwóch zgodnych źródeł jest ważony liczbą opinii: Google
        z 800 opiniami przeważa nad HolidayCheck z czterema. Bez ważenia
        wyszłoby 7.85 i różnica 0.55; z ważeniem — 8.49 i różnica 0.09."""
        r = reliability_multi(8.4, 50, [
            self.zew("holidaycheck", 7.2, 4),
            self.zew("google", 8.5, 800),
        ])
        self.assertFalse(r.divergent)
        self.assertLess(r.diff, 0.2)

    def test_cienkie_zrodlo_wciaz_moze_zawetowac_werdykt(self):
        """Świadomie zachowana konserwatywność: źródło z 4 opiniami, które
        rozjeżdża się o 2.5 pkt z 800-opiniowym Google, daje „nie wiadomo",
        a nie przegłosowanie. Zgodnie z zasadą całego modułu: lepszy brak
        danych niż liczba, która wygląda na pewną. Patrz „Znane ograniczenia"
        w docs/opinie-zewnetrzne.md."""
        r = reliability_multi(8.4, 50, [
            self.zew("holidaycheck", 6.0, 4),
            self.zew("google", 8.5, 800),
        ])
        self.assertTrue(r.divergent)
        self.assertFalse(r.agreement)
        self.assertIn("nie zgadzają się ze sobą", r.reason)

    def test_kalibracja_gasi_falszywa_rozbieznosc(self):
        """1.6 pkt różnicy przy źródle, które systematycznie zaniża o 1.5,
        to hotel w normie — a bez kalibracji byłaby zapalona flaga."""
        surowo = reliability_multi(8.6, 40, [self.zew("holidaycheck", 7.0, 60)])
        self.assertTrue(surowo.divergent)

        skalibrowane = reliability_multi(8.6, 40, [self.zew("holidaycheck", 7.0, 60)],
                                         {"holidaycheck": 1.5})
        self.assertFalse(skalibrowane.divergent)
        self.assertEqual(skalibrowane.diff, 1.6)       # surowa różnica zostaje widoczna
        self.assertEqual(skalibrowane.diff_adj, 0.1)   # reszta ponad systematykę

    def test_kalibracja_nie_ukrywa_prawdziwej_rozbieznosci(self):
        r = reliability_multi(8.6, 40, [self.zew("holidaycheck", 5.6, 36)],
                              {"holidaycheck": 0.6})
        self.assertTrue(r.divergent)
        self.assertEqual(r.diff, 3.0)
        self.assertEqual(r.diff_adj, 2.4)
        self.assertIn("ponad systematykę", r.reason)

    def test_kalibracja_dziala_per_zrodlo(self):
        """HolidayCheck korygujemy w górę, Google w dół — jednym offsetem
        dla obu wyszedłby bezsens."""
        r = reliability_multi(8.6, 10, [
            self.zew("holidaycheck", 8.0, 50),
            self.zew("google", 9.2, 500),
        ], {"holidaycheck": 0.6, "google": -0.6})
        self.assertFalse(r.divergent)
        self.assertEqual(r.level, "wysoka")

    def test_niepewne_dopasowanie_nie_wchodzi_do_werdyktu(self):
        r = reliability_multi(10.0, 1, [
            self.zew("holidaycheck", 6.2, 4, status=ST_AMBIGUOUS),
            self.zew("google", 8.4, 812, status=ST_AMBIGUOUS),
        ])
        self.assertEqual(r.sources, ())
        self.assertIsNone(r.diff)
        self.assertIn("niepewne", r.reason)

    def test_brak_klucza_google_nie_psuje_holidaycheck(self):
        """Wymaganie wprost z zadania: bez klucza reszta ma działać normalnie."""
        r = reliability_multi(9.2, 1, [
            self.zew("holidaycheck", 9.4, 46),
            ExternalRating(source=SOURCE, status=ST_NO_KEY),
        ])
        self.assertEqual(r.level, "wysoka")
        self.assertEqual(r.sources, ("holidaycheck",))

    def test_puste_wejscie_nie_wybucha(self):
        r = reliability_multi(8.0, 5, [])
        self.assertEqual(r.level, "niska")
        self.assertEqual(r.sources, ())
        self.assertIsNone(r.diff)

    def test_none_na_liscie_jest_ignorowane(self):
        r = reliability_multi(8.0, 5, [None, self.zew("google", 8.1, 200)])
        self.assertEqual(r.sources, ("google",))


class TestSchematCacheu(unittest.TestCase):
    def test_kolumny_bez_zmian(self):
        """Google nie wymagał migracji — `(hotel_id, source)` był w schemacie
        od początku."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            ExternalRatingStore(path)
            db = sqlite3.connect(path)
            kols = {r[1] for r in db.execute("PRAGMA table_info(hotel_external_rating)")}
            self.assertEqual(kols, {"hotel_id", "source", "matched_name", "rating_0_10",
                                    "review_count", "url", "confidence", "status",
                                    "fetched_at"})


if __name__ == "__main__":
    unittest.main()

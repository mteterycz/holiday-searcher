"""Testy weryfikacji ceny końcowej (verify.py) — BEZ SIECI.

Zapytania HTTP idą przez httpx.MockTransport z odpowiedziami skopiowanymi
z realnego rekonesansu (docs/weryfikacja-ceny.md), baza to plik tymczasowy.

Uruchomienie: python3 -m unittest tests.test_verify -v
(bez PYTHONPATH=src — ten plik sam dokłada src/ do sys.path).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from holiday_searcher import verify  # noqa: E402


# ---------------------------------------------------------------- atrapy API

# Skrót realnej odpowiedzi GET /v2/api/getInitOfferData/1091524/?…
INIT_OK = {
    "success": True, "type": "info", "msg": "getInitOfferData",
    "data": {
        "offerId": 1091524, "codeWak": 1091524, "hotelId": 31638,
        "serviceType": "BB", "serviceTypeId": 3, "tourId": 11194,
        "tourOpCode": "ONHO", "tourOperatorName": "Onholidays",
        "transportId": 1, "currentDuration": 5, "nights": 5,
        "departure": "WAW", "departurePlace": "Warszawa - Chopin",
        "departureDate": "2026-09-23", "returnDate": "2026-09-28",
        "price": 2047, "cruiseId": 0, "roundTripId": 0,
    },
}

# Skrót realnej odpowiedzi POST /v2/api/getCalculatorOfferVariants/1091524
# UWAGA: totalPrice jest ZA CAŁY POKÓJ (2 osoby) — 4094/2 = 2047 = cena listingowa.
CALC_OK = {
    "success": True, "type": "info",
    "data": {
        "offers": [
            {
                "id": "pi-UZ6", "roomDesc": "Dbl sea view standard sea view",
                "tourOp": "ONHO", "serviceId": 3, "serviceDesc": "Śniadania (BB)",
                "transportId": 1, "duration": 5, "departureCode": "WAW",
                "basePrice": 4094, "totalPrice": 4094, "priceCurrency": "PLN",
                "roomDescAdditional": ["Śniadania", "Widok na morze"],
                "isLuggageIncluded": None, "objectId": 31638,
            },
            {
                "id": "p8SXaQ", "roomDesc": "Superior/deluxe room superior sea view",
                "tourOp": "ONHO", "serviceId": 3, "serviceDesc": "Śniadania (BB)",
                "transportId": 1, "duration": 5, "departureCode": "WAW",
                "basePrice": 4498, "totalPrice": 4498, "priceCurrency": "PLN",
                "roomDescAdditional": ["Śniadania", "Superior", "De lux"],
                "isLuggageIncluded": False, "objectId": 31638,
            },
        ],
        "sectionHeading": {}, "showMoreRoomsButton": {"display": False},
    },
}

# Kalkulator z pustą listą — realny kształt dla terminu bez wolnych pokoi.
CALC_EMPTY = {"success": True, "type": "info", "data": {"offers": []}}

# Realny kształt dla nieistniejącego offerId (HTTP 200, ale success:false).
INIT_UNKNOWN = {"success": False, "type": "error", "msg": "getInitOfferData",
                "data": None, "errors": "⛔️ undefined"}

OFFER_ROW = {
    "key": "abc123",
    "hotel_name": "Elios",
    "departure_date": "2026-09-23",
    "nights": 5,
    "departure_code": "WAW",
    "url": "https://www.wakacje.pl/oferty/elios-1091524.html?od-2026-09-23,do-2026-09-28",
    "price": 2047,
}


def make_verifier(routes, **kwargs) -> verify.PriceVerifier:
    """`routes` mapuje fragment ścieżki -> (status, body) albo wyjątek/callable.
    delay=0, żeby testy nie spały."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for needle, resp in routes.items():
            if needle in request.url.path:
                if callable(resp):
                    return resp(request, len([c for c in calls
                                              if needle in c.url.path]))
                status, body = resp
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "Not found"})

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="https://www.wakacje.pl")
    v = verify.PriceVerifier(delay=0.0, http=client, **kwargs)
    v.calls = calls  # type: ignore[attr-defined]
    return v


def temp_db() -> sqlite3.Connection:
    path = Path(tempfile.mkdtemp()) / "test.db"
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


# ------------------------------------------------------------ czyste funkcje


class TestOfferId(unittest.TestCase):
    def test_z_deep_linku_oferty(self):
        self.assertEqual(verify.offer_id_from_url(OFFER_ROW["url"]), "1091524")

    def test_link_do_hotelu_nie_ma_offerId(self):
        # /hotele/ to strona hotelu, nie konkretna oferta — nie ma czego liczyć
        self.assertEqual(
            verify.offer_id_from_url("https://www.wakacje.pl/hotele/wlochy/elios-31638.html"),
            "",
        )

    def test_pusty_url(self):
        self.assertEqual(verify.offer_id_from_url(""), "")


class TestParseVariants(unittest.TestCase):
    def test_totalPrice_dzielony_przez_liczbe_osob(self):
        """Sedno całego modułu: 4094 zł za pokój dla 2 osób to 2047 zł/os."""
        vs = verify.parse_variants(CALC_OK["data"], adults=2)
        self.assertEqual(len(vs), 2)
        self.assertEqual(vs[0].total_price, 4094)
        self.assertEqual(vs[0].price_pp, 2047)
        self.assertEqual(vs[1].price_pp, 2249)

    def test_sortowanie_rosnace_po_cenie(self):
        body = {"offers": [
            {"roomDesc": "drogi", "totalPrice": 9000},
            {"roomDesc": "tani", "totalPrice": 4000},
        ]}
        vs = verify.parse_variants(body, adults=2)
        self.assertEqual([v.room_desc for v in vs], ["tani", "drogi"])

    def test_trzy_osoby_dziela_przez_trzy(self):
        vs = verify.parse_variants({"offers": [{"totalPrice": 5830}]}, adults=3)
        self.assertEqual(vs[0].price_pp, 1943)

    def test_zero_osob_nie_wywala_dzieleniem(self):
        vs = verify.parse_variants({"offers": [{"totalPrice": 4094}]}, adults=0)
        self.assertEqual(vs[0].price_pp, 4094)

    def test_fallback_na_basePrice(self):
        vs = verify.parse_variants({"offers": [{"basePrice": 4000}]}, adults=2)
        self.assertEqual(vs[0].price_pp, 2000)

    def test_wariant_bez_ceny_jest_pomijany(self):
        vs = verify.parse_variants(
            {"offers": [{"roomDesc": "bez ceny"}, {"totalPrice": 4094}]}, adults=2)
        self.assertEqual(len(vs), 1)

    def test_pusta_odpowiedz(self):
        self.assertEqual(verify.parse_variants({}, adults=2), [])
        self.assertEqual(verify.parse_variants({"offers": None}, adults=2), [])

    def test_pola_dodatkowe(self):
        vs = verify.parse_variants(CALC_OK["data"], adults=2)
        self.assertIn("Widok na morze", vs[0].features)
        self.assertIsNone(vs[0].luggage_included)
        self.assertIs(vs[1].luggage_included, False)


class TestDiffPercent(unittest.TestCase):
    def test_zgodna_cena_to_zero(self):
        self.assertEqual(verify.diff_percent(2047, 2047), 0.0)

    def test_zawyzenie(self):
        self.assertAlmostEqual(verify.diff_percent(2000, 2200), 10.0)

    def test_obnizka_jest_ujemna(self):
        self.assertAlmostEqual(verify.diff_percent(2000, 1800), -10.0)

    def test_brak_danych_daje_none(self):
        self.assertIsNone(verify.diff_percent(None, 2000))
        self.assertIsNone(verify.diff_percent(2000, None))

    def test_zero_w_mianowniku_to_brak_danych_a_nie_nieskonczonosc(self):
        self.assertIsNone(verify.diff_percent(0, 2000))


class TestPayload(unittest.TestCase):
    def test_wartosci_z_api_maja_pierwszenstwo_nad_baza(self):
        p = verify.build_calculator_payload(
            INIT_OK["data"], adults=2, departure_id=278,
            nights=99, departure_date="1999-01-01", departure_code="XXX")
        self.assertEqual(p["duration"], 5)
        self.assertEqual(p["departureDate"], "2026-09-23")
        self.assertEqual(p["departureCityCode"], "WAW")
        self.assertEqual(p["tourOp"], "ONHO")
        self.assertEqual(p["hotelId"], 31638)
        self.assertEqual(p["serviceId"], 3)
        self.assertEqual(p["adults"], 2)
        # cruiseId/roundTripId: 0 z API musi zejść do None, tak jak w bundlu
        self.assertIsNone(p["cruiseId"])
        self.assertIsNone(p["roundTripId"])

    def test_baza_jako_zapasowe_zrodlo(self):
        p = verify.build_calculator_payload(
            {}, adults=2, departure_id=2696, nights=7,
            departure_date="2026-10-01", departure_code="KRK")
        self.assertEqual(p["duration"], 7)
        self.assertEqual(p["departureDate"], "2026-10-01")
        self.assertEqual(p["departureCityCode"], "KRK")
        self.assertEqual(p["departureCityId"], 2696)
        self.assertEqual(p["transportId"], 1)


class TestVerdict(unittest.TestCase):
    def _v(self, listing, final, api=None):
        v = verify.Verification(offer_key="k", listing_price=listing,
                                api_listing_price=api)
        v.final_price = final
        v.variants = [verify.RoomVariant(total_price=final * 2, price_pp=final)]
        return v

    def test_zgodna(self):
        self.assertEqual(self._v(2047, 2047).verdict, "zgodna")

    def test_tolerancja_2_procent(self):
        self.assertEqual(self._v(2000, 2040).verdict, "zgodna")     # +2.0%
        self.assertEqual(self._v(2000, 2100).verdict, "odchylenie")  # +5.0%

    def test_zawyzona_powyzej_10_procent(self):
        self.assertEqual(self._v(2000, 2400).verdict, "zawyzona")

    def test_nieaktualny_snapshot_to_nie_klamstwo_listingu(self):
        """Baza mówi 2047, ale świeży listing i kalkulator zgodnie mówią 2249 —
        to zmiana ceny, a nie cena-wabik."""
        v = self._v(2047, 2249, api=2249)
        self.assertEqual(v.verdict, "nieaktualna")
        self.assertTrue(v.stale_snapshot)
        self.assertAlmostEqual(v.diff_vs_api_pct, 0.0)

    def test_listing_klamie_gdy_swieza_cena_od_tez_odstaje(self):
        v = self._v(2000, 2500, api=2000)
        self.assertEqual(v.verdict, "zawyzona")
        self.assertFalse(v.stale_snapshot)

    def test_bez_ceny_koncowej_werdykt_nieznany(self):
        v = verify.Verification(offer_key="k", listing_price=2000,
                                error="termin wyprzedany")
        self.assertEqual(v.verdict, "nieznana")
        self.assertIsNone(v.diff_pct)
        self.assertIsNone(v.diff_pln)
        self.assertFalse(v.ok)

    def test_diff_pln(self):
        self.assertEqual(self._v(2000, 2200).diff_pln, 200)


# --------------------------------------------------------- pełna weryfikacja


class TestVerify(unittest.TestCase):
    def test_sciezka_szczesliwa(self):
        v = make_verifier({"getInitOfferData": (200, INIT_OK),
                           "getCalculatorOfferVariants": (200, CALC_OK)}).verify(
            OFFER_ROW, adults=2)
        self.assertTrue(v.ok)
        self.assertIsNone(v.error)
        self.assertEqual(v.offer_id, "1091524")
        self.assertEqual(v.listing_price, 2047)
        self.assertEqual(v.api_listing_price, 2047)
        self.assertEqual(v.final_price, 2047)
        self.assertEqual(v.max_price, 2249)
        self.assertEqual(v.diff_pct, 0.0)
        self.assertEqual(v.verdict, "zgodna")
        self.assertEqual(len(v.variants), 2)

    def test_payload_kalkulatora_trafia_na_wlasciwy_adres(self):
        ver = make_verifier({"getInitOfferData": (200, INIT_OK),
                             "getCalculatorOfferVariants": (200, CALC_OK)})
        ver.verify(OFFER_ROW, adults=2)
        calc = [c for c in ver.calls if "getCalculatorOfferVariants" in c.url.path][0]
        # offerId MUSI być w ścieżce — bez niego endpoint zwraca 404 (faza 0)
        self.assertTrue(calc.url.path.endswith("/getCalculatorOfferVariants/1091524"))
        self.assertEqual(calc.method, "POST")
        body = json.loads(calc.content)
        self.assertEqual(body["adults"], 2)
        self.assertEqual(body["tourOp"], "ONHO")
        # ciało jest PŁASKIE, nie zagnieżdżone w {query: …}
        self.assertNotIn("query", body)

    def test_liczba_doroslych_wchodzi_do_zapytania_i_do_dzielenia(self):
        ver = make_verifier({"getInitOfferData": (200, INIT_OK),
                             "getCalculatorOfferVariants": (200, CALC_OK)})
        v = ver.verify(OFFER_ROW, adults=3)
        body = json.loads([c for c in ver.calls
                           if "Calculator" in c.url.path][0].content)
        self.assertEqual(body["adults"], 3)
        self.assertEqual(v.final_price, round(4094 / 3))

    # ---------- degradacja: żaden z tych przypadków nie może rzucić ----------

    def test_brak_offerId_w_url(self):
        ver = make_verifier({})
        v = ver.verify(dict(OFFER_ROW, url="https://www.wakacje.pl/hotele/x-1.html"))
        self.assertFalse(v.ok)
        self.assertIn("offerId", v.error)
        self.assertEqual(ver.calls, [])   # nawet nie ruszamy sieci

    def test_oferta_nieznana_serwisowi(self):
        v = make_verifier({"getInitOfferData": (200, INIT_UNKNOWN)}).verify(OFFER_ROW)
        self.assertFalse(v.ok)
        self.assertIn("getInitOfferData", v.error)
        self.assertIn("nieznana serwisowi", v.error)
        self.assertIsNone(v.final_price)

    def test_brak_wariantow_to_wyprzedany_termin_a_nie_wyjatek(self):
        v = make_verifier({"getInitOfferData": (200, INIT_OK),
                           "getCalculatorOfferVariants": (200, CALC_EMPTY)}).verify(
            OFFER_ROW)
        self.assertFalse(v.ok)
        self.assertIn("wyprzedany", v.error)
        # cena listingowa z API mimo wszystko została odczytana
        self.assertEqual(v.api_listing_price, 2047)

    def test_blad_http_konczy_sie_wpisem_a_nie_wyjatkiem(self):
        v = make_verifier({"getInitOfferData": (500, {"boom": True})}).verify(OFFER_ROW)
        self.assertFalse(v.ok)
        self.assertIn("getInitOfferData", v.error)

    def test_zerwane_polaczenie_konczy_sie_wpisem(self):
        def boom(request, n):
            raise httpx.ConnectError("zerwane połączenie")
        v = make_verifier({"getInitOfferData": boom}).verify(OFFER_ROW)
        self.assertFalse(v.ok)
        self.assertIn("sieć", v.error)

    def test_retry_2x_na_bledzie_sieci_a_potem_sukces(self):
        def flaky(request, n):
            if n <= 2:
                raise httpx.ConnectError("timeout")
            return httpx.Response(200, json=INIT_OK)
        ver = make_verifier({"getInitOfferData": flaky,
                             "getCalculatorOfferVariants": (200, CALC_OK)})
        v = ver.verify(OFFER_ROW)
        self.assertTrue(v.ok)
        init_calls = [c for c in ver.calls if "getInitOfferData" in c.url.path]
        self.assertEqual(len(init_calls), 3)   # 1 próba + 2 retry

    def test_retry_ma_limit(self):
        def always_boom(request, n):
            raise httpx.ConnectError("timeout")
        ver = make_verifier({"getInitOfferData": always_boom})
        v = ver.verify(OFFER_ROW)
        self.assertFalse(v.ok)
        self.assertEqual(len(ver.calls), 3)

    def test_logiczny_blad_nie_jest_ponawiany(self):
        """success:false to trwała odpowiedź serwisu — dobijanie go nic nie da."""
        ver = make_verifier({"getInitOfferData": (200, INIT_UNKNOWN)})
        ver.verify(OFFER_ROW)
        self.assertEqual(len(ver.calls), 1)


# ------------------------------------------------------------------- zapis


class TestStorage(unittest.TestCase):
    def test_schemat_jest_idempotentny(self):
        db = temp_db()
        verify.ensure_schema(db)
        verify.ensure_schema(db)       # drugi raz nie może wywalić
        cols = [r[1] for r in db.execute("PRAGMA table_info(price_verification)")]
        for c in ("offer_key", "checked_at", "listing_price",
                  "final_price", "diff_pct", "details_json"):
            self.assertIn(c, cols)

    def test_zapis_udanej_weryfikacji(self):
        db = temp_db()
        v = make_verifier({"getInitOfferData": (200, INIT_OK),
                           "getCalculatorOfferVariants": (200, CALC_OK)}).verify(
            OFFER_ROW, adults=2)
        verify.save_verification(db, v)

        row = db.execute("SELECT * FROM price_verification").fetchone()
        self.assertEqual(row["offer_key"], "abc123")
        self.assertEqual(row["listing_price"], 2047)
        self.assertEqual(row["final_price"], 2047)
        self.assertEqual(row["diff_pct"], 0.0)

        details = json.loads(row["details_json"])
        self.assertEqual(details["verdict"], "zgodna")
        self.assertEqual(details["offer_id"], "1091524")
        self.assertEqual(len(details["variants"]), 2)
        self.assertEqual(details["variants"][0]["total_price"], 4094)
        self.assertEqual(details["max_price_pp"], 2249)

    def test_zapis_nieudanej_weryfikacji_zachowuje_powod(self):
        db = temp_db()
        v = make_verifier({"getInitOfferData": (200, INIT_OK),
                           "getCalculatorOfferVariants": (200, CALC_EMPTY)}).verify(
            OFFER_ROW)
        verify.save_verification(db, v)
        row = db.execute("SELECT * FROM price_verification").fetchone()
        self.assertIsNone(row["final_price"])
        self.assertIsNone(row["diff_pct"])
        details = json.loads(row["details_json"])
        self.assertIn("wyprzedany", details["error"])
        self.assertEqual(details["verdict"], "nieznana")

    def test_zapis_jest_append_only(self):
        """Kolejna weryfikacja tej samej oferty dokłada wiersz, nie nadpisuje —
        tak samo jak price_snapshot, żeby dało się prześledzić historię."""
        db = temp_db()
        ver = make_verifier({"getInitOfferData": (200, INIT_OK),
                             "getCalculatorOfferVariants": (200, CALC_OK)})
        verify.save_verification(db, ver.verify(OFFER_ROW))
        verify.save_verification(db, ver.verify(OFFER_ROW))
        n = db.execute("SELECT COUNT(*) FROM price_verification").fetchone()[0]
        self.assertEqual(n, 2)


class TestOffersToVerify(unittest.TestCase):
    def _seed(self):
        db = temp_db()
        db.executescript("""
            CREATE TABLE offer (key TEXT PRIMARY KEY, provider TEXT, hotel_name TEXT,
                region TEXT, city TEXT, departure_date TEXT, nights INTEGER,
                board TEXT, departure_code TEXT, url TEXT);
            CREATE TABLE price_snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_key TEXT, ts TEXT, price INTEGER, price_ppn REAL, run_id INTEGER);
            CREATE TABLE run (id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT, ts TEXT);
        """)
        db.execute("INSERT INTO run(id, profile, ts) VALUES (1,'moj','t')")
        db.execute("INSERT INTO run(id, profile, ts) VALUES (2,'inny','t')")
        for key, name, price, prov, run in [
            ("k1", "Tani", 2000, "wakacje.pl", 1),
            ("k2", "Drogi", 5000, "wakacje.pl", 1),
            ("k3", "Obcy profil", 900, "wakacje.pl", 2),
            ("k4", "Inny dostawca", 800, "r.pl", 1),
        ]:
            db.execute("INSERT INTO offer VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (key, prov, name, "R", "C", "2026-09-23", 5, "BB", "WAW",
                        f"https://www.wakacje.pl/oferty/x-{key}1.html"))
            db.execute("INSERT INTO price_snapshot(offer_key,ts,price,price_ppn,run_id)"
                       " VALUES (?,?,?,?,?)", (key, "t", price, 1.0, run))
        db.commit()
        return db

    def test_bierze_najtansze_oferty_wlasnego_profilu(self):
        rows = verify.offers_to_verify(self._seed(), "moj", top=8)
        self.assertEqual([r["hotel_name"] for r in rows], ["Tani", "Drogi"])

    def test_top_ogranicza_liczbe(self):
        rows = verify.offers_to_verify(self._seed(), "moj", top=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hotel_name"], "Tani")

    def test_bierze_najnowszy_snapshot_ceny(self):
        db = self._seed()
        db.execute("INSERT INTO price_snapshot(offer_key,ts,price,price_ppn,run_id)"
                   " VALUES ('k1','t2',1500,1.0,1)")
        db.commit()
        rows = verify.offers_to_verify(db, "moj", top=8)
        self.assertEqual(rows[0]["price"], 1500)

    def test_nieznany_profil_daje_pusto(self):
        self.assertEqual(verify.offers_to_verify(self._seed(), "nie-ma", top=8), [])


if __name__ == "__main__":
    unittest.main()

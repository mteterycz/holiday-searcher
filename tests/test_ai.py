"""Testy fazy 3. Zero sieci: klient Gemini jest podmieniany, opinie budowane
w pamięci, a baza to plik w katalogu tymczasowym.

Uruchomienie:
    PYTHONPATH=src python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from holiday_searcher.ai import pool as pool_mod  # noqa: E402
from holiday_searcher.ai.client import GeminiClient, GeminiError  # noqa: E402
from holiday_searcher.ai.opinions import (  # noqa: E402
    HotelOpinions, Opinion, parse_opinions_page, slug_from_url,
)
from holiday_searcher.ai.pool import (  # noqa: E402
    MODELS, ROLE_BULK_VERDICT, ROLE_DEEP, ModelPool, QuotaExhausted,
)
from holiday_searcher.ai.prompts import (  # noqa: E402
    VERDICT_SCHEMA, build_verdict_user, build_vibe_user,
)
from holiday_searcher.ai.verdicts import VerdictService, VerdictStore  # noqa: E402

VERDICT_PAYLOAD = {
    "beach": {"quality": 4, "notes": "piaszczysta, tuż przy hotelu"},
    "food": 3,
    "cleanliness": 2,
    "noise": None,
    "family_friendly": 4,
    "red_flags": ["karaluchy"],
    "one_liner": "Tanio i blisko plaży, ale z czystością bywa różnie.",
}


class FakeClient:
    """Podstawka pod GeminiClient. Liczy wywołania — na tym stoi cały test cache'u."""

    def __init__(self, available: bool = True, payload: dict | None = None,
                 error: Exception | None = None):
        self._available = available
        self.payload = payload if payload is not None else VERDICT_PAYLOAD
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    @property
    def available(self) -> bool:
        return self._available

    def generate(self, model, system, user, schema):
        self.calls.append((model, system, user))
        if self.error:
            raise self.error
        return json.loads(json.dumps(self.payload))


def sample_opinions(hotel_id: str = "35267", n: int = 3) -> HotelOpinions:
    return HotelOpinions(
        hotel_id=hotel_id, slug="testowy-hotel", rating=6.4,
        opinions=[
            Opinion(author=f"Autor{i}", rate=6.0 + i, trip_date="2025-08",
                    kind="Rodzina z dziećmi",
                    text=f"Opinia numer {i}: hotel przy plaży, jedzenie monotonne.",
                    advantage="blisko plaży", defect="karaluchy")
            for i in range(n)
        ],
    )


def memory_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db


class TestPool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "offers.db"
        self.slept: list[float] = []
        self.clock = [0.0]

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, day: str = "2026-08-25") -> ModelPool:
        return ModelPool(
            self.path,
            sleeper=self.slept.append,
            monotonic=lambda: self.clock[0],
            today=lambda: day,
        )

    def test_acquire_liczy_zuzycie_w_sqlite(self):
        p = self.make()
        model = p.acquire(ROLE_BULK_VERDICT)
        self.assertEqual(model, "gemini-3.1-flash-lite")
        self.assertEqual(p.used_today(model), 1)
        # licznik dzienny musi przeżyć restart procesu
        self.assertEqual(self.make().used_today(model), 1)

    def test_rpd_wyczerpane_wymusza_failover_wg_roli(self):
        p = self.make()
        primary, secondary = p.chain(ROLE_BULK_VERDICT)[:2]
        p.db.execute("INSERT INTO ai_usage(model, day, requests) VALUES (?,?,?)",
                     (primary, "2026-08-25", MODELS[primary].rpd))
        p.db.commit()
        self.assertEqual(p.remaining(primary), 0)
        self.assertEqual(p.acquire(ROLE_BULK_VERDICT), secondary)

    def test_strict_nie_robi_failoveru(self):
        """Jeden przebieg rankingowy = jeden model. Werdykty z dwóch modeli
        nie są porównywalne, więc strict woli brak wyniku niż podmianę."""
        p = self.make()
        primary = p.chain(ROLE_BULK_VERDICT)[0]
        p.db.execute("INSERT INTO ai_usage(model, day, requests) VALUES (?,?,?)",
                     (primary, "2026-08-25", MODELS[primary].rpd))
        p.db.commit()
        self.assertIsNone(p.acquire(ROLE_BULK_VERDICT, strict=True))

    def test_limit_wyczerpany_na_calym_lancuchu(self):
        p = self.make()
        for m in p.chain(ROLE_DEEP):
            p.db.execute("INSERT INTO ai_usage(model, day, requests) VALUES (?,?,?)",
                         (m, "2026-08-25", MODELS[m].rpd))
        p.db.commit()
        self.assertIsNone(p.acquire(ROLE_DEEP))
        with self.assertRaises(QuotaExhausted):
            p.acquire_or_raise(ROLE_DEEP)

    def test_limity_sa_osobne_per_model(self):
        p = self.make()
        p.acquire(ROLE_BULK_VERDICT)
        self.assertEqual(p.used_today("gemini-3.1-flash-lite"), 1)
        self.assertEqual(p.used_today("gemini-3.5-flash-lite"), 0)
        self.assertEqual(p.used_today("gemini-3.5-flash"), 0)

    def test_rpm_egzekwowany_sleepem(self):
        p = self.make()
        spec = MODELS["gemini-3.5-flash"]          # RPM 5
        for _ in range(spec.rpm):
            p.acquire(ROLE_DEEP)
        self.assertEqual(self.slept, [])
        p.acquire(ROLE_DEEP)                        # 6. w tej samej minucie
        self.assertEqual(len(self.slept), 1)
        self.assertGreater(self.slept[0], 59.0)

    def test_nowy_dzien_zeruje_licznik(self):
        self.make("2026-08-25").acquire(ROLE_BULK_VERDICT)
        p2 = self.make("2026-08-26")
        self.assertEqual(p2.used_today("gemini-3.1-flash-lite"), 0)
        p2.acquire(ROLE_BULK_VERDICT)
        # historia zostaje: dwa dni, ten sam model, osobne liczniki
        self.assertEqual(len(p2.usage(days=7)), 2)
        self.assertEqual(p2.used_today("gemini-3.1-flash-lite"), 1)


class TestVerdictCache(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.store = VerdictStore(self.db)
        self.pool = ModelPool(self.db, sleeper=lambda _s: None,
                              monotonic=lambda: 0.0, today=lambda: "2026-08-25")
        self.client = FakeClient()

    def service(self, prompt_version: int = 1) -> VerdictService:
        return VerdictService(self.store, self.pool, client=self.client,
                              fetcher=None, prompt_version=prompt_version)

    def test_cache_trafia_bez_drugiego_wywolania(self):
        svc = self.service()
        ops = sample_opinions()
        first = svc.get_or_create("35267", "Hotel Testowy", "Riwiera", opinions=ops)
        self.assertIsNotNone(first)
        self.assertFalse(first.from_cache)
        self.assertEqual(len(self.client.calls), 1)

        second = svc.get_or_create("35267", "Hotel Testowy", "Riwiera", opinions=ops)
        self.assertTrue(second.from_cache)
        self.assertEqual(len(self.client.calls), 1, "cache nie może wołać modelu")
        self.assertEqual(second.one_liner, first.one_liner)
        # cache trafił, więc limit dzienny też nie drgnął
        self.assertEqual(self.pool.used_today("gemini-3.1-flash-lite"), 1)

    def test_zmiana_prompt_version_uniewaznia_cache(self):
        ops = sample_opinions()
        self.service(prompt_version=1).get_or_create("35267", "Hotel", opinions=ops)
        self.assertEqual(len(self.client.calls), 1)
        v2 = self.service(prompt_version=2).get_or_create("35267", "Hotel", opinions=ops)
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(v2.prompt_version, 2)
        # oba werdykty zostają w bazie — stary nie jest kasowany, tylko nieużywany
        self.assertEqual(self.store.count(prompt_version=1), 1)
        self.assertEqual(self.store.count(prompt_version=2), 1)

    def test_werdykt_zapisuje_model_i_prompt_version(self):
        svc = self.service()
        svc.get_or_create("35267", "Hotel", opinions=sample_opinions())
        row = self.db.execute("SELECT * FROM hotel_ai_verdict").fetchone()
        self.assertEqual(row["model"], "gemini-3.1-flash-lite")
        self.assertEqual(row["prompt_version"], 1)
        self.assertEqual(row["provider"], "wakacje.pl")
        self.assertTrue(row["input_hash"])
        self.assertTrue(row["created_at"])

    def test_inny_model_to_inny_werdykt(self):
        """Klucz zawiera model, bo werdykty z różnych modeli nie są porównywalne."""
        svc = self.service()
        svc.get_or_create("35267", "Hotel", opinions=sample_opinions())
        self.assertIsNotNone(self.store.get("35267", "gemini-3.1-flash-lite"))
        self.assertIsNone(self.store.get("35267", "gemini-3.5-flash"))

    def test_null_w_werdykcie_przezywa_zapis_i_odczyt(self):
        """Brak informacji ma zostać brakiem informacji, a nie zerem."""
        svc = self.service()
        svc.get_or_create("35267", "Hotel", opinions=sample_opinions())
        cached = self.store.get("35267", "gemini-3.1-flash-lite")
        self.assertIsNone(cached.noise)
        self.assertEqual(cached.beach, 4)

    def test_normalizacja_przycina_skale(self):
        self.client.payload = dict(VERDICT_PAYLOAD, food=9, cleanliness="brak")
        v = self.service().get_or_create("1", "Hotel", opinions=sample_opinions())
        self.assertEqual(v.food, 5)
        self.assertIsNone(v.cleanliness)


class TestGracefulDegradation(unittest.TestCase):
    def setUp(self):
        self.db = memory_db()
        self.store = VerdictStore(self.db)
        self.pool = ModelPool(self.db, sleeper=lambda _s: None,
                              monotonic=lambda: 0.0, today=lambda: "2026-08-25")

    def test_brak_klucza_zwraca_none_i_nie_wywala(self):
        client = FakeClient(available=False)
        svc = VerdictService(self.store, self.pool, client=client, fetcher=None)
        v = svc.get_or_create("35267", "Hotel", opinions=sample_opinions())
        self.assertIsNone(v)
        self.assertEqual(svc.last_error, "brak GEMINI_API_KEY")
        self.assertEqual(client.calls, [])
        self.assertEqual(self.pool.used_today("gemini-3.1-flash-lite"), 0)

    def test_klient_bez_klucza_jest_niedostepny(self):
        c = GeminiClient(api_key="")
        self.assertFalse(c.available)
        with self.assertRaises(GeminiError):
            c.generate("gemini-3.1-flash-lite", "s", "u", VERDICT_SCHEMA)

    def test_wyczerpany_limit_zwraca_none(self):
        for m in self.pool.chain(ROLE_BULK_VERDICT):
            self.db.execute("INSERT INTO ai_usage(model, day, requests) VALUES (?,?,?)",
                            (m, "2026-08-25", MODELS[m].rpd))
        self.db.commit()
        svc = VerdictService(self.store, self.pool, client=FakeClient(), fetcher=None)
        v = svc.get_or_create("35267", "Hotel", opinions=sample_opinions(),
                              strict_model=False)
        self.assertIsNone(v)
        self.assertEqual(svc.last_error, "wyczerpany limit dzienny")

    def test_brak_opinii_zwraca_none_i_nie_pali_limitu(self):
        client = FakeClient()
        svc = VerdictService(self.store, self.pool, client=client, fetcher=None)
        v = svc.get_or_create("35267", "Hotel",
                              opinions=HotelOpinions("35267", error="brak opinii"))
        self.assertIsNone(v)
        self.assertEqual(svc.last_error, "brak opinii")
        self.assertEqual(client.calls, [])
        self.assertEqual(self.pool.used_today("gemini-3.1-flash-lite"), 0)

    def test_blad_gemini_nie_przerywa_przebiegu(self):
        client = FakeClient(error=GeminiError("HTTP 503", 503))
        svc = VerdictService(self.store, self.pool, client=client, fetcher=None)
        self.assertIsNone(svc.get_or_create("35267", "Hotel", opinions=sample_opinions()))
        self.assertIn("503", svc.last_error)


class TestClientHTTP(unittest.TestCase):
    """Klient przez httpx.MockTransport — bez wychodzenia do sieci."""

    def _client(self, handler, slept=None):
        transport = httpx.MockTransport(handler)
        return GeminiClient(api_key="test-key",
                            http=httpx.Client(transport=transport),
                            sleeper=(slept.append if slept is not None else (lambda _s: None)))

    def test_structured_output_jest_parsowany(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": json.dumps(VERDICT_PAYLOAD)}]}}]
            })

        out = self._client(handler).generate("gemini-3.1-flash-lite", "SYS", "USER",
                                             VERDICT_SCHEMA)
        self.assertEqual(out["food"], 3)
        self.assertIn("gemini-3.1-flash-lite:generateContent", seen["url"])
        cfg = seen["body"]["generationConfig"]
        self.assertEqual(cfg["responseMimeType"], "application/json")
        self.assertEqual(cfg["responseSchema"], VERDICT_SCHEMA)
        self.assertEqual(seen["body"]["systemInstruction"]["parts"][0]["text"], "SYS")

    def test_retry_raz_na_429(self):
        calls = {"n": 0}
        slept: list[float] = []

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "{\"ok\": true}"}]}}]
            })

        out = self._client(handler, slept).generate("m", "s", "u", VERDICT_SCHEMA)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(slept), 1)

    def test_drugi_blad_konczy_wyjatkiem(self):
        def handler(request):
            return httpx.Response(503, text="down")

        with self.assertRaises(GeminiError) as ctx:
            self._client(handler).generate("m", "s", "u", VERDICT_SCHEMA)
        self.assertEqual(ctx.exception.status, 503)


class TestPromptsIGrounding(unittest.TestCase):
    def test_schema_dopuszcza_null_wszedzie_gdzie_trzeba(self):
        props = VERDICT_SCHEMA["properties"]
        for key in ("food", "cleanliness", "noise", "family_friendly", "one_liner", "beach"):
            self.assertTrue(props[key].get("nullable"), key)
        self.assertTrue(props["beach"]["properties"]["quality"]["nullable"])

    def test_prompt_zabrania_wiedzy_wlasnej(self):
        from holiday_searcher.ai.prompts import VERDICT_SYSTEM, VIBE_SYSTEM
        for text in (VERDICT_SYSTEM, VIBE_SYSTEM):
            self.assertIn("NIE WOLNO", text)
            self.assertIn("własnej wiedzy", text)

    def test_uzytkownik_dostaje_wylacznie_opinie(self):
        user = build_verdict_user("Hotel Testowy", "Riwiera Turecka",
                                  sample_opinions().opinions)
        self.assertIn("Hotel Testowy", user)
        self.assertIn("PLUSY: blisko plaży", user)
        self.assertIn("MINUSY: karaluchy", user)
        self.assertIn("Opinia 3", user)

    def test_brak_opinii_jest_powiedziany_wprost(self):
        self.assertIn("null", build_verdict_user("H", "R", []))

    def test_vibe_pokazuje_nulle_jako_null(self):
        text = build_vibe_user("cisza i szeroka plaża", [{
            "hotel_id": "35267", "name": "Hotel", "region": "Riwiera",
            "verdict": dict(VERDICT_PAYLOAD, noise=None),
        }])
        self.assertIn("hotel_id: 35267", text)
        self.assertIn("cisza=null", text)
        self.assertIn("czerwone flagi: karaluchy", text)


class TestOpinionsParsing(unittest.TestCase):
    HTML = """
    <html><body>
    <script> var opinions = [{"note":"podglad","authorName":"X"}]; </script>
    <div class='item'><div class='item__title'>Położenie</div>
      <div class='item__progress'><div class='score'>8.0</div></div></div>
    <script>
      var opinions = [{"authorName":"Remigiusz","rate":7.71,
        "tripDateAt":{"date":"2026-08-01 00:00:00.000000"},
        "note":"Hotel ok,\\n maly basen.","advantage":"Blisko plazy ",
        "defect":"Insekty ","kindOfTrip":"Rodzina z dziecmi","isClient":true},
       {"authorName":"Pusta","rate":3.0,"note":"","advantage":"","defect":""}];
    </script>
    <script type="application/ld+json">{"aggregateRating":{"ratingValue":"5.3"}}</script>
    </body></html>
    """

    def test_parsuje_pelny_blok_a_nie_podglad(self):
        data = parse_opinions_page(self.HTML, "35267", "arsi-paradise-beach")
        self.assertEqual(len(data), 1)          # pusta opinia odpada
        op = data.opinions[0]
        self.assertEqual(op.author, "Remigiusz")
        self.assertEqual(op.rate, 7.71)
        self.assertEqual(op.trip_date, "2026-08")
        self.assertEqual(op.text, "Hotel ok, maly basen.")
        self.assertEqual(op.advantage, "Blisko plazy")
        self.assertTrue(op.verified)
        self.assertEqual(data.subscores["Położenie"], 8.0)
        self.assertEqual(data.rating, 5.3)
        self.assertTrue(data.ok)

    def test_strona_bez_opinii_nie_wywala(self):
        data = parse_opinions_page("<html></html>", "270560", "club-bayar")
        self.assertFalse(data.ok)
        self.assertEqual(len(data), 0)
        self.assertEqual(data.error, "brak opinii na stronie")

    def test_slug_z_adresu_oferty(self):
        self.assertEqual(
            slug_from_url("https://www.wakacje.pl/hotele/arsi-paradise-beach/"),
            "arsi-paradise-beach")
        self.assertEqual(
            slug_from_url("https://www.wakacje.pl/opinie/hotele/arsi-paradise-beach-h35267.html"),
            "arsi-paradise-beach")
        self.assertEqual(slug_from_url(""), "")

    def test_fingerprint_zmienia_sie_z_trescia(self):
        a = sample_opinions(n=2).fingerprint_material()
        b = sample_opinions(n=3).fingerprint_material()
        self.assertNotEqual(a, b)


class TestModelDedykacje(unittest.TestCase):
    def test_role_maja_dedykowane_modele(self):
        self.assertEqual(pool_mod.ROLE_CHAINS["bulk-misc"][0], "gemini-3.5-flash-lite")
        self.assertEqual(pool_mod.ROLE_CHAINS["bulk-verdict"][0], "gemini-3.1-flash-lite")
        self.assertEqual(pool_mod.ROLE_CHAINS["deep"][0], "gemini-3.5-flash")
        self.assertEqual(pool_mod.ROLE_CHAINS["deep"][1], "gemini-3.6-flash")
        self.assertEqual(pool_mod.ROLE_CHAINS["experimental"], ["gemini-3.7-flash"])

    def test_limity_zgadzaja_sie_z_kontem(self):
        for name in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
            self.assertEqual((MODELS[name].rpm, MODELS[name].rpd), (15, 500))
        for name in ("gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash"):
            self.assertEqual((MODELS[name].rpm, MODELS[name].rpd), (5, 20))
        self.assertTrue(all(m.tpm == 250_000 for m in MODELS.values()))


if __name__ == "__main__":
    unittest.main()

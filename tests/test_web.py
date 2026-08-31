"""Testy dashboardu webowego (web/*) na tymczasowej bazie.

Serwer jest uruchamiany na porcie efemerycznym (port=0) w osobnym wątku tylko
na czas testu i zatrzymywany w tearDown — żaden proces nie zostaje uruchomiony
na stałe.

Dane testowe są dobrane tak, żeby pokryć wszystkie stany komponentu oceny:

* **zgodne źródła, dużo opinii** — Alpha: wakacje.pl 8.5/42, Google 8.6/1425,
  HolidayCheck 8.4/3053 → wysoka wiarygodność, brak ostrzeżenia,
* **rozjazd źródeł** — Gamma: lokalnie 10.0 z JEDNEJ opinii, w Google 6.9
  z 1000 → ocena wiodąca musi pochodzić z Google, a strona ma pokazać, że
  źródła się rozjeżdżają,
* **sama pojedyncza opinia** — Delta: 10.0 z 1 opinii i żadnego potwierdzenia
  z zewnątrz (wiersze `hotel_external_rating` są, ale ze statusem innym niż
  `ok`, więc NIE mogą trafić na ekran),
* **brak oceny** — Beta.

Osobne klasy sprawdzają: bazę zupełnie pustą, bazę bez tabel opcjonalnych
oraz eksport statyczny (`hs export`).

Uruchomienie: python3 -m unittest tests.test_web -v
(uruchamiane też bez PYTHONPATH=src — ten plik sam dokłada src/ do sys.path).
"""
from __future__ import annotations

import itertools
import json
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from holiday_searcher.models import Offer  # noqa: E402
from holiday_searcher.storage import Storage  # noqa: E402
from holiday_searcher.web import data as webdata  # noqa: E402
from holiday_searcher.web import styles as webstyles  # noqa: E402
from holiday_searcher.web.server import build_server  # noqa: E402
from holiday_searcher.web.static_export import export_site  # noqa: E402

# --------------------------------------------------------------------------
# Zasoby zewnętrzne
#
# Dashboard był kiedyś w 100% self-contained. Nowa szata sięga po trzy kroje
# z Google Fonts — i to JEDYNY dopuszczony wyjątek. Poniższe sprawdzenie
# przepuszcza dokładnie te dwa hosty i odrzuca wszystkie inne, żeby lista nie
# rozrosła się przy okazji kolejnej zmiany.
# --------------------------------------------------------------------------

ALLOWED_ASSET_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")

# href/src elementów ładujących zasoby oraz url(...) w CSS.
_ASSET_RE = re.compile(
    r'<(?:link|script|img|iframe|source|embed)\b[^>]*?\b(?:href|src)\s*=\s*"([^"]*)"',
    re.IGNORECASE,
)
_CSS_URL_RE = re.compile(r'url\(\s*[\'"]?([^)\'"]+)', re.IGNORECASE)


def assert_only_font_assets(case: unittest.TestCase, html: str, label: str = "") -> None:
    """Każdy zasób ładowany przez stronę musi być lokalny albo z Google Fonts."""
    for url in itertools.chain(_ASSET_RE.findall(html), _CSS_URL_RE.findall(html)):
        if not url.startswith(("http://", "https://", "//")):
            continue   # relatywny -> lokalny, w porządku
        host = urllib.parse.urlparse(url if "//" != url[:2] else "https:" + url).netloc
        case.assertIn(host, ALLOWED_ASSET_HOSTS, f"{label}: obcy zasób {url!r}")


# --------------------------------------------------------------------------
# Kontrast WCAG
# --------------------------------------------------------------------------

def _rel_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    channels = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255.0
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float:
    a, b = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)

_VERDICT_SCHEMA = """
CREATE TABLE IF NOT EXISTS hotel_ai_verdict (
    hotel_id       TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    input_hash     TEXT NOT NULL,
    verdict_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (hotel_id, prompt_version, model)
);
"""

_EXTERNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS hotel_external_rating (
    hotel_id     TEXT NOT NULL,
    source       TEXT NOT NULL,
    matched_name TEXT,
    rating_0_10  REAL,
    review_count INTEGER,
    url          TEXT,
    confidence   REAL,
    status       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (hotel_id, source)
);
"""

_VERIFY_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_verification (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_key     TEXT NOT NULL,
    checked_at    TEXT NOT NULL,
    listing_price INTEGER,
    final_price   INTEGER,
    diff_pct      REAL,
    details_json  TEXT
);
"""

_CALENDAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_calendar (
    profile        TEXT NOT NULL,
    hotel_id       TEXT NOT NULL DEFAULT '',
    departure_date TEXT NOT NULL,
    nights         INTEGER NOT NULL,
    price_pp       INTEGER NOT NULL,
    price_ppn      REAL NOT NULL,
    checked_at     TEXT NOT NULL,
    PRIMARY KEY (profile, hotel_id, departure_date, nights, checked_at)
);
"""


def _make_offer(hotel_id: str, price: int, hotel_name: str, country: str,
                region: str = "Riwiera Turecka", city: str = "Alanya",
                rating=8.5, rating_count=42, tour_operator: str = "TestOp") -> Offer:
    return Offer(
        provider="test",
        hotel_name=hotel_name,
        hotel_id=hotel_id,
        tour_operator=tour_operator,
        country=country,
        region=region,
        city=city,
        stars=4.0,
        departure_date=date(2026, 9, 19),
        return_date=date(2026, 9, 26),
        nights=7,
        board="AI",
        board_raw="All Inclusive",
        departure_place="Warszawa",
        departure_code="WAW",
        room_type="Standard",
        price=price,
        price_old=0,
        rating=rating,
        rating_count=rating_count if rating else None,
        url="https://www.wakacje.pl/hotele/przyklad/",
        raw_id=hotel_id,
    )


def _insert_snapshot(store: Storage, offer_key: str, price: int, ts: str) -> None:
    store.db.execute(
        "INSERT INTO price_snapshot(offer_key, ts, price, price_ppn, run_id) VALUES (?,?,?,?,?)",
        (offer_key, ts, price, round(price / 7, 2), None),
    )
    store.db.commit()


def _insert_verdict(store: Storage, hotel_id: str, data: dict, model: str = "gemini-test") -> None:
    store.db.executescript(_VERDICT_SCHEMA)
    store.db.execute(
        """INSERT INTO hotel_ai_verdict
           (hotel_id, provider, model, prompt_version, input_hash, verdict_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (hotel_id, "wakacje.pl", model, 1, "deadbeef",
         json.dumps(data, ensure_ascii=False),
         datetime.now().isoformat(timespec="seconds")),
    )
    store.db.commit()


def _insert_external(store: Storage, hotel_id: str, source: str, rating, count,
                     status: str = "ok", url: str = "", matched: str = "Match") -> None:
    store.db.executescript(_EXTERNAL_SCHEMA)
    store.db.execute(
        """INSERT OR REPLACE INTO hotel_external_rating
           (hotel_id, source, matched_name, rating_0_10, review_count, url,
            confidence, status, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (hotel_id, source, matched, rating, count, url, 1.0, status,
         datetime.now().isoformat(timespec="seconds")),
    )
    store.db.commit()


class _ServerTestMixin:
    """Wspólny setup/teardown: buduje serwer na porcie 0 wokół self.db_path."""

    def _start_server(self) -> None:
        self.server = build_server(port=0, db_path=self.db_path)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmpdir.cleanup()

    def _get(self, path: str) -> tuple[int, str]:
        url = f"http://{self.host}:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # 4xx/5xx: urlopen podnosi wyjątek zamiast zwrócić odpowiedź.
            return exc.code, exc.read().decode("utf-8")


def _seed_full_db(db_path: Path) -> dict:
    """Buduje pełną bazę testową. Zwraca słownik ofert (nazwa -> Offer)."""
    store = Storage(db_path)
    run_id = store.start_run("test-profil", "test")

    offers = {
        # Turcja: dużo opinii, źródła zgodne + hotel bez żadnej oceny.
        "alpha": _make_offer("h1", 2000, "Hotel Alpha Resort", "Turcja",
                             rating=8.5, rating_count=42),
        "beta": _make_offer("h2", 3500, "Beta Beach Hotel", "Turcja",
                            rating=None, rating_count=None),
        # Grecja: rozjazd źródeł + pojedyncza opinia bez potwierdzenia.
        "gamma": _make_offer("h3", 2800, "Gamma & Sons Resort", "Grecja",
                             region="Kreta", city="Chania", rating=10.0, rating_count=1),
        "delta": _make_offer("h4", 2400, "Delta Solo Opinion", "Grecja",
                             region="Kreta", city="Rethymno", rating=10.0, rating_count=1),
    }
    store.save(list(offers.values()), run_id)

    now = datetime.now()
    _insert_snapshot(store, offers["alpha"].key, 2400, (now - timedelta(days=5)).isoformat(timespec="seconds"))
    _insert_snapshot(store, offers["alpha"].key, 2000, (now - timedelta(days=1)).isoformat(timespec="seconds"))
    store.finish_run(run_id, 4)

    # Oceny zewnętrzne dla obu źródeł.
    _insert_external(store, "h1", "google", 8.6, 1425, url="https://maps.example/h1")
    _insert_external(store, "h1", "holidaycheck", 8.4, 3053, url="https://hc.example/h1")
    # „1 opinia lokalnie, 1000 zewnętrznie" + wyraźny rozjazd (10.0 vs 6.9).
    _insert_external(store, "h3", "google", 6.9, 1000, url="https://maps.example/h3")
    # Delta: wiersze SĄ, ale żaden nie ma statusu ok — nie mogą trafić na ekran.
    _insert_external(store, "h4", "google", None, None, status="ambiguous")
    _insert_external(store, "h4", "holidaycheck", 9.9, 500, status="no_match")

    _insert_verdict(store, "h3", {
        "beach": {"quality": 2, "notes": "Kamienista plaża, daleko od hotelu."},
        "food": 2, "cleanliness": 1, "noise": 3, "family_friendly": 2,
        "red_flags": ["karaluchy w pokojach", "zatrucia pokarmowe u kilku gości",
                      "kradzieże z sejfu", "mały balkon"],
        "one_liner": "Tani, ale ryzykowny wybór — częste skargi na higienę.",
    })

    # Weryfikacja ceny dla Alphy.
    store.db.executescript(_VERIFY_SCHEMA)
    store.db.execute(
        """INSERT INTO price_verification
           (offer_key, checked_at, listing_price, final_price, diff_pct, details_json)
           VALUES (?,?,?,?,?,?)""",
        (offers["alpha"].key, now.isoformat(timespec="seconds"), 2000, 2180, 9.0,
         json.dumps({"verdict": "odchylenie", "note": "2 warianty pokoi; bagaż płatny osobno",
                     "variants": [{"room_desc": "Standard podwójny", "price_pp": 2180,
                                   "features": ["All Inclusive"]}]}, ensure_ascii=False)),
    )

    # Kalendarz cen: 2 daty × 2 długości pobytu, minimum w rogu.
    store.db.executescript(_CALENDAR_SCHEMA)
    ts = now.isoformat(timespec="seconds")
    for dep, nights, pp in (("2026-09-19", 7, 2000), ("2026-09-19", 5, 1700),
                            ("2026-09-26", 7, 1750), ("2026-09-26", 5, 1650)):
        store.db.execute(
            "INSERT INTO price_calendar(profile, hotel_id, departure_date, nights, "
            "price_pp, price_ppn, checked_at) VALUES (?,?,?,?,?,?,?)",
            ("test-profil", "", dep, nights, pp, round(pp / nights, 2), ts),
        )
    store.db.commit()
    store.db.close()
    return offers


class WebDashboardTestCase(_ServerTestMixin, unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test-offers.db"
        self.offers = _seed_full_db(self.db_path)
        self.hotel_a = self.offers["alpha"]
        self.hotel_b = self.offers["beta"]
        self.hotel_c = self.offers["gamma"]
        self.hotel_d = self.offers["delta"]
        self._start_server()

    # --- strony podstawowe -------------------------------------------------

    def test_index_ok(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Przegląd", body)
        self.assertIn("test-profil", body)
        self.assertIn("Turcja", body)
        self.assertIn("Grecja", body)
        self.assertIn("TOP 5", body)

    def test_index_top_rated_uses_only_confirmed_ratings(self):
        """Ranking ocen nie może wygrywać hotelem z jedną entuzjastyczną opinią."""
        status, body = self._get("/")
        self.assertEqual(status, 200)
        section = body[body.find("najlepiej ocenianych"):body.find("największych spadków")]
        self.assertIn("Hotel Alpha Resort", section)     # 3053 opinii
        self.assertNotIn("Delta Solo Opinion", section)  # 1 opinia

    def test_offers_lists_hotels_grouped_by_country(self):
        status, body = self._get("/offers")
        self.assertEqual(status, 200)
        self.assertIn("Hotel Alpha Resort", body)
        self.assertIn("Beta Beach Hotel", body)
        self.assertIn("Gamma &amp; Sons Resort", body)  # html.escape na "&"
        self.assertIn("Turcja", body)
        self.assertIn("Grecja", body)
        self.assertIn("section-header", body)

        # Termin w formacie DD.MM–DD.MM i liczba nocy muszą być widoczne.
        self.assertIn("19.09", body)
        self.assertIn("26.09", body)
        self.assertIn("7 nocy", body)

    def test_offers_supports_all_sorts(self):
        for sort in ("price", "ppn", "rating", "drop", "date"):
            status, body = self._get(f"/offers?sort={sort}")
            self.assertEqual(status, 200, f"sort={sort}")
            self.assertIn("Hotel Alpha Resort", body)

    def test_offers_country_filter(self):
        status, body = self._get("/offers?country=Turcja")
        self.assertEqual(status, 200)
        self.assertIn("Hotel Alpha Resort", body)
        self.assertNotIn("Gamma &amp; Sons Resort", body)

    def test_offers_max_price_filter(self):
        status, body = self._get("/offers?max_price=2100")
        self.assertEqual(status, 200)
        self.assertIn("Hotel Alpha Resort", body)   # 2000 zł
        self.assertNotIn("Beta Beach Hotel", body)  # 3500 zł

    def test_offers_min_rating_filter_uses_leading_source(self):
        """Filtr oceny działa na ocenie wiodącej, nie na samej lokalnej:
        Gamma ma lokalnie 10.0, ale wiodące Google daje 6.9 — ma wypaść."""
        status, body = self._get("/offers?min_rating=8.0")
        self.assertEqual(status, 200)
        self.assertIn("Hotel Alpha Resort", body)          # wiodące 8.4
        self.assertIn("Delta Solo Opinion", body)          # wiodące 10.0 (1 opinia)
        self.assertNotIn("Gamma &amp; Sons Resort", body)  # wiodące 6.9
        self.assertNotIn("Beta Beach Hotel", body)         # brak oceny

    def test_offers_sort_and_country_combined(self):
        status, body = self._get("/offers?sort=drop&country=Turcja")
        self.assertEqual(status, 200)
        self.assertIn("Hotel Alpha Resort", body)

    def test_offers_shows_sparkline_for_offer_with_history(self):
        status, body = self._get("/offers")
        self.assertEqual(status, 200)
        self.assertIn('class="sparkline"', body)

    def test_offers_shows_ai_warning_for_severe_flags(self):
        status, body = self._get("/offers")
        self.assertEqual(status, 200)
        self.assertIn("Uwaga w opiniach AI", body)
        self.assertIn("flag-badge severe", body)

    # --- komponent oceny z wiarygodnością ---------------------------------

    def test_offers_rating_component_shows_count_and_confidence(self):
        status, body = self._get("/offers")
        self.assertEqual(status, 200)
        # Alpha: ocena wiodąca z HolidayCheck (najwięcej opinii) + wysoka wiarygodność.
        self.assertIn("conf-high", body)
        self.assertIn("3 053 opinie", body)  # 3053 -> "opinie" (odmiana PL)
        self.assertIn("wysoka wiarygodność", body)
        # Wszystkie trzy źródła są wypisane obok siebie.
        self.assertIn("HolidayCheck", body)
        self.assertIn("Google", body)
        self.assertIn("wakacje.pl", body)

    def test_offers_single_review_rating_is_visually_degraded(self):
        """10.0 z jednej opinii ma inną klasę wizualną niż ocena z tysiąca."""
        status, body = self._get("/offers")
        self.assertEqual(status, 200)
        self.assertIn("conf-thin", body)
        self.assertIn("tylko 1 opinia", body)
        self.assertIn("pojedyncze opinie", body)

    def test_offers_warns_when_sources_disagree(self):
        status, body = self._get("/offers")
        self.assertEqual(status, 200)
        self.assertIn("Źródła się rozjeżdżają", body)
        self.assertIn("6.9 (Google)", body)

    def test_external_rating_with_status_other_than_ok_is_ignored(self):
        """`ambiguous`/`no_match` to „nie wiemy", nie ocena — nie wolno ich pokazać."""
        status, body = self._get("/offers")
        self.assertEqual(status, 200)
        self.assertNotIn("9.9", body)  # ocena Delty z wiersza no_match

    def test_offer_without_any_rating_says_so(self):
        status, body = self._get(f"/offer/{self.hotel_b.key}")
        self.assertEqual(status, 200)
        self.assertIn("brak potwierdzonych ocen", body)

    def test_build_rating_summary_prefers_most_reviewed_source(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            ext = webdata.load_external_ratings(conn)
            s = webdata.build_rating_summary(10.0, 1, ext.get("h3"))
        finally:
            conn.close()
        self.assertEqual(s.headline.key, "google")
        self.assertEqual(s.headline.count, 1000)
        self.assertEqual(s.confidence, "high")
        self.assertGreater(s.spread, webdata.DISAGREE_THRESHOLD)

    # --- strona oferty ------------------------------------------------------

    def test_offer_detail_has_hotel_name_and_svg_chart(self):
        status, body = self._get(f"/offer/{self.hotel_a.key}")
        self.assertEqual(status, 200)
        self.assertIn("Hotel Alpha Resort", body)
        self.assertIn("<svg", body)
        self.assertIn("</svg>", body)
        self.assertIn("2 400", body)  # fmt_money wstawia spację jako separator tysięcy
        self.assertIn("2 000", body)
        self.assertIn("19.09", body)

    def test_offer_detail_shows_price_verification(self):
        status, body = self._get(f"/offer/{self.hotel_a.key}")
        self.assertEqual(status, 200)
        self.assertIn("Weryfikacja ceny", body)
        self.assertIn("2 180", body)          # cena po wejściu w ofertę
        self.assertIn("+9.0%", body)
        self.assertIn("bagaż płatny osobno", body)
        self.assertIn("Standard podwójny", body)

    def test_offer_detail_shows_full_ai_verdict_with_severe_banner(self):
        status, body = self._get(f"/offer/{self.hotel_c.key}")
        self.assertEqual(status, 200)
        self.assertIn("Gamma &amp; Sons Resort", body)
        self.assertIn("Poważne zastrzeżenia", body)
        self.assertIn("karaluchy w pokojach", body)
        self.assertIn("Tani, ale ryzykowny wybór", body)
        self.assertIn("score-row", body)  # paski ocen cząstkowych

    def test_offer_detail_separates_minor_from_severe_flags(self):
        status, body = self._get(f"/offer/{self.hotel_c.key}")
        self.assertEqual(status, 200)
        self.assertIn("Drobniejsze uwagi", body)
        self.assertIn("mały balkon", body)
        # Drobiazg nie może wylądować w banerze poważnych zastrzeżeń.
        banner = body[body.find("flag-banner"):body.find("Drobniejsze uwagi")]
        self.assertNotIn("mały balkon", banner)

    def test_offer_detail_unknown_key_is_404(self):
        status, body = self._get("/offer/nieistniejacy-klucz")
        self.assertEqual(status, 404)
        self.assertIn("Nie znaleziono", body)

    def test_unknown_path_is_404(self):
        status, body = self._get("/nie-ma-takiej-strony")
        self.assertEqual(status, 404)

    def test_drops_shows_biggest_drop_first(self):
        status, body = self._get("/drops")
        self.assertEqual(status, 200)
        self.assertIn("Hotel Alpha Resort", body)
        self.assertNotEqual(body.find("Hotel Alpha Resort"), -1)

    def test_single_snapshot_offer_renders_chart_without_history(self):
        status, body = self._get(f"/offer/{self.hotel_b.key}")
        self.assertEqual(status, 200)
        self.assertIn("Beta Beach Hotel", body)
        self.assertIn("<svg", body)

    def test_hotels_page_groups_variants(self):
        status, body = self._get("/hotels")
        self.assertEqual(status, 200)
        self.assertIn("Hotel Alpha Resort", body)
        self.assertIn("Beta Beach Hotel", body)
        self.assertIn("Gamma &amp; Sons Resort", body)
        self.assertIn("1 wariant", body)  # każdy hotel testowy ma tylko jeden wariant

    def test_hotels_page_supports_sort(self):
        for sort in ("price", "rating", "variants"):
            status, body = self._get(f"/hotels?sort={sort}")
            self.assertEqual(status, 200, f"sort={sort}")
            self.assertIn("Hotel Alpha Resort", body)

    # --- kalendarz ----------------------------------------------------------

    def test_calendar_page_renders_grid_with_minimum(self):
        status, body = self._get("/kalendarz")
        self.assertEqual(status, 200)
        self.assertIn("Kalendarz cen", body)
        self.assertIn("2026-09-26", body)
        self.assertIn("5 nocy", body)
        self.assertIn("7 nocy", body)
        self.assertIn("cell best", body)      # minimum wyróżnione
        self.assertIn("1 650", body)          # najtańsza komórka

    def test_calendar_link_in_nav_when_data_exists(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn('href="/kalendarz"', body)

    # --- warstwa wizualna ---------------------------------------------------

    def test_pages_are_self_contained_and_themed(self):
        for path in ("/", "/offers", "/hotels", "/drops", "/kalendarz"):
            status, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("prefers-color-scheme: dark", body, path)
            self.assertNotIn("<script src=", body, path)   # żadnego obcego JS
            self.assertIn('name="viewport"', body, path)
            assert_only_font_assets(self, body, path)

    def test_body_has_explicit_background_token(self):
        _status, body = self._get("/")
        self.assertIn("background: var(--paper)", body)

    def test_reduced_motion_is_respected(self):
        _status, body = self._get("/")
        self.assertIn("prefers-reduced-motion: reduce", body)

    def test_print_stylesheet_exists(self):
        _status, body = self._get("/offers")
        self.assertIn("@media print", body)
        # Link musi dać się przepisać z wydruku.
        self.assertIn('a[href^="http"]::after', body)

    # --- praktyczność: tryb tabeli, licznik, sygnał -------------------------

    def test_offers_has_card_and_table_view_toggle(self):
        status, body = self._get("/offers")
        self.assertEqual(status, 200)
        self.assertIn('data-view-btn="cards"', body)
        self.assertIn('data-view-btn="table"', body)
        self.assertIn('class="offer-table"', body)
        self.assertIn("hs.offers.view", body)      # klucz localStorage
        self.assertIn("localStorage", body)
        # Każda oferta jest i kartą, i wierszem — filtr rusza obydwa naraz.
        self.assertEqual(body.count('class="offer-card"'), body.count('class="offer-row"'))

    def test_offers_result_counter_shows_narrowing(self):
        _status, full = self._get("/offers")
        self.assertIn('id="offers-count"', full)
        self.assertIn('data-base="4"', full)
        _status, filtered = self._get("/offers?country=Turcja")
        # „4 → 2 oferty": baza, strzałka, wynik.
        self.assertIn('<span class="of">4</span>', filtered)
        self.assertIn("→", filtered)
        self.assertIn("<b>2</b> oferty", filtered)

    def test_signal_marks_cheapest_offer_once_per_card(self):
        """`--signal` to wyróżnienie treściowe używane oszczędnie: najwyżej
        raz na kartę i tylko na najtańszej ofercie w kraju."""
        _status, body = self._get("/offers")
        self.assertIn("najtaniej w kraju", body)
        self.assertIn("Najtańsza oferta w grupie: Turcja", body)
        # Dwa kraje -> dokładnie dwa znaczniki na całej liście kart.
        self.assertEqual(body.count('class="offer-flag"'), 2)
        for card in body.split('<article class="offer-card"')[1:]:
            self.assertLessEqual(card.count('class="offer-flag"'), 1)

    def test_rating_uses_segmented_gauge_not_bar(self):
        _status, body = self._get("/offers")
        self.assertIn('class="gauge"', body)
        self.assertNotIn("conf-bar", body)
        # Alpha ma 3053 opinie -> miernik pełny (5 z 5).
        self.assertIn("wielkość próby: 5 z 5", body)
        # Delta ma 1 opinię -> jedno pole i etykieta w --warn.
        self.assertIn("wielkość próby: 1 z 5", body)
        self.assertIn("słabe dowody", body)

    def test_fonts_have_real_fallbacks(self):
        """Strona otwarta bez sieci ma wyglądać poprawnie — każdy z trzech
        krojów musi mieć pełny stos zapasowy."""
        _status, body = self._get("/")
        self.assertIn('Georgia, "Times New Roman", serif', body)
        self.assertIn('system-ui, -apple-system, "Segoe UI"', body)
        self.assertIn("ui-monospace", body)


class WebDashboardNoOptionalTablesTestCase(_ServerTestMixin, unittest.TestCase):
    """Baza ma oferty, ale ŻADNEJ z tabel dokładanych przez późniejsze fazy
    (werdykty AI, oceny zewnętrzne, weryfikacja ceny, kalendarz)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "bare-offers.db"
        store = Storage(self.db_path)
        run_id = store.start_run("goly-profil", "test")
        self.offer = _make_offer("h9", 1900, "Hotel Bez Dodatków", "Malta", rating=8.0)
        store.save([self.offer], run_id)
        store.finish_run(run_id, 1)
        store.db.close()
        self._start_server()

    def test_optional_tables_really_absent(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            for name in ("hotel_ai_verdict", "hotel_external_rating",
                         "price_verification", "price_calendar"):
                self.assertFalse(webdata.table_exists(conn, name), name)
        finally:
            conn.close()

    def test_all_pages_render(self):
        for path in ("/", "/offers", "/hotels", "/drops", "/kalendarz"):
            status, body = self._get(path)
            self.assertEqual(status, 200, path)
        status, body = self._get(f"/offer/{self.offer.key}")
        self.assertEqual(status, 200)
        self.assertIn("Hotel Bez Dodatków", body)
        self.assertNotIn("Weryfikacja ceny", body)

    def test_no_calendar_link_without_data(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertNotIn('href="/kalendarz"', body)

    def test_local_rating_alone_still_renders(self):
        status, body = self._get("/offers")
        self.assertEqual(status, 200)
        self.assertIn("wakacje.pl", body)
        self.assertIn("8.0", body)


class WebDashboardEmptyDbTestCase(_ServerTestMixin, unittest.TestCase):
    """Baza istnieje (schemat utworzony), ale nie ma żadnych ofert ani werdyktów —
    dashboard MUSI działać i pokazać czytelny komunikat, a nie wyjątek/500."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "empty-offers.db"
        store = Storage(self.db_path)  # tworzy tylko schemat, zero ofert
        store.db.close()
        self._start_server()

    def test_index_on_empty_db(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Przegląd", body)
        self.assertIn("Baza jest pusta", body)

    def test_offers_on_empty_db(self):
        status, body = self._get("/offers")
        self.assertEqual(status, 200)
        self.assertIn("Oferty", body)

    def test_hotels_on_empty_db(self):
        status, body = self._get("/hotels")
        self.assertEqual(status, 200)

    def test_drops_on_empty_db(self):
        status, body = self._get("/drops")
        self.assertEqual(status, 200)

    def test_calendar_on_empty_db(self):
        status, body = self._get("/kalendarz")
        self.assertEqual(status, 200)
        self.assertIn("Brak danych kalendarza", body)

    def test_offer_detail_on_empty_db_is_404_not_500(self):
        status, body = self._get("/offer/whatever")
        self.assertEqual(status, 404)

    def test_filters_on_empty_db_do_not_crash(self):
        status, body = self._get("/offers?sort=drop&country=Malta&max_price=1000&min_rating=4")
        self.assertEqual(status, 200)


class StaticExportTestCase(unittest.TestCase):
    """`hs export` — statyczna migawka, która ma działać z dysku i z hostingu."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        base = Path(cls._tmpdir.name)
        cls.db_path = base / "export-offers.db"
        cls.offers = _seed_full_db(cls.db_path)
        cls.out = base / "dist"
        cls.files = export_site(cls.db_path, cls.out)
        cls.pages = {p.relative_to(cls.out).as_posix(): p.read_text(encoding="utf-8")
                     for p in cls.files}

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def test_expected_files_exist_and_are_not_empty(self):
        for name in ("index.html", "offers.html", "hotels.html", "drops.html", "kalendarz.html"):
            self.assertIn(name, self.pages, name)
            self.assertGreater(len(self.pages[name]), 2000, name)

    def test_offer_pages_generated_for_every_offer(self):
        for offer in self.offers.values():
            name = f"offer/{offer.key}.html"
            self.assertIn(name, self.pages, name)
            self.assertIn(offer.hotel_name.replace("&", "&amp;"), self.pages[name])

    def test_no_http_references_outside_wakacje_links(self):
        """Nic nie może wołać po sieć — jedyne dozwolone `http://` to link do oferty."""
        for name, html in self.pages.items():
            idx = 0
            while True:
                idx = html.find("http://", idx)
                if idx == -1:
                    break
                self.assertTrue(
                    html.startswith("http://www.wakacje.pl", idx)
                    or html.startswith("http://wakacje.pl", idx),
                    f"{name}: obce http:// -> {html[idx:idx + 60]!r}",
                )
                idx += 1

    def test_external_assets_limited_to_google_fonts(self):
        """Jedyne dopuszczone zasoby zewnętrzne to dwa hosty Google Fonts.
        Wszystko inne — CDN-y, obrazki, obcy JS — jest błędem."""
        for name, html in self.pages.items():
            assert_only_font_assets(self, html, name)
            self.assertNotIn("<script src=", html, name)   # JS zawsze inline
            self.assertIn("<style>", html, name)           # CSS zawsze inline
            for host in webstyles.FONT_HOSTS:
                self.assertIn(host, html, name)

    def test_font_stack_degrades_without_network(self):
        """Migawka otwarta z dysku bez internetu ma wyglądać poprawnie —
        każdy krój ma pełny stos zapasowy, więc brak Google Fonts zmienia
        tylko krój, nie układ."""
        html = self.pages["index.html"]
        self.assertIn('Georgia, "Times New Roman", serif', html)
        self.assertIn('system-ui, -apple-system, "Segoe UI"', html)
        self.assertIn('ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace', html)

    def test_offers_export_has_table_mode_and_persisted_choice(self):
        html = self.pages["offers.html"]
        self.assertIn('data-view-btn="table"', html)
        self.assertIn('class="offer-table"', html)
        self.assertIn("hs.offers.view", html)
        # Dostęp do localStorage musi być opakowany w try/catch — przy file://
        # i w trybie prywatnym potrafi rzucić.
        self.assertIn("try { return window.localStorage.getItem(KEY); }", html)

    def test_links_are_relative_with_html_extension(self):
        index = self.pages["index.html"]
        self.assertIn('href="offers.html"', index)
        self.assertIn('href="hotels.html"', index)
        self.assertIn('href="drops.html"', index)
        self.assertIn('href="kalendarz.html"', index)
        self.assertNotIn('href="/', index)
        # Strony ofert leżą piętro niżej i muszą wracać przez ../
        detail = self.pages[f"offer/{self.offers['alpha'].key}.html"]
        self.assertIn('href="../offers.html"', detail)
        self.assertNotIn('href="/', detail)

    def test_offers_page_has_client_side_filtering(self):
        html = self.pages["offers.html"]
        self.assertIn("<script>", html)
        self.assertIn("data-sort=", html)
        self.assertIn('data-price="2000"', html)
        self.assertIn("offers-total", html)
        # Wszystkie oferty są w pliku — filtry działają na gotowej liście.
        for offer in self.offers.values():
            self.assertIn(offer.hotel_name.replace("&", "&amp;"), html)

    def test_footer_has_generation_date_and_offer_count(self):
        footer = self.pages["index.html"]
        self.assertIn("Wygenerowano:", footer)
        self.assertIn(datetime.now().strftime("%Y-%m-%d"), footer)
        self.assertIn("4 oferty", footer)

    def test_rating_component_survives_export(self):
        html = self.pages["offers.html"]
        self.assertIn("3 053 opinie", html)
        self.assertIn("tylko 1 opinia", html)
        self.assertIn("Źródła się rozjeżdżają", html)

    def test_calendar_and_verification_exported(self):
        self.assertIn("cell best", self.pages["kalendarz.html"])
        self.assertIn("Weryfikacja ceny", self.pages[f"offer/{self.offers['alpha'].key}.html"])

    def test_export_on_empty_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pusta.db"
            Storage(db).db.close()
            out = Path(tmp) / "dist"
            files = export_site(db, out)
            names = {p.relative_to(out).as_posix() for p in files}
            self.assertEqual(names, {"index.html", "offers.html", "hotels.html",
                                     "drops.html", "kalendarz.html"})
            self.assertIn("Baza jest pusta", (out / "index.html").read_text(encoding="utf-8"))


def _css_block(css: str, selector: str) -> str:
    """Treść pierwszego bloku `{...}` po podanym selektorze, z parowaniem klamr."""
    start = css.find(selector)
    if start == -1:
        raise AssertionError(f"brak bloku {selector!r} w arkuszu")
    i = css.index("{", start)
    depth, j = 0, i
    while j < len(css):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[i + 1:j]
        j += 1
    raise AssertionError(f"niedomknięty blok {selector!r}")


class ThemeTokensTestCase(unittest.TestCase):
    """Pełna paleta MUSI być w bazowym `:root`.

    Kolor zdefiniowany wyłącznie w `@media (prefers-color-scheme: dark)` albo
    w `[data-theme="dark"]` po prostu nie istnieje w motywie jasnym — element,
    który go używa, dostaje wtedy `unset` i znika albo traci kontrast.
    """

    def setUp(self):
        self.css = webstyles.CSS
        self.base = _css_block(self.css, ":root {")

    def _tokens(self, block: str) -> set[str]:
        return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))

    def test_dark_media_query_only_overrides_known_tokens(self):
        dark = _css_block(self.css, '@media (prefers-color-scheme: dark)')
        missing = self._tokens(dark) - self._tokens(self.base)
        self.assertFalse(missing, f"tokeny tylko w media query: {sorted(missing)}")

    def test_data_theme_dark_only_overrides_known_tokens(self):
        dark = _css_block(self.css, ':root[data-theme="dark"] {')
        missing = self._tokens(dark) - self._tokens(self.base)
        self.assertFalse(missing, f"tokeny tylko w [data-theme]: {sorted(missing)}")

    def test_dark_media_query_is_scoped_against_explicit_light(self):
        """Jawny wybór motywu jasnego musi wygrywać z preferencją systemu."""
        self.assertIn(':root:not([data-theme="light"])', self.css)

    def test_both_dark_blocks_define_the_same_roles(self):
        media = self._tokens(_css_block(self.css, '@media (prefers-color-scheme: dark)'))
        attr = self._tokens(_css_block(self.css, ':root[data-theme="dark"] {'))
        self.assertEqual(media, attr, "bloki motywu ciemnego rozjechały się")

    def test_body_background_comes_from_a_token(self):
        body = _css_block(self.css, "\nbody {")
        self.assertIn("background: var(--paper)", body)


class ContrastTestCase(unittest.TestCase):
    """Kontrast WCAG AA liczony z tokenów wprost z arkusza — nie z tabelki
    przepisanej ręcznie, żeby test złapał każdą zmianę palety."""

    SURFACES = ("--paper", "--surface", "--surface-2")
    TEXT = ("--ink", "--ink-2", "--ink-3", "--accent", "--good", "--bad", "--warn")

    @classmethod
    def setUpClass(cls):
        css = webstyles.CSS
        cls.light = cls._palette(_css_block(css, ":root {"))
        dark = _css_block(css, ':root[data-theme="dark"] {')
        cls.dark = dict(cls.light)
        cls.dark.update(cls._palette(dark))

    @staticmethod
    def _palette(block: str) -> dict[str, str]:
        return {
            name: value
            for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})\s*;", block)
        }

    def _check(self, palette: dict[str, str], label: str) -> tuple[float, str]:
        worst = (99.0, "")
        for fg, bg in itertools.product(self.TEXT, self.SURFACES):
            ratio = contrast_ratio(palette[fg], palette[bg])
            self.assertGreaterEqual(
                ratio, 4.5, f"{label}: {fg} na {bg} = {ratio:.2f} (AA wymaga 4.5)"
            )
            if ratio < worst[0]:
                worst = (ratio, f"{fg} na {bg}")
        return worst

    def test_text_on_every_surface_meets_aa_light(self):
        ratio, pair = self._check(self.light, "jasny")
        self.assertGreaterEqual(ratio, 4.5, pair)

    def test_text_on_every_surface_meets_aa_dark(self):
        ratio, pair = self._check(self.dark, "ciemny")
        self.assertGreaterEqual(ratio, 4.5, pair)

    def test_signal_is_used_as_a_fill_and_carries_readable_text(self):
        """`--signal` jest za jasny na tekst wprost na papierze, więc szata
        używa go jako WYPEŁNIENIA. Sprawdzamy parę, która naprawdę występuje:
        `--on-signal` na `--signal`."""
        for label, pal in (("jasny", self.light), ("ciemny", self.dark)):
            ratio = contrast_ratio(pal["--on-signal"], pal["--signal"])
            self.assertGreaterEqual(ratio, 4.5, f"{label}: on-signal na signal = {ratio:.2f}")

    def test_signal_marker_is_distinguishable_as_non_text(self):
        """Obrys minimum w kalendarzu to element nietekstowy — próg 3:1."""
        for label, pal in (("jasny", self.light), ("ciemny", self.dark)):
            for bg in self.SURFACES:
                ratio = contrast_ratio(pal["--signal"], pal[bg])
                self.assertGreaterEqual(ratio, 3.0, f"{label}: signal na {bg} = {ratio:.2f}")

    def test_button_label_meets_aa(self):
        for label, pal in (("jasny", self.light), ("ciemny", self.dark)):
            ratio = contrast_ratio(pal["--on-accent"], pal["--accent"])
            self.assertGreaterEqual(ratio, 4.5, f"{label}: on-accent na accent = {ratio:.2f}")

    def test_control_border_meets_non_text_threshold(self):
        """Obrys pól formularza jest jedynym nośnikiem ich granicy — 1.4.11
        wymaga 3:1 wobec sąsiadującego tła."""
        for label, pal in (("jasny", self.light), ("ciemny", self.dark)):
            for bg in self.SURFACES:
                ratio = contrast_ratio(pal["--control-line"], pal[bg])
                self.assertGreaterEqual(ratio, 3.0, f"{label}: control-line na {bg} = {ratio:.2f}")


class GaugeTestCase(unittest.TestCase):
    """Segmentowany miernik wielkości próby — podziałka musi być monotoniczna
    i musi odróżniać „brak danych" od „jedna opinia"."""

    def test_zero_reviews_means_zero_segments(self):
        self.assertEqual(webdata.confidence_segments(0), 0)
        self.assertEqual(webdata.confidence_segments(None), 0)

    def test_one_review_still_lights_one_segment(self):
        self.assertEqual(webdata.confidence_segments(1), 1)

    def test_scale_is_monotonic_and_capped(self):
        prev = -1
        for n in (0, 1, 5, 10, 60, 300, 1000, 5000, 10 ** 6):
            seg = webdata.confidence_segments(n)
            self.assertGreaterEqual(seg, prev)
            self.assertLessEqual(seg, webdata.CONFIDENCE_SEGMENTS)
            prev = seg
        self.assertEqual(webdata.confidence_segments(10 ** 6), webdata.CONFIDENCE_SEGMENTS)

    def test_thousand_reviews_fills_the_gauge(self):
        self.assertEqual(webdata.confidence_segments(1000), 5)

    def test_handfuls_sit_in_the_middle(self):
        self.assertEqual(webdata.confidence_segments(10), 2)
        self.assertEqual(webdata.confidence_segments(60), 3)
        self.assertEqual(webdata.confidence_segments(300), 4)


class ExportCliRegistrationTestCase(unittest.TestCase):
    def test_export_subcommand_is_registered(self):
        import argparse

        from holiday_searcher.cli_ext import web as web_cli

        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers(dest="cmd")
        web_cli.register(sub)
        args = ap.parse_args(["export", "--out", "/tmp/x"])
        self.assertEqual(args.out, "/tmp/x")
        self.assertIs(args.func, web_cli.cmd_export)
        self.assertIs(ap.parse_args(["web"]).func, web_cli.cmd_web)


if __name__ == "__main__":
    unittest.main()

"""Eksport statyczny — `hs export`.

Zrzuca cały dashboard do katalogu z plikami `.html`, które działają bez
serwera: otwarte z dysku (`file://`) i wrzucone na GitHub Pages / dowolny
hosting statyczny. Konsekwencje, których pilnuje ten moduł:

* **zasoby zewnętrzne tylko z Google Fonts** — CSS jest w `<style>`, wykresy to
  inline SVG, cały JS jest wpisany w plik; nie ma CDN-ów ani obrazków. Jedyne
  ładowane zasoby zewnętrzne to `fonts.googleapis.com` i `fonts.gstatic.com`,
  a fallbacki systemowe są dobrane tak, żeby strona otwarta z dysku bez sieci
  wyglądała poprawnie. Pozostałe adresy `https://` to linki wychodzące
  (wakacje.pl, Google Maps, HolidayCheck) — czyli treść, nie zależność;
* **adresy relatywne z rozszerzeniem** — `offers.html`, `offer/<key>.html`;
  strony ofert leżą w podkatalogu, więc dostają prefiks `../`;
* **filtry po stronie klienta** — parametry URL nie mają statycznie sensu,
  więc `offers.html` zawiera pełną listę i sortuje/filtruje ją w czystym JS
  (jeden plik, pełna funkcjonalność, `<noscript>` wciąż pokazuje wszystko).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import components as C
from . import data as D
from . import pages
from .pages import Ctx
from .server import connect_readonly
from .urls import StaticUrls, safe_key

DEFAULT_OUT = "dist"


def _footer(n_offers: int, generated: str) -> str:
    return (
        f"<p>Wygenerowano: {generated} · {n_offers} "
        f"{C.offers_word(n_offers)} w migawce</p>"
        f"<p>holiday-searcher — statyczna migawka dashboardu, tylko do odczytu</p>"
    )


def _write(path: Path, html: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def export_site(db_path: Path | str, out_dir: Path | str = DEFAULT_OUT) -> list[Path]:
    """Generuje komplet stron. Zwraca listę zapisanych plików."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    conn = connect_readonly(Path(db_path))
    try:
        n_offers = conn.execute("SELECT COUNT(*) FROM offer").fetchone()[0]
        generated = datetime.now().strftime("%Y-%m-%d %H:%M")
        footer = _footer(n_offers, generated)
        has_calendar = D.table_has_rows(conn, "price_calendar")

        def ctx(prefix: str = "") -> Ctx:
            # Nowy Ctx na każdą stronę: render_* ustawia w nim `current`
            # i (dla /offers) wstrzykuje skrypt filtrowania.
            return Ctx(urls=StaticUrls(prefix), has_calendar=has_calendar, footer=footer)

        written: list[Path] = [
            _write(out / "index.html", pages.render_index(conn, ctx())),
            _write(out / "offers.html", pages.render_offers(conn, ctx=ctx())),
            _write(out / "hotels.html", pages.render_hotels(conn, ctx=ctx())),
            _write(out / "drops.html", pages.render_drops(conn, ctx())),
            _write(out / "kalendarz.html", pages.render_calendar(conn, ctx())),
        ]

        for row in conn.execute("SELECT key FROM offer ORDER BY key"):
            key = row[0]
            html = pages.render_offer_detail(conn, key, ctx("../"))
            if html is None:
                continue
            written.append(_write(out / "offer" / f"{safe_key(key)}.html", html))

        return written
    finally:
        conn.close()

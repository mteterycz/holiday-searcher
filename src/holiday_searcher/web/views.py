"""Zgodnościowa fasada nad rozbitym dashboardem.

Pierwotnie cały dashboard mieszkał w tym pliku (ponad 1000 linii: CSS, SQL,
komponenty i strony naraz). Po dołożeniu ocen zewnętrznych, weryfikacji ceny
i kalendarza został rozbity na moduły:

* `styles.py`        — tokeny CSS, arkusz, szkielet strony
* `urls.py`          — adresy: serwerowe vs. statyczne (eksport)
* `data.py`          — zapytania do bazy, oceny/werdykty/kalendarz
* `components.py`    — formatowanie, SVG, komponent oceny, karta oferty
* `pages.py`         — strony `/`, `/offers`, `/hotels`, `/drops`, `/kalendarz`, `/offer/<key>`
* `static_export.py` — `hs export`

Ten moduł nie ma własnej logiki — re-eksportuje publiczne nazwy, żeby stary
import (`from .web import views`) dalej działał.
"""
from __future__ import annotations

from .components import (  # noqa: F401
    fmt_diff,
    fmt_money,
    fmt_term,
    offer_card_html,
    rating_block_html,
    svg_price_chart,
    svg_sparkline,
)
from .data import build_offer_items, build_rating_summary, table_exists  # noqa: F401
from .pages import (  # noqa: F401
    Ctx,
    ctx_for,
    render_calendar,
    render_drops,
    render_error,
    render_hotels,
    render_index,
    render_offer_detail,
    render_offers,
)
from .styles import CSS, page  # noqa: F401

__all__ = [
    "CSS", "Ctx", "ctx_for", "page",
    "fmt_money", "fmt_diff", "fmt_term",
    "svg_price_chart", "svg_sparkline", "rating_block_html", "offer_card_html",
    "build_offer_items", "build_rating_summary", "table_exists",
    "render_index", "render_offers", "render_hotels", "render_drops",
    "render_calendar", "render_offer_detail", "render_error",
]

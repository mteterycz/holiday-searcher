"""Strony dashboardu. Każda funkcja `render_*` bierze połączenie SQLite (tylko
do odczytu) i `Ctx`, a zwraca gotowy HTML.

`Ctx` decyduje, w jakim trybie renderujemy: `Urls` (serwer, adresy z query
stringiem) albo `StaticUrls` (eksport, relatywne pliki `.html` + filtrowanie
w JS). Poza tym kod stron jest identyczny w obu trybach.

Żadna strona nie zakłada niepustej bazy ani obecności tabel opcjonalnych.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from . import components as C
from . import data as D
from .styles import page
from .urls import Urls

# --------------------------------------------------------------------------
# Kontekst renderowania
# --------------------------------------------------------------------------

@dataclass
class Ctx:
    urls: Urls = field(default_factory=Urls)
    has_calendar: bool = False
    current: str = ""
    footer: str = ""
    inline_js: str = ""


def ctx_for(conn: sqlite3.Connection, current: str = "", urls: Urls | None = None) -> Ctx:
    """Kontekst dla serwera — kalendarz w nawigacji tylko wtedy, gdy ma dane."""
    return Ctx(
        urls=urls or Urls(),
        has_calendar=D.table_has_rows(conn, "price_calendar"),
        current=current,
    )


def render_error(message: str, ctx: Ctx | None = None) -> str:
    ctx = ctx or Ctx()
    return page("Błąd", f'<h1>Błąd</h1><p class="lede">{escape(message)}</p>'
                        f'<p><a href="{escape(ctx.urls.index())}">← wróć na stronę główną</a></p>', ctx)


# --------------------------------------------------------------------------
# Wspólne fragmenty
# --------------------------------------------------------------------------

def _sorter(ctx: Ctx, labels: dict[str, str], active: str, href) -> str:
    """Pasek sortowania: linki na serwerze, przyciski JS w eksporcie."""
    if ctx.urls.static:
        return "".join(
            f'<button type="button" data-sort="{k}" aria-pressed="{"true" if k == active else "false"}">'
            f'{escape(v)}</button>'
            for k, v in labels.items()
        )
    return "".join(
        f'<a href="{escape(href(k))}" class="{"active" if k == active else ""}">{escape(v)}</a>'
        for k, v in labels.items()
    )


def _country_sections(
    ctx: Ctx, items: list[dict], summaries, ai_lookup, histories
) -> str:
    """Oferty pogrupowane po kraju — sekcja + licznik + lista kart."""
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(D.country_label(it["country"]), []).append(it)

    out = []
    for label, group in groups.items():
        cards = "".join(
            C.offer_card_html(
                it,
                urls=ctx.urls,
                rating=summaries.get(it["key"], D.RatingSummary()),
                verdict_row=ai_lookup.get(str(it["hotel_id"])) if it["hotel_id"] else None,
                history=histories.get(it["key"], []),
            )
            for it in group
        )
        out.append(
            f'<section class="country-section" data-country="{escape(label)}">'
            f'<div class="section-header"><h2>{escape(label)}</h2>'
            f'<span class="section-count">{len(group)} {C.offers_word(len(group))}</span></div>'
            f'<div class="offer-list">{cards}</div></section>'
        )
    return "".join(out)


# --------------------------------------------------------------------------
# /  — przegląd
# --------------------------------------------------------------------------

def render_index(conn: sqlite3.Connection, ctx: Ctx | None = None) -> str:
    ctx = ctx or Ctx()
    ctx.current = "index"

    n_offers = conn.execute("SELECT COUNT(*) FROM offer").fetchone()[0]
    n_snaps = conn.execute("SELECT COUNT(*) FROM price_snapshot").fetchone()[0]
    n_runs = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]

    cutoff = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
    n_changes = conn.execute("""
        WITH ordered AS (
            SELECT offer_key, ts, price,
                   LAG(price) OVER (PARTITION BY offer_key ORDER BY ts, id) AS prev_price
            FROM price_snapshot
        )
        SELECT COUNT(*) FROM ordered
        WHERE prev_price IS NOT NULL AND prev_price != price AND ts >= ?
    """, (cutoff,)).fetchone()[0]

    items = D.build_offer_items(conn) if n_offers else []
    summaries = D.rating_summaries(conn, items)

    cheapest = min((it["price"] for it in items if it["price"] is not None), default=None)
    n_confirmed = sum(1 for s in summaries.values() if s.confidence in ("high", "medium"))

    # --- kraje ---------------------------------------------------------
    country_rows = conn.execute(
        "SELECT country, COUNT(*) AS n FROM offer GROUP BY country ORDER BY n DESC"
    ).fetchall()
    chips = []
    for r in country_rows:
        label = D.country_label(r["country"])
        text = f'{escape(label)} · <span class="num">{r["n"]}</span>'
        if r["country"] and not ctx.urls.static:
            chips.append(f'<a class="chip count" href="{escape(ctx.urls.offers(country=r["country"]))}">{text}</a>')
        else:
            chips.append(f'<span class="chip count">{text}</span>')
    country_html = "".join(chips) or '<p class="muted">brak danych</p>'

    # --- TOP-y ---------------------------------------------------------
    def mini_card(it: dict, note: str = "") -> str:
        return f"""<a class="mini-card" href="{escape(ctx.urls.offer(it['key']))}">
  <div class="mc-title">{escape(it['hotel_name'] or '')}</div>
  <div class="mc-sub">{escape(D.country_label(it['country']))} · {escape(C.fmt_term(it['departure_date'], it['return_date']))} · {escape(C.nights_text(it['nights']))}</div>
  <div class="mc-price">{C.fmt_money(it['price'])}<span class="unit"> /os</span></div>
  {note}
</a>"""

    top_cheap = sorted((it for it in items if it["price"] is not None), key=lambda x: x["price"])[:5]
    cheap_html = "".join(mini_card(it) for it in top_cheap) or '<p class="empty-note">brak ofert</p>'

    top_drops = sorted(
        (x for x in items if x["n_snap"] >= 2 and x["drop_amount"] > 0),
        key=lambda x: (-x["drop_pct"], -x["drop_amount"]),
    )[:5]
    drops_html = "".join(
        mini_card(it, f'<div class="mc-note drop">▼ −{it["drop_pct"]:.0f}% ({C.fmt_money(it["drop_amount"])})</div>')
        for it in top_drops
    ) or '<p class="empty-note">Za mało historii cen na policzenie spadków.</p>'

    # TOP ocen: tylko oceny, którym można wierzyć — inaczej ranking wygrywa
    # hotel z jedną entuzjastyczną opinią.
    rated = [
        (it, summaries[it["key"]]) for it in items
        if summaries.get(it["key"]) and summaries[it["key"]].confidence in ("high", "medium")
    ]
    rated.sort(key=lambda pair: -pair[1].headline.value)
    seen: set[str] = set()
    top_rated = []
    for it, s in rated:
        hid = str(it["hotel_id"] or it["hotel_name"])
        if hid in seen:
            continue
        seen.add(hid)
        top_rated.append((it, s))
        if len(top_rated) == 5:
            break
    rated_html = "".join(
        mini_card(it, f'<div class="mc-note"><b>{s.headline.value:.1f}</b>/10 '
                      f'<span class="muted">· {C.fmt_int(s.headline.count)} {C.reviews_word(s.headline.count or 0)}</span></div>')
        for it, s in top_rated
    ) or '<p class="empty-note">Brak ocen z wystarczającą liczbą opinii. Uruchom <code>hs opinie</code>, żeby dociągnąć Google/HolidayCheck.</p>'

    # --- przebiegi i profile -------------------------------------------
    runs = conn.execute(
        "SELECT id, profile, ts, provider, found, note FROM run ORDER BY id DESC LIMIT 12"
    ).fetchall()
    run_rows = "".join(
        f'<tr><td class="num">{r["id"]}</td><td>{escape(r["ts"] or "")}</td>'
        f'<td>{escape(r["profile"] or "")}</td><td>{escape(r["provider"] or "")}</td>'
        f'<td class="num">{r["found"] if r["found"] is not None else "—"}</td>'
        f'<td>{escape(r["note"] or "")}</td></tr>'
        for r in runs
    )

    profile_rows = "".join(
        "<tr><td>{name}</td><td>{country}</td><td>{term}</td><td class='num'>{nights}</td><td>{boards}</td></tr>".format(
            name=escape(str(p.get("name", ""))),
            country=escape(str(p.get("country", ""))),
            term=f"{escape(str(p.get('date_from', '')))} – {escape(str(p.get('date_to', '')))}",
            nights=f"{p.get('nights_min', '?')}–{p.get('nights_max', '?')}",
            boards=escape("/".join(p.get("boards") or [])),
        )
        for p in D.load_profiles()
    )

    empty_note = "" if n_offers else (
        '<p class="empty-note">Baza jest pusta — uruchom <code>hs search</code> albo '
        '<code>hs monitor</code>, żeby zebrać oferty.</p>'
    )

    cal_link = (
        f' &nbsp;·&nbsp; <a href="{escape(ctx.urls.calendar())}">Kalendarz cen →</a>'
        if ctx.has_calendar else ""
    )

    body = f"""
<h1>Przegląd</h1>
<p class="lede">Co jest w bazie w tej chwili — najtańsze wyloty, największe obniżki
i hotele, których ocena ma pokrycie w liczbie opinii.</p>

<div class="stat-grid">
  <div class="stat lead"><div class="stat-value">{C.fmt_money(cheapest)}</div><div class="stat-label">najtaniej /os</div></div>
  <div class="stat"><div class="stat-value">{n_offers}</div><div class="stat-label">ofert</div></div>
  <div class="stat"><div class="stat-value">{n_confirmed}</div><div class="stat-label">ocen potwierdzonych</div></div>
  <div class="stat"><div class="stat-value">{n_changes}</div><div class="stat-label">zmian cen (48h)</div></div>
  <div class="stat"><div class="stat-value">{n_snaps}</div><div class="stat-label">snapshotów</div></div>
  <div class="stat"><div class="stat-value">{n_runs}</div><div class="stat-label">przebiegów</div></div>
</div>
{empty_note}

<h2>Kraje</h2>
<div class="chip-cloud">{country_html}</div>

<h2>TOP 5 najtańszych</h2>
<div class="mini-grid">{cheap_html}</div>

<h2>TOP 5 najlepiej ocenianych <span class="muted small">(min. 60 opinii)</span></h2>
<div class="mini-grid">{rated_html}</div>

<h2>TOP 5 największych spadków</h2>
<div class="mini-grid">{drops_html}</div>

<h2>Ostatnie przebiegi</h2>
<div class="table-wrap"><table>
  <thead><tr><th class="num">#</th><th>Czas</th><th>Profil</th><th>Dostawca</th><th class="num">Znaleziono</th><th>Notatka</th></tr></thead>
  <tbody>{run_rows or '<tr><td colspan="6" class="muted">brak przebiegów</td></tr>'}</tbody>
</table></div>

<h2>Profile wyszukiwania</h2>
<div class="table-wrap"><table>
  <thead><tr><th>Nazwa</th><th>Kraj</th><th>Termin</th><th class="num">Noce</th><th>Wyżywienie</th></tr></thead>
  <tbody>{profile_rows or '<tr><td colspan="5" class="muted">brak profili</td></tr>'}</tbody>
</table></div>

<p style="margin-top:var(--sp-6)">
  <a href="{escape(ctx.urls.offers())}">Wszystkie oferty →</a> &nbsp;·&nbsp;
  <a href="{escape(ctx.urls.hotels())}">Hotele →</a> &nbsp;·&nbsp;
  <a href="{escape(ctx.urls.drops())}">Największe obniżki →</a>{cal_link}
</p>
"""
    return page("Przegląd", body, ctx)


# --------------------------------------------------------------------------
# /offers
# --------------------------------------------------------------------------

SORT_LABELS = {"price": "Cena", "ppn": "zł/os/noc", "rating": "Ocena", "drop": "Spadek", "date": "Termin"}


def _offer_sort_key(it: dict, sort: str, summary: D.RatingSummary):
    if sort == "ppn":
        return (it["price_ppn"] if it["price_ppn"] is not None else float("inf"),)
    if sort == "rating":
        # Sortujemy po ocenie wiodącej (nie po samej lokalnej), a hotele bez
        # jakiejkolwiek oceny lądują na końcu.
        v = summary.headline.value if summary.has_value else None
        return (v is None, -(v or 0))
    if sort == "drop":
        return (-it["drop_pct"], -it["drop_amount"])
    if sort == "date":
        return (it["departure_date"] or "9999-99-99",)
    return (it["price"] if it["price"] is not None else float("inf"),)


OFFERS_JS = """
(function () {
  var sections = [].slice.call(document.querySelectorAll('.country-section'));
  var state = { sort: 'price', country: '', maxPrice: null, minRating: null };

  function num(el, attr) {
    var v = el.getAttribute(attr);
    if (v === null || v === '') return null;
    var f = parseFloat(v);
    return isNaN(f) ? null : f;
  }
  function word(n) {
    if (n === 1) return 'oferta';
    var l = n % 10, l2 = n % 100;
    return (l >= 2 && l <= 4 && !(l2 >= 12 && l2 <= 14)) ? 'oferty' : 'ofert';
  }
  function asc(a, b) {
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    return a - b;
  }
  function cmp(a, b) {
    if (state.sort === 'date') {
      var da = a.getAttribute('data-date') || '9999-99-99';
      var db = b.getAttribute('data-date') || '9999-99-99';
      return da < db ? -1 : (da > db ? 1 : 0);
    }
    // Ocena malejąco, ale hotele bez oceny na końcu -> negujemy i sortujemy rosnąco.
    if (state.sort === 'rating') return asc(negate(num(a, 'data-rating')), negate(num(b, 'data-rating')));
    if (state.sort === 'drop') return (num(b, 'data-drop') || 0) - (num(a, 'data-drop') || 0);
    return asc(num(a, state.sort === 'ppn' ? 'data-ppn' : 'data-price'),
               num(b, state.sort === 'ppn' ? 'data-ppn' : 'data-price'));
  }
  function negate(v) { return v === null ? null : -v; }

  function apply() {
    var total = 0;
    sections.forEach(function (sec) {
      var list = sec.querySelector('.offer-list');
      var cards = [].slice.call(list.querySelectorAll('.offer-card'));
      var shown = 0;
      cards.forEach(function (c) {
        var ok = true;
        if (state.country && c.getAttribute('data-country') !== state.country) ok = false;
        if (ok && state.maxPrice !== null) {
          var p = num(c, 'data-price');
          if (p === null || p > state.maxPrice) ok = false;
        }
        if (ok && state.minRating !== null) {
          var r = num(c, 'data-rating');
          if (r === null || r < state.minRating) ok = false;
        }
        c.hidden = !ok;
        if (ok) shown++;
      });
      cards.sort(cmp).forEach(function (c) { list.appendChild(c); });
      sec.hidden = shown === 0;
      var cnt = sec.querySelector('.section-count');
      if (cnt) cnt.textContent = shown + ' ' + word(shown);
      total += shown;
    });
    var t = document.getElementById('offers-total');
    if (t) t.textContent = total;
    var e = document.getElementById('offers-empty');
    if (e) e.hidden = total > 0;
  }

  [].slice.call(document.querySelectorAll('[data-sort]')).forEach(function (b) {
    b.addEventListener('click', function () {
      state.sort = b.getAttribute('data-sort');
      [].slice.call(document.querySelectorAll('[data-sort]')).forEach(function (o) {
        o.setAttribute('aria-pressed', o === b ? 'true' : 'false');
      });
      apply();
    });
  });
  function bind(id, fn) {
    var el = document.getElementById(id);
    if (el) { el.addEventListener('input', fn); el.addEventListener('change', fn); }
    return el;
  }
  var fc = bind('f-country', function () { state.country = fc.value; apply(); });
  var fp = bind('f-price', function () { state.maxPrice = fp.value === '' ? null : parseFloat(fp.value); apply(); });
  var fr = bind('f-rating', function () { state.minRating = fr.value === '' ? null : parseFloat(fr.value); apply(); });
  var clear = document.getElementById('f-clear');
  if (clear) clear.addEventListener('click', function () {
    if (fc) fc.value = ''; if (fp) fp.value = ''; if (fr) fr.value = '';
    state.country = ''; state.maxPrice = null; state.minRating = null;
    apply();
  });
  apply();
})();
"""


def render_offers(
    conn: sqlite3.Connection,
    sort: str = "price",
    country: str | None = None,
    max_price: int | None = None,
    min_rating: float | None = None,
    ctx: Ctx | None = None,
) -> str:
    ctx = ctx or Ctx()
    ctx.current = "offers"
    sort = sort if sort in SORT_LABELS else "price"

    items = D.build_offer_items(conn)
    summaries = D.rating_summaries(conn, items)
    countries_all = sorted({it["country"] for it in items if it["country"]})

    # W eksporcie statycznym filtry robi JS — server-side filtrujemy tylko
    # wtedy, gdy adres naprawdę je niesie.
    if not ctx.urls.static:
        if country:
            items = [it for it in items if it["country"] == country]
        if max_price is not None:
            items = [it for it in items if it["price"] is not None and it["price"] <= max_price]
        if min_rating is not None:
            items = [
                it for it in items
                if summaries.get(it["key"], D.RatingSummary()).has_value
                and summaries[it["key"]].headline.value >= min_rating
            ]

    items.sort(key=lambda it: (
        D.country_label(it["country"]),
        _offer_sort_key(it, sort, summaries.get(it["key"], D.RatingSummary())),
    ))

    ai_lookup = D.load_ai_lookup(conn)
    histories = D.price_histories(conn)
    sections = _country_sections(ctx, items, summaries, ai_lookup, histories)

    sorter = _sorter(ctx, SORT_LABELS, sort,
                     lambda k: ctx.urls.offers(sort=k, country=country,
                                               max_price=max_price, min_rating=min_rating))

    country_options = "".join(
        f'<option value="{escape(c)}"{" selected" if c == country else ""}>{escape(c)}</option>'
        for c in countries_all
    )

    if ctx.urls.static:
        ctx.inline_js = OFFERS_JS
        controls_open, controls_close = "<div class=\"toolbar\">", "</div>"
        clear_btn = '<button type="button" class="btn ghost" id="f-clear">Wyczyść</button>'
        submit = ""
        noscript = ('<noscript><p class="muted small">Filtrowanie i sortowanie wymaga JavaScriptu — '
                    'bez niego widać pełną listę ofert.</p></noscript>')
    else:
        controls_open = '<form class="toolbar" method="get" action="' + escape(ctx.urls.offers()) + '">'
        controls_open += f'<input type="hidden" name="sort" value="{escape(sort)}">'
        controls_close = "</form>"
        clear_btn = (f'<a class="btn ghost" href="{escape(ctx.urls.offers())}">Wyczyść</a>'
                     if (country or max_price is not None or min_rating is not None) else "")
        submit = '<button type="submit" class="btn">Filtruj</button>'
        noscript = ""

    toolbar = f"""{controls_open}
  <div class="toolbar-row">
    <span class="toolbar-label">Sortuj</span>
    <div class="segmented">{sorter}</div>
  </div>
  <div class="toolbar-row">
    <div class="field"><label for="f-country">Kraj</label>
      <select id="f-country" name="country"><option value="">Wszystkie</option>{country_options}</select></div>
    <div class="field"><label for="f-price">Cena maks. (zł/os)</label>
      <input id="f-price" type="number" name="max_price" min="0" step="50" inputmode="numeric"
             value="{max_price if max_price is not None else ''}"></div>
    <div class="field"><label for="f-rating">Ocena min. (0–10)</label>
      <input id="f-rating" type="number" name="min_rating" min="0" max="10" step="0.1" inputmode="decimal"
             value="{min_rating if min_rating is not None else ''}"></div>
    {submit}{clear_btn}
  </div>
{controls_close}"""

    empty = ('<p class="empty-note" id="offers-empty"'
             + (' hidden' if items else '')
             + '>Brak ofert spełniających kryteria.</p>')

    body = f"""
<h1>Oferty <span class="h1-count">(<span id="offers-total">{len(items)}</span>)</span></h1>
<p class="lede">Ceny za osobę. Ocena obok każdej oferty pokazuje też, ile opinii za nią stoi.</p>
{toolbar}
{noscript}
{sections}
{empty}
"""
    return page("Oferty", body, ctx)


# --------------------------------------------------------------------------
# /hotels
# --------------------------------------------------------------------------

HOTEL_SORT_LABELS = {"price": "Cena", "rating": "Ocena", "variants": "Warianty"}


def render_hotels(conn: sqlite3.Connection, sort: str = "price", ctx: Ctx | None = None) -> str:
    ctx = ctx or Ctx()
    ctx.current = "hotels"
    sort = sort if sort in HOTEL_SORT_LABELS else "price"

    items = D.build_offer_items(conn)
    summaries = D.rating_summaries(conn, items)
    ai_lookup = D.load_ai_lookup(conn)

    groups: dict[str, list[dict]] = {}
    for it in items:
        gid = str(it["hotel_id"]) if it["hotel_id"] else f"name:{it['hotel_name']}"
        groups.setdefault(gid, []).append(it)

    hotels = []
    for variants in groups.values():
        best = min(variants, key=lambda x: (x["price"] if x["price"] is not None else float("inf")))
        prices = [v["price"] for v in variants if v["price"] is not None]
        hotels.append({
            "best": best, "variant_count": len(variants),
            "min_price": min(prices) if prices else None,
            "max_price": max(prices) if prices else None,
        })

    def sort_key(h: dict):
        if sort == "rating":
            s = summaries.get(h["best"]["key"], D.RatingSummary())
            v = s.headline.value if s.has_value else None
            return (v is None, -(v or 0))
        if sort == "variants":
            return (-h["variant_count"],)
        return (h["min_price"] if h["min_price"] is not None else float("inf"),)

    hotels.sort(key=lambda h: (D.country_label(h["best"]["country"]), sort_key(h)))

    def hotel_card(h: dict) -> str:
        b = h["best"]
        n = h["variant_count"]
        variant_word = C.plural_pl(n, "wariant", "warianty", "wariantów")
        range_txt = ""
        if h["min_price"] is not None and h["max_price"] is not None and h["min_price"] != h["max_price"]:
            range_txt = f' · ceny od {C.fmt_money(h["min_price"])} do {C.fmt_money(h["max_price"])}'
        loc = " / ".join(x for x in (b["region"], b["city"]) if x)
        rating = summaries.get(b["key"], D.RatingSummary())
        return f"""<article class="offer-card">
  <div class="offer-main">
    <h3 class="offer-hotel"><a href="{escape(ctx.urls.offer(b['key']))}">{escape(b['hotel_name'] or '')}</a> {C.stars_html(b['stars'])}</h3>
    <div class="offer-loc">{escape(loc)}</div>
    <div class="offer-term">{escape(C.fmt_term(b['departure_date'], b['return_date']))}<span class="nights">{escape(C.nights_text(b['nights']))}</span></div>
    {C.rating_block_html(rating)}
    <div class="chip-row">
      {f'<span class="chip">{escape(b["board_raw"])}</span>' if b['board_raw'] else ''}
      {f'<span class="chip">{escape(b["tour_operator"])}</span>' if b['tour_operator'] else ''}
    </div>
    {C.ai_summary_html(ai_lookup.get(str(b['hotel_id'])) if b['hotel_id'] else None)}
    <div class="offer-variants">{n} {variant_word} w bazie{range_txt}</div>
  </div>
  <div class="offer-price">
    <div class="price-main">{C.fmt_money(b['price'])}<span class="unit"> /os</span></div>
    <div class="price-sub">najtańszy wariant</div>
    <a class="btn price-cta" href="{escape(ctx.urls.offer(b['key']))}">Szczegóły →</a>
  </div>
</article>"""

    grouped: dict[str, list[dict]] = {}
    for h in hotels:
        grouped.setdefault(D.country_label(h["best"]["country"]), []).append(h)
    sections = "".join(
        f'<section class="country-section"><div class="section-header"><h2>{escape(label)}</h2>'
        f'<span class="section-count">{len(group)} {C.plural_pl(len(group), "hotel", "hotele", "hoteli")}</span></div>'
        f'<div class="offer-list">{"".join(hotel_card(h) for h in group)}</div></section>'
        for label, group in grouped.items()
    )

    sorter = _sorter(ctx, HOTEL_SORT_LABELS, sort, lambda k: ctx.urls.hotels(sort=k))
    # Na stronie hoteli sortowanie w eksporcie robimy tak samo jak na serwerze:
    # sekcje są już posortowane, a JS listy ofert tu nie ma — pokazujemy więc
    # statyczny wybór tylko wtedy, gdy adresy działają.
    toolbar = (
        f'<div class="toolbar"><div class="toolbar-row"><span class="toolbar-label">Sortuj</span>'
        f'<div class="segmented">{sorter}</div></div></div>'
        if not ctx.urls.static else ""
    )

    body = f"""
<h1>Hotele <span class="h1-count">({len(hotels)})</span></h1>
<p class="lede">Każdy hotel raz, w swoim najtańszym wariancie. Ofert bywa więcej —
ten sam hotel wraca w różnych terminach, pokojach i wyżywieniu.</p>
{toolbar}
{sections or '<p class="empty-note">Brak hoteli w bazie.</p>'}
"""
    return page("Hotele", body, ctx)


# --------------------------------------------------------------------------
# /drops
# --------------------------------------------------------------------------

def render_drops(conn: sqlite3.Connection, ctx: Ctx | None = None) -> str:
    ctx = ctx or Ctx()
    ctx.current = "drops"

    items = [it for it in D.build_offer_items(conn) if it["n_snap"] >= 2]
    items.sort(key=lambda x: (-x["drop_pct"], -x["drop_amount"]))
    histories = D.price_histories(conn)

    rows = []
    for it in items:
        cls = "drop" if it["drop_amount"] > 0 else ("rise" if it["drop_amount"] < 0 else "muted")
        hist = histories.get(it["key"], [])
        loc = " / ".join(x for x in (D.country_label(it["country"]), it["region"], it["city"]) if x)
        rows.append(
            "<tr>"
            f'<td><a href="{escape(ctx.urls.offer(it["key"]))}">{escape(it["hotel_name"] or "")}</a></td>'
            f'<td class="muted">{escape(loc)}</td>'
            f'<td>{escape(C.fmt_term(it["departure_date"], it["return_date"]))}</td>'
            f'<td class="num muted">{C.fmt_money(it["max_price"])}</td>'
            f'<td class="num"><strong>{C.fmt_money(it["price"])}</strong></td>'
            f'<td class="num {cls}">{C.fmt_money(it["drop_amount"])}</td>'
            f'<td class="num {cls}">{it["drop_pct"]:.1f}%</td>'
            f'<td>{C.svg_sparkline(hist) if len(hist) >= 3 else ""}</td>'
            "</tr>"
        )

    note = "" if items else (
        '<p class="empty-note">Za mało historii cen — poczekaj na kolejne przebiegi monitora.</p>'
    )

    body = f"""
<h1>Największe obniżki</h1>
<p class="lede">Ostatnia cena zestawiona z maksimum zaobserwowanym w historii snapshotów tej samej oferty.</p>
{note}
<div class="table-wrap"><table>
  <thead><tr>
    <th>Hotel</th><th>Lokalizacja</th><th>Termin</th><th class="num">Było (max)</th>
    <th class="num">Teraz</th><th class="num">Spadek</th><th class="num">%</th><th>Trend</th>
  </tr></thead>
  <tbody>{''.join(rows) or '<tr><td colspan="8" class="muted">brak danych</td></tr>'}</tbody>
</table></div>
"""
    return page("Obniżki", body, ctx)


# --------------------------------------------------------------------------
# /kalendarz
# --------------------------------------------------------------------------

_WEEKDAYS = ("pon", "wt", "śr", "czw", "pt", "sob", "niedz")


def _weekday(iso: str) -> str:
    try:
        return _WEEKDAYS[datetime.fromisoformat(str(iso)[:10]).weekday()]
    except (ValueError, IndexError):
        return ""


def render_calendar(conn: sqlite3.Connection, ctx: Ctx | None = None) -> str:
    ctx = ctx or Ctx()
    ctx.current = "calendar"
    grids = D.load_calendars(conn)

    blocks = []
    for g in grids:
        ppns = [c["price_ppn"] for c in g["cells"].values() if c["price_ppn"] is not None]
        lo, hi = (min(ppns), max(ppns)) if ppns else (0.0, 1.0)
        span = (hi - lo) or 1.0

        head = "".join(f'<th class="num">{n} {C.nights_word(n)}</th>' for n in g["nights"])
        body_rows = []
        for d in g["dates"]:
            cells = []
            for n in g["nights"]:
                cell = g["cells"].get((d, n))
                if cell is None:
                    cells.append('<td class="empty">—</td>')
                    continue
                heat = (hi - (cell["price_ppn"] or hi)) / span   # taniej = mocniej
                best = " best" if (d, n) == g["best_key"] else ""
                cells.append(
                    f'<td class="cell{best}" style="--heat:{heat:.2f}">'
                    f'<span class="heat">{C.fmt_money(cell["price_pp"])}<br>'
                    f'<span class="muted small">{cell["price_ppn"]:.0f} zł/noc</span></span></td>'
                )
            body_rows.append(
                f'<tr><th class="rowhead">{escape(str(d))} '
                f'<span class="muted small">{_weekday(d)}</span></th>{"".join(cells)}</tr>'
            )

        best_cell = g["cells"][g["best_key"]]
        span_txt = (
            f"{escape(str(g['oldest'])[:16])} – {escape(str(g['checked_at'])[:16])}"
            if g.get("oldest") and g["oldest"][:10] != g["checked_at"][:10]
            else escape(str(g["checked_at"] or "")[:16])
        )
        blocks.append(f"""
<h2>{escape(g['profile'] or 'profil bez nazwy')}</h2>
<p class="page-sub">Sprawdzone: {span_txt} ·
  minimum <strong>{C.fmt_money(best_cell['price_pp'])}</strong>
  ({best_cell['price_ppn']:.0f} zł/os/noc) — wylot {escape(str(g['best_key'][0]))},
  {g['best_key'][1]} {C.nights_word(g['best_key'][1])}</p>
<div class="cal-wrap"><table class="cal">
  <thead><tr><th class="rowhead">Wylot</th>{head}</tr></thead>
  <tbody>{''.join(body_rows)}</tbody>
</table></div>
<div class="cal-legend"><span class="swatch"></span> drożej → taniej ·
  <span class="drop">obramowanie</span> = najtaniej w przeliczeniu na dobę</div>
""")

    if not blocks:
        blocks = ['<p class="empty-note">Brak danych kalendarza. Uruchom '
                  '<code>hs kalendarz &lt;profil&gt;</code>, żeby zbudować siatkę.</p>']

    body = f"""
<h1>Kalendarz cen</h1>
<p class="lede">Ile kosztuje ten sam wyjazd przy innej dacie wylotu i innej długości pobytu.
Porównywalne są tylko ceny za dobę — kolumny mają różną liczbę nocy.</p>
{''.join(blocks)}
"""
    return page("Kalendarz cen", body, ctx)


# --------------------------------------------------------------------------
# /offer/<key>
# --------------------------------------------------------------------------

def _verification_html(v: dict) -> str:
    details = v["details"] or {}
    verdict = str(details.get("verdict") or "").strip()
    diff = v["diff_pct"]
    ok = verdict == "zgodna" or (diff is not None and abs(diff) < 1.0)
    badge_cls = "ok" if ok else "warn"
    badge_txt = verdict or ("zgodna" if ok else "odchylenie")

    diff_txt = "—" if diff is None else f'{"+" if diff > 0 else ""}{diff:.1f}%'
    note = str(details.get("note") or "").strip()

    variants = details.get("variants") or []
    var_rows = "".join(
        f'<tr><td>{escape(str(x.get("room_desc") or ""))}</td>'
        f'<td class="num">{C.fmt_money(x.get("price_pp"))}</td>'
        f'<td class="muted">{escape(", ".join(str(f) for f in (x.get("features") or [])))}</td></tr>'
        for x in variants if isinstance(x, dict)
    )
    var_html = (
        f'<div class="table-wrap"><table><thead><tr><th>Pokój</th><th class="num">Cena /os</th>'
        f'<th>W cenie</th></tr></thead><tbody>{var_rows}</tbody></table></div>'
        if var_rows else ""
    )

    return f"""<h2>Weryfikacja ceny</h2>
<div class="verify">
  <span class="verify-badge {badge_cls}">{escape(badge_txt)}</span>
  <div class="verify-item"><span class="k">z listingu</span>
    <span class="verify-num">{C.fmt_money(v['listing_price'])}</span></div>
  <div class="verify-item"><span class="k">po wejściu w ofertę</span>
    <span class="verify-num">{C.fmt_money(v['final_price'])}</span></div>
  <div class="verify-item"><span class="k">różnica</span>
    <span class="verify-num {'rise' if (diff or 0) > 0 else ('drop' if (diff or 0) < 0 else '')}">{escape(diff_txt)}</span></div>
  <div class="verify-item"><span class="k">sprawdzone</span>
    <span class="small">{escape(str(v['checked_at'] or ''))}</span></div>
  {f'<p class="verify-note">{escape(note)}</p>' if note else ''}
</div>
{var_html}"""


def render_offer_detail(conn: sqlite3.Connection, key: str, ctx: Ctx | None = None) -> str | None:
    ctx = ctx or Ctx()
    ctx.current = "offers"

    o = conn.execute("SELECT * FROM offer WHERE key=?", (key,)).fetchone()
    if o is None:
        return None

    history = conn.execute(
        "SELECT ts, price, price_ppn, run_id FROM price_snapshot WHERE offer_key=? ORDER BY ts, id",
        (key,),
    ).fetchall()
    hist_pairs = [(h["ts"], h["price"]) for h in history]
    prices = [h["price"] for h in history]
    latest_price = prices[-1] if prices else None

    snap_rows = []
    prev = None
    for h in history:
        diff = "" if prev is None else C.fmt_diff(h["price"] - prev)
        snap_rows.append(
            f'<tr><td>{escape(str(h["ts"]))}</td>'
            f'<td class="num">{C.fmt_money(h["price"])}</td>'
            f'<td class="num">{h["price_ppn"]:.0f}</td>'
            f'<td class="num">{diff}</td>'
            f'<td class="num muted">{h["run_id"] if h["run_id"] is not None else "—"}</td></tr>'
        )
        prev = h["price"]

    ext = D.load_external_ratings(conn)
    summary = D.build_rating_summary(
        o["rating"], o["rating_count"],
        ext.get(str(o["hotel_id"])) if o["hotel_id"] else None,
    )

    verdict_row = D.load_verdict_for_hotel(conn, o["hotel_id"])
    verdict_html = f'<h2>Ocena AI z opinii gości</h2>{C.ai_verdict_full_html(verdict_row)}' if verdict_row is not None else ""

    verification = D.load_verification(conn, key)
    verify_html = _verification_html(verification) if verification else ""

    term = C.fmt_term(o["departure_date"], o["return_date"])
    nights_txt = C.nights_text(o["nights"])
    loc = " · ".join(x for x in (D.country_label(o["country"]), o["region"], o["city"]) if x)

    body = f"""
<a class="backlink" href="{escape(ctx.urls.offers())}">← wszystkie oferty</a>
<div class="detail-head">
  <h1>{escape(o['hotel_name'] or '')} {C.stars_html(o['stars'])}</h1>
  <p class="page-sub">{escape(loc)}</p>

  <div class="stat-grid">
    <div class="stat lead"><div class="stat-value">{C.fmt_money(latest_price)}</div><div class="stat-label">cena /os</div></div>
    <div class="stat lead"><div class="stat-value term">{escape(term)}</div><div class="stat-label">termin</div></div>
    <div class="stat"><div class="stat-value">{escape(nights_txt or '—')}</div><div class="stat-label">długość pobytu</div></div>
    <div class="stat"><div class="stat-value">{C.fmt_money(min(prices) if prices else None)}</div><div class="stat-label">minimum w historii</div></div>
    <div class="stat"><div class="stat-value">{C.fmt_money(max(prices) if prices else None)}</div><div class="stat-label">maksimum w historii</div></div>
    <div class="stat"><div class="stat-value">{len(history)}</div><div class="stat-label">snapshotów</div></div>
  </div>

  {C.rating_block_html(summary, big=True)}

  <p style="margin-top:var(--sp-4)">
    <a class="btn" href="{escape(o['url'] or '#')}" target="_blank" rel="noopener">Otwórz na wakacje.pl →</a>
  </p>
</div>

<h2>Historia ceny</h2>
{C.svg_price_chart(hist_pairs)}
<div class="table-wrap" style="margin-top:var(--sp-3)"><table>
  <thead><tr><th>Data</th><th class="num">Cena</th><th class="num">zł/os/noc</th><th class="num">Zmiana</th><th class="num">Run</th></tr></thead>
  <tbody>{''.join(snap_rows) or '<tr><td colspan="5" class="muted">brak snapshotów</td></tr>'}</tbody>
</table></div>

{verify_html}

{verdict_html}

<h2>Szczegóły</h2>
<div class="table-wrap"><table class="kv">
  <tr><th>Termin</th><td>{escape(str(o['departure_date'] or ''))} – {escape(str(o['return_date'] or ''))} ({escape(nights_txt)})</td></tr>
  <tr><th>Wylot</th><td>{escape(str(o['departure_place'] or ''))} ({escape(str(o['departure_code'] or ''))})</td></tr>
  <tr><th>Pokój</th><td>{escape(str(o['room_type'] or ''))}</td></tr>
  <tr><th>Wyżywienie</th><td>{escape(str(o['board_raw'] or o['board'] or ''))}</td></tr>
  <tr><th>Biuro podróży</th><td>{escape(str(o['tour_operator'] or ''))}</td></tr>
  <tr><th>Dostawca</th><td>{escape(str(o['provider'] or ''))}</td></tr>
  <tr><th>Link</th><td><a href="{escape(o['url'] or '#')}" target="_blank" rel="noopener">{escape(str(o['url'] or ''))}</a></td></tr>
  <tr><th>Pierwszy raz widziane</th><td>{escape(str(o['first_seen'] or ''))}</td></tr>
  <tr><th>Ostatnio widziane</th><td>{escape(str(o['last_seen'] or ''))}</td></tr>
</table></div>
"""
    return page(o["hotel_name"] or "Oferta", body, ctx)

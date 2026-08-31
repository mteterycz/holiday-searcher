"""Kawałki HTML wielokrotnego użytku: formatowanie, wykresy SVG, komponent
oceny, karta oferty, ostrzeżenia AI.

Wszystko jest czystą funkcją `dane -> string HTML`, bez dostępu do bazy — dzięki
temu ten sam kod obsługuje serwer i eksport statyczny, a testy mogą sprawdzać
pojedyncze komponenty bez stawiania serwera.
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from . import data as D
from .data import RatingSummary

# --------------------------------------------------------------------------
# Formatowanie
# --------------------------------------------------------------------------

def fmt_money(n) -> str:
    if n is None:
        return "—"
    return f"{n:,.0f}".replace(",", " ") + " zł"


def fmt_int(n) -> str:
    if n is None:
        return "—"
    return f"{n:,.0f}".replace(",", " ")


def fmt_diff(diff: int) -> str:
    """Zmiana ceny: spadek na zielono (dobra wiadomość), wzrost na czerwono."""
    sign = "+" if diff > 0 else ""
    text = f"{sign}{diff:,}".replace(",", " ") + " zł"
    if diff < 0:
        return f'<span class="drop">▼ {text}</span>'
    if diff > 0:
        return f'<span class="rise">▲ {text}</span>'
    return '<span class="muted">— 0 zł</span>'


def nights_word(n: int | None) -> str:
    if n is None:
        return "nocy"
    if n == 1:
        return "noc"
    last, last2 = n % 10, n % 100
    if 2 <= last <= 4 and not (12 <= last2 <= 14):
        return "noce"
    return "nocy"


def plural_pl(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    last, last2 = n % 10, n % 100
    if 2 <= last <= 4 and not (12 <= last2 <= 14):
        return few
    return many


def offers_word(n: int) -> str:
    return plural_pl(n, "oferta", "oferty", "ofert")


def reviews_word(n: int) -> str:
    return plural_pl(n, "opinia", "opinie", "opinii")


def short_date(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        d = datetime.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    return f"{d.day:02d}.{d.month:02d}"


def fmt_term(dep: str | None, ret: str | None) -> str:
    return f"{short_date(dep)}–{short_date(ret)}"


def stars_html(stars) -> str:
    if not stars:
        return ""
    n = max(0, int(round(stars)))
    return f'<span class="stars" title="{n} gwiazdki">{"★" * n}</span>' if n else ""


def nights_text(nights: int | None) -> str:
    return f"{nights} {nights_word(nights)}" if nights is not None else ""


# --------------------------------------------------------------------------
# Wykresy SVG (inline, bez żadnej biblioteki)
# --------------------------------------------------------------------------

def svg_price_chart(history: list[tuple[str, int]], width: int = 780, height: int = 240) -> str:
    """Historia ceny jako wykres liniowy. `history` chronologicznie."""
    if not history:
        return '<p class="empty-note">Brak historii cen — dopiero pierwszy snapshot.</p>'

    pad_l, pad_r, pad_t, pad_b = 72, 20, 18, 32
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    prices = [p for _, p in history]
    lo, hi = min(prices), max(prices)
    flat = hi == lo
    span = (hi - lo) or 1
    n = len(history)

    def x_for(i: int) -> float:
        return pad_l + (plot_w / 2 if n == 1 else plot_w * i / (n - 1))

    def y_for(price: int) -> float:
        # Cena, która nigdy się nie zmieniła, rysuje się w połowie wysokości —
        # przyklejona do dolnej krawędzi wyglądałaby jak spadek do zera.
        if flat:
            return pad_t + plot_h / 2
        return pad_t + plot_h - (price - lo) / span * plot_h

    pts = [(x_for(i), y_for(p)) for i, (_, p) in enumerate(history)]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(pts))
    area = (
        f'<path d="{line} L{pts[-1][0]:.1f} {height - pad_b} L{pts[0][0]:.1f} {height - pad_b} Z" '
        f'class="chart-area"/>' if n > 1 else ""
    )

    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" class="chart-pt">'
        f'<title>{escape(str(ts))} — {fmt_money(price)}</title></circle>'
        for (x, y), (ts, price) in zip(pts, history)
    )

    grid = []
    for tick in sorted({lo, (lo + hi) // 2, hi}):
        y_px = y_for(tick)
        grid.append(
            f'<line x1="{pad_l}" y1="{y_px:.1f}" x2="{width - pad_r}" y2="{y_px:.1f}" class="chart-guide"/>'
            f'<text x="{pad_l - 8:.1f}" y="{y_px + 4:.1f}" text-anchor="end" class="chart-axis">'
            f'{fmt_money(tick)}</text>'
        )

    label_idxs = {0, n - 1} | ({n // 2} if n >= 3 else set())
    x_labels = "".join(
        f'<text x="{x_for(i):.1f}" y="{height - pad_b + 18:.1f}" '
        f'text-anchor="{"start" if i == 0 else ("end" if i == n - 1 else "middle")}" '
        f'class="chart-axis">{escape(str(history[i][0])[:10])}</text>'
        for i in sorted(label_idxs)
    )

    axes = (
        f'<line x1="{pad_l}" y1="{height - pad_b}" x2="{width - pad_r}" y2="{height - pad_b}" class="chart-axis-line"/>'
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{height - pad_b}" class="chart-axis-line"/>'
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Wykres historii ceny" class="price-chart">'
        f'{"".join(grid)}{axes}{area}<path d="{line}" class="chart-line" fill="none" '
        f'stroke-linejoin="round" stroke-linecap="round"/>{dots}{x_labels}</svg>'
    )


def svg_sparkline(history: list[tuple[str, int]], width: int = 84, height: int = 24) -> str:
    """Mini-trend ceny. Pusty string dla mniej niż 2 punktów."""
    if len(history) < 2:
        return ""
    prices = [p for _, p in history]
    lo, hi = min(prices), max(prices)
    flat = hi == lo
    span = (hi - lo) or 1
    n = len(prices)

    def x(i: int) -> float:
        return 2 + (width - 4) * i / (n - 1)

    def y(p: int) -> float:
        if flat:
            return height / 2
        return height - 3 - (p - lo) / span * (height - 6)

    pts = [(x(i), y(p)) for i, p in enumerate(prices)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{px:.1f} {py:.1f}" for i, (px, py) in enumerate(pts))

    trend = prices[-1] - prices[0]
    kind = "good" if trend < 0 else ("bad" if trend > 0 else "flat")
    lx, ly = pts[-1]
    tip = f"{fmt_money(prices[0])} → {fmt_money(prices[-1])}"
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" class="sparkline" '
        f'role="img" aria-label="Trend ceny: {escape(tip)}"><title>{escape(tip)}</title>'
        f'<path d="{path}" fill="none" class="spark-{kind}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.2" class="spark-{kind}-dot"/></svg>'
    )


# --------------------------------------------------------------------------
# OCENA + WIARYGODNOŚĆ
#
# Sedno aplikacji: „10.0" i „8.6" to dwie różne liczby, ale „10.0 z 1 opinii"
# i „8.6 z 1425 opinii" to dwie różne KLASY informacji. Komponent pokazuje
# jedno obok drugiego i degraduje wizualnie ocenę, której nie ma co ufać.
# --------------------------------------------------------------------------

def rating_block_html(summary: RatingSummary, *, big: bool = False) -> str:
    cls_big = " big" if big else ""

    if not summary.has_value:
        return (
            f'<div class="rating conf-none{cls_big}">'
            f'<span class="rating-value">—</span>'
            f'<div class="rating-body"><span class="rating-count muted">'
            f'{escape(D.CONFIDENCE_LABELS["none"])}</span></div></div>'
        )

    head = summary.headline
    assert head is not None
    count = head.count or 0
    frac = D.confidence_fraction(head.count)
    conf_label = D.CONFIDENCE_LABELS[summary.confidence]

    if head.count is None:
        count_txt = "liczba opinii nieznana"
    elif summary.confidence == "thin":
        count_txt = f"tylko {count} {reviews_word(count)}"
    else:
        count_txt = f"{fmt_int(count)} {reviews_word(count)}"

    pills = ""
    if len(summary.sources) > 1:
        parts = []
        for s in summary.sources:
            n = f" · {fmt_int(s.count)}" if s.count is not None else ""
            inner = f"{escape(s.label)} <b>{s.value:.1f}</b>{n}"
            title = f"{s.label}: {s.value:.1f}/10" + (
                f", {fmt_int(s.count)} {reviews_word(s.count or 0)}" if s.count is not None else ""
            )
            if s.url:
                parts.append(
                    f'<a class="src-pill" href="{escape(s.url)}" target="_blank" '
                    f'rel="noopener" title="{escape(title)}">{inner}</a>'
                )
            else:
                parts.append(f'<span class="src-pill" title="{escape(title)}">{inner}</span>')
        pills = f'<div class="rating-sources">{"".join(parts)}</div>'

    disagree = ""
    if summary.spread >= D.DISAGREE_THRESHOLD:
        lo = min(summary.sources, key=lambda s: s.value)
        hi = max(summary.sources, key=lambda s: s.value)
        disagree = (
            f'<div class="rating-disagree">Źródła się rozjeżdżają: '
            f'{hi.value:.1f} ({escape(hi.label)}) vs {lo.value:.1f} ({escape(lo.label)})</div>'
        )

    return f"""<div class="rating conf-{summary.confidence}{cls_big}">
  <div><span class="rating-value">{head.value:.1f}</span><span class="rating-scale">/10</span></div>
  <div class="rating-body">
    <span class="rating-count">{escape(count_txt)} <span class="src">· {escape(head.label)}</span></span>
    <span class="rating-conf"><span class="conf-bar" aria-hidden="true"><i style="width:{frac * 100:.0f}%"></i></span>
      {escape(conf_label)}</span>
  </div>
  {pills}
  {disagree}
</div>"""


# --------------------------------------------------------------------------
# Ostrzeżenia z werdyktu AI
# --------------------------------------------------------------------------

def ai_summary_html(verdict_row) -> str:
    """Skrót werdyktu na kartę: jednozdaniowa ocena + znacznik zastrzeżeń."""
    if verdict_row is None:
        return ""
    data = D.parse_verdict(verdict_row)
    if not data:
        return ""

    out = ""
    one_liner = str(data.get("one_liner") or "").strip()
    if one_liner:
        short = one_liner if len(one_liner) <= 140 else one_liner[:137] + "…"
        out += f'<p class="ai-oneliner">{escape(short)}</p>'

    severe, minor = D.split_flags(D.verdict_flags(data))
    if severe:
        n = len(severe)
        out += (
            f'<div class="flag-badge severe" title="{escape("; ".join(severe))}">'
            f'⚠ Uwaga w opiniach AI: {n} {plural_pl(n, "poważne zastrzeżenie", "poważne zastrzeżenia", "poważnych zastrzeżeń")}'
            f'</div>'
        )
    elif minor:
        n = len(minor)
        out += (
            f'<div class="flag-badge minor" title="{escape("; ".join(minor))}">'
            f'{n} {plural_pl(n, "drobne zastrzeżenie", "drobne zastrzeżenia", "drobnych zastrzeżeń")}</div>'
        )
    return out


def score_bar_html(label: str, value) -> str:
    pct = 0 if value is None else max(0, min(5, value)) / 5 * 100
    val_txt = f"{value}/5" if value is not None else "brak danych"
    return (
        f'<div class="score-row"><span class="score-label">{escape(label)}</span>'
        f'<div class="score-track"><div class="score-fill" style="width:{pct:.0f}%"></div></div>'
        f'<span class="score-value">{escape(val_txt)}</span></div>'
    )


def ai_verdict_full_html(row) -> str:
    data = D.parse_verdict(row)
    if data is None:
        return '<p class="empty-note">Werdykt AI jest w bazie, ale nie da się go odczytać.</p>'

    parts: list[str] = []
    one_liner = str(data.get("one_liner") or "").strip()
    if one_liner:
        parts.append(f'<p class="verdict-oneliner">{escape(one_liner)}</p>')

    severe, minor = D.split_flags(D.verdict_flags(data))
    if severe:
        items = "".join(f'<li class="severe">{escape(f)}</li>' for f in severe)
        parts.append(
            f'<div class="flag-banner"><div><strong>⚠ Poważne zastrzeżenia w opiniach gości</strong>'
            f'<ul class="flag-list">{items}</ul></div></div>'
        )
    if minor:
        items = "".join(f"<li>{escape(f)}</li>" for f in minor)
        parts.append(f'<p class="small muted" style="margin-bottom:0">Drobniejsze uwagi:</p>'
                     f'<ul class="flag-list">{items}</ul>')

    beach = data.get("beach") or {}
    if not isinstance(beach, dict):
        beach = {}
    parts.append(score_bar_html("Plaża", beach.get("quality")))
    notes = str(beach.get("notes") or "").strip()
    if notes:
        parts.append(f'<p class="beach-notes muted">{escape(notes)}</p>')

    for field_key, label in D.VERDICT_SCORE_LABELS:
        parts.append(score_bar_html(label, data.get(field_key)))

    try:
        model, created = row["model"], row["created_at"]
    except (KeyError, IndexError):
        model, created = "", ""
    parts.append(
        f'<p class="muted small" style="margin-top:var(--sp-4)">model: {escape(str(model or ""))}'
        f' · wygenerowano: {escape(str(created or ""))}</p>'
    )
    return "".join(parts)


# --------------------------------------------------------------------------
# Karta oferty
# --------------------------------------------------------------------------

def offer_card_html(
    it: dict,
    *,
    urls,
    rating: RatingSummary,
    verdict_row=None,
    history: list[tuple[str, int]] | None = None,
    show_country: bool = False,
) -> str:
    """Karta jednej oferty.

    Hierarchia (od najgłośniejszego): CENA (prawa szyna, największa liczba) →
    TERMIN → OCENA z wiarygodnością → nazwa hotelu → kontekst (chipy, AI).
    """
    history = history or []
    spark = svg_sparkline(history) if len(history) >= 3 else ""
    change_html = "" if it["change"] is None else f'<div class="price-change">{fmt_diff(it["change"])}</div>'

    chips = []
    if show_country and it["country"]:
        chips.append(f'<span class="chip">{escape(it["country"])}</span>')
    for value in (it["board_raw"], it["tour_operator"], it["departure_place"]):
        if value:
            chips.append(f'<span class="chip">{escape(str(value))}</span>')

    loc = " / ".join(x for x in (it["region"], it["city"]) if x)
    term = fmt_term(it["departure_date"], it["return_date"])

    # data-* karmi filtrowanie po stronie klienta w eksporcie statycznym.
    dataset = (
        f' data-country="{escape(D.country_label(it["country"]))}"'
        f' data-price="{it["price"] if it["price"] is not None else ""}"'
        f' data-ppn="{it["price_ppn"] if it["price_ppn"] is not None else ""}"'
        f' data-rating="{rating.headline.value if rating.has_value else ""}"'
        f' data-reviews="{(rating.headline.count or 0) if rating.has_value else 0}"'
        f' data-drop="{it["drop_pct"]:.2f}"'
        f' data-date="{escape(str(it["departure_date"] or ""))}"'
    )

    return f"""<article class="offer-card"{dataset}>
  <div class="offer-main">
    <h3 class="offer-hotel"><a href="{escape(urls.offer(it['key']))}">{escape(it['hotel_name'] or '')}</a> {stars_html(it['stars'])}</h3>
    <div class="offer-loc">{escape(loc)}</div>
    <div class="offer-term">{escape(term)}<span class="nights">{escape(nights_text(it['nights']))}</span></div>
    {rating_block_html(rating)}
    <div class="chip-row">{''.join(chips)}</div>
    {ai_summary_html(verdict_row)}
  </div>
  <div class="offer-price">
    <div class="price-main">{fmt_money(it['price'])}<span class="unit"> /os</span></div>
    <div class="price-sub">{(f"{it['price_ppn']:.0f} zł/os/noc" if it['price_ppn'] is not None else '&nbsp;')}</div>
    {change_html}
    {spark}
    <a class="btn price-cta" href="{escape(urls.offer(it['key']))}">Szczegóły →</a>
  </div>
</article>"""

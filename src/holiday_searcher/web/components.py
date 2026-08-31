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

def svg_price_chart(history: list[tuple[str, int]], width: int = 780, height: int = 260) -> str:
    """Historia ceny jako wykres liniowy: siatka, podpisane osie, obszar pod
    krzywą w `--accent` z niską alfą i wyraźnie wyróżniony OSTATNI punkt —
    to on odpowiada na pytanie „ile jest teraz".

    `history` chronologicznie.
    """
    if not history:
        return ('<div class="empty-note"><strong>Brak historii cen</strong>'
                'To dopiero pierwszy snapshot tej oferty — wykres pojawi się '
                'po kolejnym przebiegu monitora.</div>')

    pad_l, pad_r, pad_t, pad_b = 78, 74, 26, 44
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

    # Wszystkie punkty poza ostatnim: dyskretne kółka z podpowiedzią.
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" class="chart-pt">'
        f'<title>{escape(str(ts))} — {fmt_money(price)}</title></circle>'
        for (x, y), (ts, price) in zip(pts[:-1], history[:-1])
    )

    # Ostatni punkt — halo, pełne kółko i podpis z aktualną ceną.
    lx, ly = pts[-1]
    last_price = history[-1][1]
    last = (
        f'<line x1="{lx:.1f}" y1="{ly:.1f}" x2="{lx:.1f}" y2="{height - pad_b}" class="chart-guide"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="9" class="chart-last-halo"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="5" class="chart-last">'
        f'<title>{escape(str(history[-1][0]))} — {fmt_money(last_price)} (ostatni pomiar)</title></circle>'
        f'<text x="{lx + 10:.1f}" y="{ly + 4:.1f}" class="chart-last-label">{fmt_money(last_price)}</text>'
    )

    grid = []
    for tick in sorted({lo, (lo + hi) // 2, hi}):
        y_px = y_for(tick)
        grid.append(
            f'<line x1="{pad_l}" y1="{y_px:.1f}" x2="{width - pad_r}" y2="{y_px:.1f}" class="chart-guide"/>'
            f'<text x="{pad_l - 8:.1f}" y="{y_px + 4:.1f}" text-anchor="end" class="chart-axis">'
            f'{fmt_money(tick)}</text>'
        )

    # Gdy cała historia mieści się w jednej dobie, trzykrotnie powtórzona ta
    # sama data nic nie mówi — oś przełącza się wtedy na godziny.
    same_day = str(history[0][0])[:10] == str(history[-1][0])[:10]
    axis_title = f"godzina · {escape(str(history[0][0])[:10])}" if same_day else "data pomiaru"

    def x_label(i: int) -> str:
        ts = str(history[i][0])
        return ts[11:16] if same_day else ts[:10]

    label_idxs = {0, n - 1} | ({n // 2} if n >= 3 else set())
    x_labels = "".join(
        f'<text x="{x_for(i):.1f}" y="{height - pad_b + 16:.1f}" '
        f'text-anchor="{"start" if i == 0 else ("end" if i == n - 1 else "middle")}" '
        f'class="chart-axis">{escape(x_label(i))}</text>'
        for i in sorted(label_idxs)
    )

    axes = (
        f'<line x1="{pad_l}" y1="{height - pad_b}" x2="{width - pad_r}" y2="{height - pad_b}" class="chart-axis-line"/>'
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{height - pad_b}" class="chart-axis-line"/>'
        # Osie muszą być podpisane — sama liczba nie mówi, czego dotyczy.
        f'<text x="{pad_l:.1f}" y="{pad_t - 10:.1f}" class="chart-axis-title">cena /os</text>'
        f'<text x="{width - pad_r:.1f}" y="{height - 8:.1f}" text-anchor="end" '
        f'class="chart-axis-title">{axis_title}</text>'
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Wykres historii ceny, ostatni pomiar {escape(fmt_money(last_price))}" '
        f'class="price-chart">'
        f'{"".join(grid)}{axes}{area}<path d="{line}" class="chart-line" fill="none" '
        f'stroke-linejoin="round" stroke-linecap="round"/>{dots}{last}{x_labels}</svg>'
    )


def svg_sparkline(history: list[tuple[str, int]], width: int = 92, height: int = 26) -> str:
    """Mini-trend ceny — ten sam język wizualny co duży wykres: obszar pod
    krzywą z niską alfą i wyróżniony ostatni punkt. Kolor niesie kierunek
    zmiany (`--good` spadek, `--bad` wzrost, `--accent` bez zmian).

    Pusty string dla mniej niż 2 punktów.
    """
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
    base = height - 1
    area = f'<path d="{path} L{lx:.1f} {base} L{pts[0][0]:.1f} {base} Z" class="spark-{kind}-area"/>'
    tip = f"{fmt_money(prices[0])} → {fmt_money(prices[-1])}"
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" class="sparkline" '
        f'role="img" aria-label="Trend ceny: {escape(tip)}"><title>{escape(tip)}</title>'
        f'{area}<path d="{path}" fill="none" class="spark-{kind}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.6" class="spark-{kind}-dot"/></svg>'
    )


# --------------------------------------------------------------------------
# OCENA + WIARYGODNOŚĆ
#
# Sedno aplikacji: „10.0" i „8.6" to dwie różne liczby, ale „10.0 z 1 opinii"
# i „8.6 z 1425 opinii" to dwie różne KLASY informacji. Komponent pokazuje
# jedno obok drugiego i degraduje wizualnie ocenę, której nie ma co ufać.
#
# Wielkość próby rysujemy SEGMENTOWANYM MIERNIKIEM (5 pól, skala
# logarytmiczna), a nie ciągłym paskiem: pola są policzalne jednym rzutem oka
# i porównywalne między kartami, a estetyka przyrządu z podziałką pasuje do
# reszty szaty (almanach / mapa nawigacyjna).
# --------------------------------------------------------------------------

def gauge_html(count: int | None) -> str:
    """Segmentowany miernik wielkości próby — 5 pól wypełnianych logarytmicznie."""
    filled = D.confidence_segments(count)
    total = D.CONFIDENCE_SEGMENTS
    cells = "".join(
        f'<i class="on"></i>' if i < filled else "<i></i>" for i in range(total)
    )
    label = f"wielkość próby: {filled} z {total}"
    return f'<span class="gauge" role="img" aria-label="{escape(label)}">{cells}</span>'


def rating_block_html(summary: RatingSummary, *, big: bool = False) -> str:
    cls_big = " big" if big else ""

    if not summary.has_value:
        return (
            f'<div class="rating conf-none{cls_big}">'
            f'<div class="rating-head"><span class="rating-value">—</span></div>'
            f'<div class="rating-body"><span class="rating-count muted">'
            f'{escape(D.CONFIDENCE_LABELS["none"])}</span>'
            f'<span class="rating-conf">{gauge_html(0)}</span></div></div>'
        )

    head = summary.headline
    assert head is not None
    count = head.count or 0
    conf_label = D.CONFIDENCE_LABELS[summary.confidence]

    if head.count is None:
        count_txt = "liczba opinii nieznana"
    elif summary.confidence == "thin":
        count_txt = f"tylko {count} {reviews_word(count)}"
    else:
        count_txt = f"{fmt_int(count)} {reviews_word(count)}"

    # Znikoma próba: liczba już cichnie (CSS `.conf-thin`), a obok niej staje
    # etykieta w --warn, żeby powód ściszenia był nazwany, nie tylko pokazany.
    thin_tag = (
        '<span class="rating-thin-tag">słabe dowody</span>'
        if summary.confidence == "thin" else ""
    )

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
  <div class="rating-head"><span class="rating-value">{head.value:.1f}</span><span class="rating-scale">/10</span></div>
  <div class="rating-body">
    <span class="rating-count">{escape(count_txt)} <span class="src">· {escape(head.label)}</span> {thin_tag}</span>
    <span class="rating-conf">{gauge_html(head.count)} {escape(conf_label)}</span>
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

def offer_dataset(it: dict, rating: RatingSummary) -> str:
    """Atrybuty `data-*` wspólne dla karty i wiersza tabeli.

    Karmią filtrowanie i sortowanie po stronie klienta. Oba widoki tej samej
    oferty niosą identyczny komplet, więc jeden przebieg JS obsługuje je razem
    i nie da się doprowadzić do stanu, w którym tabela pokazuje co innego niż
    karty.
    """
    return (
        f' data-offer="{escape(str(it["key"]))}"'
        f' data-country="{escape(D.country_label(it["country"]))}"'
        f' data-price="{it["price"] if it["price"] is not None else ""}"'
        f' data-ppn="{it["price_ppn"] if it["price_ppn"] is not None else ""}"'
        f' data-rating="{rating.headline.value if rating.has_value else ""}"'
        f' data-reviews="{(rating.headline.count or 0) if rating.has_value else 0}"'
        f' data-drop="{it["drop_pct"]:.2f}"'
        f' data-date="{escape(str(it["departure_date"] or ""))}"'
    )


def offer_card_html(
    it: dict,
    *,
    urls,
    rating: RatingSummary,
    verdict_row=None,
    history: list[tuple[str, int]] | None = None,
    show_country: bool = False,
    flag: str = "",
    flag_title: str = "",
) -> str:
    """Karta jednej oferty.

    Hierarchia (od najgłośniejszego): CENA za osobę (prawa szyna, największa
    liczba na karcie, mono) → TERMIN tuż pod nazwą hotelu → OCENA
    z wiarygodnością. Reszta — wyżywienie, biuro, lotnisko, region — to
    kontekst: mniejszy stopień, `--ink-2`/`--ink-3`, bez kolorów.

    `flag` to JEDYNY sygnał (`--signal`) dopuszczony na karcie; przekazuje go
    strona, gdy oferta jest najtańsza w swoim kraju.
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
    flag_html = (
        f'<div class="offer-flag" title="{escape(flag_title or flag)}">{escape(flag)}</div>'
        if flag else ""
    )

    return f"""<article class="offer-card"{offer_dataset(it, rating)}>
  <div class="offer-main">
    {flag_html}
    <h3 class="offer-hotel"><a href="{escape(urls.offer(it['key']))}">{escape(it['hotel_name'] or '')}</a> {stars_html(it['stars'])}</h3>
    <div class="offer-term">{escape(term)}<span class="term-nights">{escape(nights_text(it['nights']))}</span></div>
    <div class="offer-loc">{escape(loc)}</div>
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


OFFER_TABLE_HEAD = (
    "<thead><tr>"
    "<th>Hotel</th><th>Termin</th><th class='num'>Noc.</th>"
    "<th class='num'>Cena /os</th><th class='num'>zł/noc</th>"
    "<th class='num'>Ocena</th><th class='num'>Opinie</th>"
    "<th>Wyżywienie</th><th>Biuro</th><th>Region</th>"
    "</tr></thead>"
)


def offer_row_html(it: dict, *, urls, rating: RatingSummary, flag: str = "",
                   flag_title: str = "") -> str:
    """Wiersz trybu tabeli: wszystko w jednej linii, liczby w mono.

    Przy stu kilkudziesięciu ofertach karta jest za droga w pionie — tabela
    pozwala przeskanować całość wzrokiem, a kolumny liczbowe (tabular-nums)
    układają się w pion, więc porównanie nie wymaga czytania.
    """
    loc = " / ".join(x for x in (it["region"], it["city"]) if x)
    term = fmt_term(it["departure_date"], it["return_date"])
    href = escape(urls.offer(it["key"]))

    if rating.has_value:
        thin = " thin" if rating.confidence == "thin" else ""
        rate_cell = f'<td class="n{thin}">{rating.headline.value:.1f}</td>'
        count = rating.headline.count
        count_cell = (
            f'<td class="n{thin}">{fmt_int(count)}</td>' if count is not None
            else '<td class="n thin">?</td>'
        )
    else:
        rate_cell = '<td class="n thin">—</td>'
        count_cell = '<td class="n thin">—</td>'

    dot = '<span class="row-flag" aria-hidden="true"></span>' if flag else ""
    title = f' title="{escape(flag_title or flag)}"' if flag else ""
    ppn = f"{it['price_ppn']:.0f}" if it["price_ppn"] is not None else "—"

    return (
        f'<tr class="offer-row"{offer_dataset(it, rating)}{title}>'
        f'<td class="hotel">{dot}<a href="{href}">{escape(it["hotel_name"] or "")}</a></td>'
        f'<td class="n">{escape(term)}</td>'
        f'<td class="n">{it["nights"] if it["nights"] is not None else "—"}</td>'
        f'<td class="price">{fmt_money(it["price"])}</td>'
        f'<td class="n">{ppn}</td>'
        f'{rate_cell}{count_cell}'
        f'<td class="ctx">{escape(str(it["board_raw"] or ""))}</td>'
        f'<td class="ctx">{escape(str(it["tour_operator"] or ""))}</td>'
        f'<td class="ctx">{escape(loc)}</td>'
        f"</tr>"
    )

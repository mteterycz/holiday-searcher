"""Kuracja wyników: co ZAPISUJEMY do bazy, a co było tylko materiałem statystycznym.

Powód istnienia: przy szerokim wyszukiwaniu serwis zwraca setki ofert, z czego
większość to warianty tego samego hotelu (inny operator, inne lotnisko, dzień
różnicy). Baza pełna takich duplikatów jest nie do przejrzenia, a historia cen
rozmywa się na klucze, które nikogo nie interesują.

Zasada: pobieramy szeroko (statystyka potrzebuje próbki), ale utrwalamy wąsko.
"""
from __future__ import annotations

from .scoring import Scored


def curate(scored: list[Scored], per_country: int = 15,
           per_hotel: int = 1, global_cap: int | None = None) -> list[Scored]:
    """Najpierw najlepsze warianty każdego hotelu, potem limit na kraj.

    `per_hotel=1` oznacza jedną, najlepiej ocenioną ofertę danego hotelu —
    reszta wariantów i tak prowadzi do tego samego miejsca.
    """
    by_hotel: dict[str, list[Scored]] = {}
    for s in scored:
        key = s.offer.hotel_id or f"{s.offer.hotel_name}|{s.offer.country}"
        by_hotel.setdefault(key, []).append(s)

    winners: list[Scored] = []
    for variants in by_hotel.values():
        variants.sort(key=lambda s: s.score, reverse=True)
        winners.extend(variants[:per_hotel])

    winners.sort(key=lambda s: s.score, reverse=True)

    kept: list[Scored] = []
    seen_per_country: dict[str, int] = {}
    for s in winners:
        c = s.offer.country or "?"
        if seen_per_country.get(c, 0) >= per_country:
            continue
        seen_per_country[c] = seen_per_country.get(c, 0) + 1
        kept.append(s)
        if global_cap and len(kept) >= global_cap:
            break
    return kept


def summarize(scored: list[Scored], kept: list[Scored]) -> str:
    dropped = len(scored) - len(kept)
    by_country: dict[str, int] = {}
    for s in kept:
        by_country[s.offer.country or "?"] = by_country.get(s.offer.country or "?", 0) + 1
    rozbicie = ", ".join(f"{k}: {v}" for k, v in sorted(by_country.items()))
    return f"zachowano {len(kept)} z {len(scored)} ofert (odrzucono {dropped}) — {rozbicie}"

"""Scoring deterministyczny — bez LLM. Liczby zostają po stronie kodu.

Kluczowa idea: oferty porównujemy tylko wewnątrz KOSZYKA porównawczego
(ten sam region, ±1 gwiazdka, ta sama rodzina wyżywienia), i wyłącznie
w cenie za osobę za noc. Inaczej 'okazja' znaczy tyle co nic.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from .models import BOARD_TIERS, Offer

W_PRICE = 0.45
W_QUALITY = 0.30
W_BOARD = 0.10
W_DEAL = 0.15

# Sufit jakości dla hotelu bez opinii — poniżej tego, co daje przyzwoita ocena.
UNRATED_CAP = 0.55


@dataclass
class Scored:
    offer: Offer
    score: float
    price_index: float      # >1 = taniej niż mediana koszyka
    quality: float          # 0..1
    board_score: float      # 0..1
    deal: float             # 0..1, z priceOld (w fazie 2 zastąpi to własna historia)
    basket_median_ppn: float
    basket_size: int

    def explain(self) -> str:
        return (f"cena {self.price_index:.2f}× | jakość {self.quality:.2f} | "
                f"wyżyw. {self.board_score:.2f} | promo {self.deal:.2f} "
                f"(koszyk n={self.basket_size})")


def _basket_key(o: Offer) -> tuple:
    board_family = "AI" if o.board in ("AI", "UAI", "AI_PLUS", "AI_SOFT") else o.board
    return (o.region, round(o.stars), board_family)


def score_all(offers: list[Offer], reference: list[Offer] | None = None) -> list[Scored]:
    """`reference` to NIEOBCIĄŻONA próbka rynku, z której liczymy mediany koszyków.
    Bez niej mediana pochodziłaby z próbki posortowanej po cenie i każda oferta
    wychodziłaby okazją względem samego taniego ogona."""
    ref = reference if reference else offers
    baskets: dict[tuple, list[float]] = {}
    for o in ref:
        baskets.setdefault(_basket_key(o), []).append(o.price_ppn)

    # Fallback, gdy koszyk jest za mały, żeby mediana coś znaczyła.
    global_median = statistics.median([o.price_ppn for o in ref]) if ref else 0.0

    out: list[Scored] = []
    for o in offers:
        bk = _basket_key(o)
        vals = baskets.get(bk, [])
        if len(vals) >= 4:
            med = statistics.median(vals)
            n = len(vals)
        else:
            med, n = global_median, 0

        price_index = (med / o.price_ppn) if o.price_ppn else 0.0
        # 0.7×–1.4× mediany mapujemy na 0..1; skrajności obcinamy
        price_component = _clamp((price_index - 0.7) / 0.7)

        # Brak opinii to brak danych, nie dobra ocena. Hotel nieoceniony nie może
        # przebić hotelu z realnie dobrą oceną — stąd twardy sufit UNRATED_CAP.
        if o.rating is not None and (o.rating_count or 0) > 0:
            conf = min((o.rating_count or 0) / 50.0, 1.0)   # mało opinii = mniejsza waga
            prior = (o.stars / 5.0) * UNRATED_CAP
            quality = _clamp(o.rating / 10.0) * conf + prior * (1 - conf)
        else:
            quality = (o.stars / 5.0) * UNRATED_CAP

        board_score = BOARD_TIERS.get(o.board, ("", 0))[1] / 3.0

        deal = 0.0
        if o.price_old and o.price_old > o.price:
            deal = _clamp((o.price_old - o.price) / o.price_old / 0.30)

        total = (W_PRICE * price_component + W_QUALITY * quality
                 + W_BOARD * board_score + W_DEAL * deal)

        out.append(Scored(o, round(total * 10, 2), price_index, quality,
                          board_score, deal, med, n))

    out.sort(key=lambda s: s.score, reverse=True)
    return out


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))

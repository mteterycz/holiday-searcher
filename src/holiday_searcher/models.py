"""Kanoniczny model danych — wspólny dla wszystkich dostawców."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# Rodziny wyżywienia sprowadzone do wspólnego mianownika.
# tier pozwala scoringowi karać słabsze warianty (AI Soft) bez wyrzucania ich z wyników.
BOARD_TIERS = {
    "UAI": ("Ultra All Inclusive", 3),
    "AI_PLUS": ("All Inclusive Plus", 3),
    "AI": ("All Inclusive", 2),
    "AI_SOFT": ("All Inclusive Soft", 1),
    "FB": ("Trzy posiłki", 0),
    "HB": ("Śniadania i obiadokolacje", 0),
    "BB": ("Śniadania", 0),
    "OTHER": ("Inne", 0),
}


@dataclass(frozen=True)
class Destination:
    """Jeden kierunek w profilu. Reguły wyżywienia bywają różne per kraj
    (Turcja ma sens tylko w All Inclusive, Włochy czy Malta już ze śniadaniem),
    więc `boards` mogą nadpisać ustawienie profilu."""
    country: str
    regions: list[str] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    label: str = ""
    max_price_pp: Optional[int] = None

    @property
    def name(self) -> str:
        return self.label or self.country.capitalize()


@dataclass(frozen=True)
class SearchProfile:
    name: str
    country: str
    date_from: date
    date_to: date
    nights_min: int
    nights_max: int
    boards: list[str]
    adults: int = 2
    children_ages: list[int] = field(default_factory=list)
    stars_min: int = 0
    rating_min: float = 0.0   # odrzucaj oferty z oceną gości poniżej progu (0 = wyłączone)
    max_price_pp: Optional[int] = None
    departures: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    vibe: Optional[str] = None
    destinations: list[Destination] = field(default_factory=list)

    def legs(self) -> list[Destination]:
        """Kierunki do przeszukania. Profil jednokierunkowy (stary format) jest
        opakowywany w jeden Destination, żeby reszta kodu miała jedną ścieżkę."""
        if self.destinations:
            return [
                Destination(
                    country=d.country,
                    regions=list(d.regions),
                    boards=list(d.boards or self.boards),
                    label=d.label,
                    max_price_pp=d.max_price_pp or self.max_price_pp,
                )
                for d in self.destinations
            ]
        return [Destination(country=self.country, regions=list(self.regions),
                            boards=list(self.boards), label=self.country.capitalize(),
                            max_price_pp=self.max_price_pp)]


@dataclass
class Offer:
    """Jedna oferta w postaci znormalizowanej. Tożsamość jest stabilna w czasie —
    cena NIE jest jej częścią, bo to ona się zmienia i to ją śledzimy."""
    provider: str
    hotel_name: str
    hotel_id: str
    tour_operator: str
    country: str
    region: str
    city: str
    stars: float
    departure_date: date
    return_date: date
    nights: int
    board: str
    board_raw: str
    departure_place: str
    departure_code: str
    room_type: str
    price: int                  # PLN za osobę (patrz README — do weryfikacji per dostawca)
    price_old: int
    rating: Optional[float]
    rating_count: Optional[int]
    url: str
    raw_id: str

    @property
    def key(self) -> str:
        parts = [
            self.provider, self.hotel_id, self.tour_operator,
            self.departure_date.isoformat(), str(self.nights),
            self.board, self.departure_code, self.room_type,
        ]
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]

    @property
    def price_ppn(self) -> float:
        """Cena za osobę za noc — jedyna wielkość, w której oferty wolno porównywać."""
        return self.price / self.nights if self.nights else float("inf")

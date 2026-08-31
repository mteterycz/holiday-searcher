"""Interfejs dostawcy. Adapter ma być głupi: pobrać i zmapować.
Cała logika (scoring, historia, dedup) żyje piętro wyżej i jest wspólna."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Offer, SearchProfile


class Provider(ABC):
    name: str

    @abstractmethod
    def search(self, profile: SearchProfile, limit: int | None = None) -> list[Offer]:
        ...

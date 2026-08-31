"""Warstwa AI (faza 3): opinie o hotelach + ocena jakościowa przez Gemini.

Trzy zasady, na których stoi cały moduł:

1. **AI ocenia HOTELE, nie oferty.** Oferta zmienia się co dzień (cena, termin,
   biuro), hotel nie. Werdykt jest więc cache'owany permanentnie per
   (hotel, prompt_version, model) i przeżywa wszystkie kolejne przebiegi.
2. **Grounding.** Prompt jawnie zabrania modelowi używania własnej wiedzy
   o hotelu — ocena wyłącznie z dostarczonych opinii. Brak informacji => null.
   Dlatego schema odpowiedzi dopuszcza null w każdym polu oceny.
3. **Graceful degradation.** Brak klucza API albo wyczerpany limit dzienny nie
   wywala niczego: moduł zwraca brak werdyktu, a CLI mówi o tym po ludzku.

Werdykty z różnych modeli NIE są porównywalne — każdy zapisuje `model`
i `prompt_version`, a jeden przebieg rankingowy używa jednego modelu.
"""
from __future__ import annotations

from .client import GeminiClient, GeminiError
from .pool import ROLE_CHAINS, ModelPool, ModelSpec
from .prompts import PROMPT_VERSION

__all__ = [
    "GeminiClient", "GeminiError", "ModelPool", "ModelSpec",
    "ROLE_CHAINS", "PROMPT_VERSION",
]

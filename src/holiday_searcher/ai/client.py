"""Cienki klient Gemini. Celowo cienki: bez SDK, bez sesji, bez magii.

Endpoint:
  POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=...

Structured output wymuszamy przez generationConfig.responseMimeType="application/json"
+ responseSchema. Dzięki temu nie parsujemy prozy i nie wycinamy ```json z odpowiedzi.

Brak klucza NIE jest błędem — `available` jest wtedy False, a wywołujący degraduje
się po cichu. Wyjątkiem rzucamy tylko wtedy, gdy request naprawdę poszedł i padł.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..config import get_secret

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT = 60.0
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_SLEEP = 5.0
RETRY_SLEEP_429 = 65.0  # okno minutowe TPM/RPM musi zdążyć się przetoczyć


class GeminiError(RuntimeError):
    """Wywołanie doszło do API i padło (status != 2xx albo śmieć w odpowiedzi)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GeminiClient:
    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = TIMEOUT,
        http: httpx.Client | None = None,
        sleeper=time.sleep,
    ):
        self.api_key = api_key if api_key is not None else get_secret("GEMINI_API_KEY")
        self.timeout = timeout
        self._http = http
        self._sleep = sleeper

    @property
    def available(self) -> bool:
        """Bez klucza cały moduł AI ma się zdegradować, a nie wywalić."""
        return bool(self.api_key)

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        return self._http

    def generate(self, model: str, system: str, user: str, schema: dict[str, Any]) -> dict:
        """Jedno wywołanie ze structured output. Zwraca sparsowany JSON.

        Retry dokładnie 1× na 429/5xx — więcej nie ma sensu: przy RPD=20
        uparte ponawianie tylko wypala limit.
        """
        if not self.available:
            raise GeminiError("Brak GEMINI_API_KEY — klient niedostępny")

        url = f"{API_BASE}/{model}:generateContent?key={self.api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                # Ocena ma być powtarzalna, nie kreatywna.
                "temperature": 0.2,
            },
        }

        last: Exception | None = None
        for attempt in (1, 2):
            try:
                r = self.http.post(url, content=json.dumps(payload))
            except httpx.HTTPError as exc:
                last = GeminiError(f"Błąd sieci: {exc}")
                if attempt == 1:
                    self._sleep(RETRY_SLEEP)
                    continue
                raise last from exc

            if r.status_code in RETRY_STATUSES and attempt == 1:
                self._sleep(RETRY_SLEEP_429 if r.status_code == 429 else RETRY_SLEEP)
                continue
            if r.status_code >= 400:
                raise GeminiError(
                    f"Gemini {model}: HTTP {r.status_code} {r.text[:300]}", r.status_code
                )
            return _extract_json(r.json(), model)

        raise last or GeminiError(f"Gemini {model}: nieudane wywołanie")


def _extract_json(body: dict, model: str) -> dict:
    candidates = body.get("candidates") or []
    if not candidates:
        reason = (body.get("promptFeedback") or {}).get("blockReason")
        raise GeminiError(f"Gemini {model}: brak odpowiedzi (blockReason={reason})")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    text = "".join(p.get("text") or "" for p in parts).strip()
    if not text:
        raise GeminiError(f"Gemini {model}: pusta treść odpowiedzi")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiError(f"Gemini {model}: odpowiedź nie jest JSON-em: {text[:200]}") from exc
    if not isinstance(data, dict):
        raise GeminiError(f"Gemini {model}: oczekiwano obiektu JSON, jest {type(data).__name__}")
    return data

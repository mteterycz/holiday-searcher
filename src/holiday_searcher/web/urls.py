"""Budowanie adresów — jedno miejsce, dwa tryby.

Dashboard renderuje te same strony w dwóch kontekstach:

* **serwer** (`hs web`) — adresy ścieżkowe z query stringiem: `/offers?sort=ppn`
* **eksport statyczny** (`hs export`) — pliki na dysku: `offers.html`,
  `offer/<key>.html`; muszą działać także przy otwarciu z `file://`, więc są
  **relatywne** (żadnych wiodących `/`) i mają rozszerzenie `.html`.

Strony nigdy nie sklejają adresów ręcznie — dostają obiekt `Urls` i pytają go
o link. Dzięki temu ten sam kod renderujący obsługuje oba tryby, a testy
eksportu sprawdzają tylko, czy w wygenerowanym HTML nie ma adresów absolutnych.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode

# Klucz oferty to skrót heksadecymalny, ale nie ufamy temu na ślepo — do nazwy
# pliku przepuszczamy wyłącznie znaki bezpieczne na każdym systemie plików.
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_key(key: str) -> str:
    return _SAFE_KEY_RE.sub("_", str(key))[:120] or "oferta"


def _qs(**params) -> str:
    """Query string bez pustych wartości (`?sort=ppn`, albo pusty string)."""
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return f"?{urlencode(clean)}" if clean else ""


class Urls:
    """Adresy dla trybu serwerowego."""

    static = False

    def index(self) -> str:
        return "/"

    def offers(self, **params) -> str:
        return "/offers" + _qs(**params)

    def hotels(self, **params) -> str:
        return "/hotels" + _qs(**params)

    def drops(self) -> str:
        return "/drops"

    def calendar(self) -> str:
        return "/kalendarz"

    def offer(self, key: str) -> str:
        return f"/offer/{safe_key(key)}"


class StaticUrls(Urls):
    """Adresy dla eksportu na dysk. `prefix` to droga do katalogu głównego
    eksportu z miejsca, w którym leży renderowana strona (`""` dla plików
    w korzeniu, `"../"` dla `offer/<key>.html`)."""

    static = True

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix

    def index(self) -> str:
        return self.prefix + "index.html"

    def offers(self, **params) -> str:
        return self.prefix + "offers.html"

    def hotels(self, **params) -> str:
        return self.prefix + "hotels.html"

    def drops(self) -> str:
        return self.prefix + "drops.html"

    def calendar(self) -> str:
        return self.prefix + "kalendarz.html"

    def offer(self, key: str) -> str:
        return f"{self.prefix}offer/{safe_key(key)}.html"

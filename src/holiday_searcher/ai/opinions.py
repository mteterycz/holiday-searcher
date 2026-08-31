"""Opinie o hotelach z wakacje.pl.

REKONESANS (faza 3, szczegóły i surowe odpowiedzi w docs/faza3-ai.md)
--------------------------------------------------------------------
Endpointy z fazy 0 okazały się tylko częściowo prawdziwe:

* `POST /v2/api/getOpinionsBox` — DZIAŁA, ale payload wymaga kompletu
  `{"hotelId": <int>, "objType": "H", "brand": "WAK"}`. Sam `hotelId` daje
  upstreamowe 404, brak `hotelId` daje 400. Zwraca WYŁĄCZNIE agregaty
  (opinionsCount, ratingValue, reservationCount) — zero treści opinii.
* `POST /v2/api/getOpinions` — nieudokumentowany w fazie 0, a to on zwraca
  treści: {opinions: [{rank, note, advantage, defect, kindOfTrip, tripDate}]}.
  Ten sam payload co wyżej. W praktyce oddaje jednak tylko opinie ze starego
  systemu (sprzed 2013 / spoza puli zweryfikowanych) — dla przykładowego hotelu
  1 opinię zamiast 20.
* `POST /v2/api/newOpinions/` i `/v2/api/getPlusesMinuses/` — NIE ISTNIEJĄ.
  Proxy odpowiada 404 `{"error":"Not found"}` niezależnie od payloadu,
  z ukośnikiem i bez. Sprawdzono też warianty `getNewOpinions`,
  `getHotelPlusesMinuses`. Plusy i minusy nie mają osobnego endpointu —
  są polami `advantage`/`defect` przy każdej opinii.
* `POST /v2/api/getHotelDescription` — istnieje, ale nie udało się odgadnąć
  payloadu (400 dla hotelId/objType/objCode/tourOpCode w każdej kombinacji).
  Patrz `fetch_description` — świadomie NotImplementedError.

Główne źródło to więc STRONA OPINII, renderowana po stronie serwera:
`https://www.wakacje.pl/opinie/hotele/{slug}-h{hotelId}.html`
Zawiera inline `<script>var opinions = [...]</script>` z pełnym, czystym JSON-em
(rate, note, advantage, defect, kindOfTrip, tripDateAt) oraz oceny cząstkowe
(Ogólne wrażenia / Hotel / Położenie / Pokoje / Wyżywienie / Atrakcje dla dzieci /
Sport i rozrywka). Jeden GET zamiast trzech POST-ów, bez parsowania prozy.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

BASE = "https://www.wakacje.pl"
API = f"{BASE}/v2/api"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Inline JSON ze strony opinii. Na stronie są DWA bloki `var opinions` —
# krótki (podgląd u góry) i pełny; bierzemy dłuższy.
_OPINIONS_RE = re.compile(r"<script[^>]*>\s*var\s+opinions\s*=\s*(\[.*?\])\s*;?\s*</script>", re.S)
_SUBSCORE_RE = re.compile(r"item__title'>(.*?)</div>.*?class='score'>\s*([\d.,]+)\s*<", re.S)
_RATING_RE = re.compile(r'"ratingValue"\s*:\s*"?([\d.]+)"?')


@dataclass
class Opinion:
    author: str = ""
    rate: float | None = None          # 0-10, skala wakacje.pl
    trip_date: str = ""                # YYYY-MM
    kind: str = ""                     # np. "Rodzina z dziećmi"
    text: str = ""                     # główna treść
    advantage: str = ""                # "Zalety"
    defect: str = ""                   # "Wady"
    verified: bool = False

    @property
    def is_empty(self) -> bool:
        return not (self.text or self.advantage or self.defect)


@dataclass
class HotelOpinions:
    hotel_id: str
    slug: str = ""
    url: str = ""
    rating: float | None = None
    subscores: dict[str, float] = field(default_factory=dict)
    opinions: list[Opinion] = field(default_factory=list)
    source: str = "wakacje.pl"
    error: str | None = None

    def __len__(self) -> int:
        return len(self.opinions)

    @property
    def ok(self) -> bool:
        return bool(self.opinions)

    def fingerprint_material(self) -> str:
        """Materiał do input_hash werdyktu — dokładnie to, co zobaczy model."""
        return "\n".join(
            f"{o.rate}|{o.trip_date}|{o.text}|{o.advantage}|{o.defect}"
            for o in self.opinions
        )


def slug_from_url(url: str) -> str:
    """Z `https://www.wakacje.pl/hotele/arsi-paradise-beach/` robi
    `arsi-paradise-beach`. Slug jest jedyną częścią adresu opinii, której
    nie da się złożyć z samego hotelId."""
    if not url:
        return ""
    path = urlparse(url).path.strip("/")
    if not path:
        return ""
    last = path.split("/")[-1]
    last = re.sub(r"\.html?$", "", last)
    last = re.sub(r"-h?\d+$", "", last)      # ...-h35267 / ...-35267
    return last


def opinions_url(hotel_id: str | int, slug: str) -> str:
    return f"{BASE}/opinie/hotele/{slug}-h{hotel_id}.html"


class WakacjeOpinions:
    """Pobieranie opinii. Nigdy nie rzuca przy błędzie sieci — zwraca pusty
    `HotelOpinions` z ustawionym `error`, bo brak opinii to normalny stan
    (hotel bez recenzji), a nie awaria."""

    name = "wakacje.pl"

    def __init__(self, delay: float = 1.5, timeout: float = 30.0,
                 http: httpx.Client | None = None):
        self.delay = delay
        self._http = http
        self._timeout = timeout
        self._last_call = 0.0

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": UA, "Accept": "text/html,application/json",
                         "Referer": f"{BASE}/", "Origin": BASE},
            )
        return self._http

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_call
        if self._last_call and gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.monotonic()

    # ---------- główna droga: strona opinii (SSR) ----------

    def fetch(self, hotel_id: str | int, slug: str = "", url: str = "",
              max_opinions: int = 30) -> HotelOpinions:
        slug = slug or slug_from_url(url)
        hid = str(hotel_id)
        if not slug or not hid:
            return HotelOpinions(hid, slug, error="brak sluga albo hotelId")

        target = opinions_url(hid, slug)
        out = HotelOpinions(hid, slug, url=target)
        self._throttle()
        try:
            r = self.http.get(target)
        except httpx.HTTPError as exc:
            out.error = f"sieć: {exc}"
            return out
        if r.status_code != 200:
            out.error = f"HTTP {r.status_code}"
            return out
        return parse_opinions_page(r.text, hid, slug, target, max_opinions)

    # ---------- droga zapasowa: API ----------

    def fetch_api(self, hotel_id: str | int, limit: int = 25) -> HotelOpinions:
        """`POST /v2/api/getOpinions`. Zwraca opinie ze starego systemu —
        zwykle znacznie mniej niż strona SSR, ale w czystym JSON-ie."""
        hid = str(hotel_id)
        out = HotelOpinions(hid, source="wakacje.pl/api")
        self._throttle()
        try:
            r = self.http.post(
                f"{API}/getOpinions",
                content=json.dumps(_api_payload(hid, limit)),
                headers={"Content-Type": "application/json"},
            )
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            out.error = f"sieć: {exc}"
            return out
        if not body.get("success"):
            out.error = str((body.get("error") or {}).get("message") or body.get("msg"))
            return out
        data = body.get("data") or {}
        for raw in (data.get("opinions") or [])[:limit]:
            out.opinions.append(Opinion(
                author=str(raw.get("name") or ""),
                rate=_as_float(raw.get("rank")),
                trip_date=str(raw.get("tripDate") or "")[:7],
                kind=str(raw.get("kindOfTrip") or ""),
                text=_clean(raw.get("note")),
                advantage=_clean(raw.get("advantage")),
                defect=_clean(raw.get("defect")),
                verified=bool(raw.get("isClient")),
            ))
        return out

    def fetch_box(self, hotel_id: str | int) -> dict[str, Any]:
        """`POST /v2/api/getOpinionsBox` — same agregaty (liczba opinii, ocena,
        liczba rezerwacji). Bez treści opinii, więc do werdyktu się nie nadaje;
        przydaje się do sanity-checku, czy w ogóle jest co pobierać."""
        self._throttle()
        try:
            r = self.http.post(
                f"{API}/getOpinionsBox",
                content=json.dumps(_api_payload(str(hotel_id))),
                headers={"Content-Type": "application/json"},
            )
            body = r.json()
        except (httpx.HTTPError, ValueError):
            return {}
        return (body.get("data") or {}) if body.get("success") else {}

    def fetch_description(self, hotel_id: str | int) -> dict[str, Any]:
        """`POST /v2/api/getHotelDescription` istnieje (proxy przepuszcza do
        `/v2/hotels/getHotelDescription`), ale każdy zgadnięty payload kończy
        się upstreamowym 400. Sprawdzone kombinacje: hotelId; hotelId+brand;
        hotelId+objType+brand; hotelId+objType+objCode+tourOpCode.
        Do dokończenia dopiero, gdy uda się podejrzeć realny request przeglądarki
        — opis hotelu i tak jest w HTML-u strony hotelu, więc nie blokuje fazy 3."""
        raise NotImplementedError(
            "getHotelDescription: nieodgadnięty payload — patrz docs/faza3-ai.md"
        )


def parse_opinions_page(html: str, hotel_id: str, slug: str = "", url: str = "",
                        max_opinions: int = 30) -> HotelOpinions:
    """Wydzielone z pobierania, żeby dało się testować na zapisanym HTML-u."""
    out = HotelOpinions(hotel_id, slug, url=url)

    blocks = _OPINIONS_RE.findall(html)
    raw: list[dict] = []
    for block in sorted(blocks, key=len, reverse=True):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        # Pełny blok ma `rate`; krótki (podgląd) tylko note+authorName.
        if isinstance(parsed, list) and parsed and "rate" in parsed[0]:
            raw = parsed
            break
        if isinstance(parsed, list) and not raw:
            raw = parsed
    for item in raw[:max_opinions]:
        if not isinstance(item, dict):
            continue
        out.opinions.append(Opinion(
            author=str(item.get("authorName") or ""),
            rate=_as_float(item.get("rate")),
            trip_date=_date_of(item.get("tripDateAt"))[:7],
            kind=str(item.get("kindOfTrip") or ""),
            text=_clean(item.get("note")),
            advantage=_clean(item.get("advantage")),
            defect=_clean(item.get("defect")),
            verified=bool(item.get("isClient")),
        ))
    out.opinions = [o for o in out.opinions if not o.is_empty]

    for title, score in _SUBSCORE_RE.findall(html):
        name = re.sub(r"<[^>]+>", "", title).strip()
        val = _as_float(score.replace(",", "."))
        if name and val is not None and name not in out.subscores:
            out.subscores[name] = val

    m = _RATING_RE.search(html)
    if m:
        out.rating = _as_float(m.group(1))
    if not out.opinions and not out.error:
        out.error = "brak opinii na stronie"
    return out


def _api_payload(hotel_id: str, limit: int | None = None) -> dict[str, Any]:
    """Komplet pól jest wymagany — patrz rekonesans w docstringu modułu."""
    payload: dict[str, Any] = {"hotelId": int(hotel_id), "objType": "H", "brand": "WAK"}
    if limit:
        payload["limit"] = limit
    return payload


def _date_of(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("date") or "")
    return str(v or "")


def _clean(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v else ""


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

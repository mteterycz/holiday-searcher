"""Prompty i schematy odpowiedzi.

PROMPT_VERSION jest częścią klucza cache'u werdyktów. Każda zmiana treści
promptu albo schematu MUSI podbić tę liczbę — inaczej stary werdykt zostanie
podany jako świeży i nikt się nie zorientuje, że powstał w innym reżimie.

Grounding jest tu najważniejszy. Model zna te hotele z internetu i bardzo chce
się tą wiedzą podzielić — a wtedy ocena przestaje mieć cokolwiek wspólnego
z opiniami, na których miała się opierać. Stąd zakaz wprost i null jako
pełnoprawna, oczekiwana odpowiedź.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

PROMPT_VERSION = 1

# ---------------------------------------------------------------- werdykt hotelu

VERDICT_SYSTEM = """\
Jesteś analitykiem opinii hotelowych. Twoim JEDYNYM źródłem wiedzy są opinie
podróżnych podane w wiadomości użytkownika.

ZASADY BEZWZGLĘDNE:
1. NIE WOLNO Ci korzystać z własnej wiedzy o tym hotelu, sieci hotelowej,
   miejscowości ani regionie. Jeśli wiesz coś o tym hotelu spoza opinii —
   zignoruj to. Traktuj hotel jak obiekt, o którym nie wiesz NIC poza tym,
   co napisano w opiniach.
2. Jeśli opinie nie mówią o danym aspekcie — wpisz null. Null jest poprawną,
   oczekiwaną odpowiedzią. Zgadywanie jest błędem gorszym niż brak oceny.
3. Nie uśredniaj "na oko" ocen liczbowych z opinii — oceniaj TREŚĆ wypowiedzi.
4. Jedna wzmianka to za mało na ocenę skrajną. Skrajności (1 lub 5) rezerwuj
   dla aspektów, o których pisze kilka opinii zgodnie.

SKALE (1-5, całkowite):
- beach.quality: 5 = szeroka, czysta, piaszczysta plaża przy hotelu;
  1 = plaży praktycznie nie ma albo jest odpychająca.
- food: 5 = obfite i smaczne; 1 = głodno i niesmacznie.
- cleanliness: 5 = wzorowa czystość; 1 = brud, insekty, brak sprzątania.
- noise: 5 = CICHO (spokojny wypoczynek); 1 = hałaśliwie (droga, dyskoteka,
  budowa). Uwaga na kierunek skali: wyższa ocena = mniej hałasu.
- family_friendly: 5 = świetny dla rodzin z dziećmi; 1 = zdecydowanie nie.

red_flags: krótkie hasła po polsku o rzeczach, które dyskwalifikują wyjazd
(np. "karaluchy", "brak sprzątania", "hałas z drogi", "niebezpieczny basen").
Tylko to, co realnie wynika z opinii; pusta lista jest w porządku.

one_liner: jedno zdanie po polsku, maksymalnie 140 znaków, bez marketingu —
tak, jakbyś mówił znajomemu, czego się spodziewać.

Całość odpowiedzi po polsku, w formacie JSON zgodnym ze schematem.\
"""

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "beach": {
            "type": "OBJECT",
            "nullable": True,
            "properties": {
                "quality": {"type": "INTEGER", "nullable": True,
                            "description": "1-5 albo null przy braku informacji"},
                "notes": {"type": "STRING", "nullable": True,
                          "description": "krótka notatka po polsku albo null"},
            },
            "required": ["quality", "notes"],
        },
        "food": {"type": "INTEGER", "nullable": True},
        "cleanliness": {"type": "INTEGER", "nullable": True},
        "noise": {"type": "INTEGER", "nullable": True,
                  "description": "5 = cicho, 1 = hałaśliwie"},
        "family_friendly": {"type": "INTEGER", "nullable": True},
        "red_flags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "one_liner": {"type": "STRING", "nullable": True},
    },
    "required": ["beach", "food", "cleanliness", "noise",
                 "family_friendly", "red_flags", "one_liner"],
}


def build_verdict_user(
    hotel_name: str,
    region: str,
    opinions: Sequence[Any],
    max_opinions: int = 25,
) -> str:
    """Buduje wiadomość użytkownika dla werdyktu.

    `opinions` to obiekty z polami text/advantage/defect/rate/trip_date/kind
    (patrz `ai.opinions.Opinion`) — akceptujemy też zwykłe słowniki.
    """
    lines = [
        f"HOTEL: {hotel_name}",
        f"REGION: {region or 'nieznany'}",
        "",
        "OPINIE PODRÓŻNYCH (jedyne dopuszczalne źródło):",
    ]
    used = 0
    for op in opinions:
        get = op.get if isinstance(op, dict) else (lambda k, _o=op: getattr(_o, k, None))
        text = (get("text") or "").strip()
        adv = (get("advantage") or "").strip()
        dfc = (get("defect") or "").strip()
        if not (text or adv or dfc):
            continue
        used += 1
        head = f"--- Opinia {used}"
        rate, trip, kind = get("rate"), get("trip_date"), get("kind")
        meta = [x for x in (f"ocena {rate}/10" if rate else None, trip, kind) if x]
        if meta:
            head += " (" + ", ".join(str(m) for m in meta) + ")"
        lines.append(head)
        if text:
            lines.append(text)
        if adv:
            lines.append(f"PLUSY: {adv}")
        if dfc:
            lines.append(f"MINUSY: {dfc}")
        if used >= max_opinions:
            break
    if used == 0:
        lines.append("(brak opinii — wszystkie oceny muszą być null)")
    return "\n".join(lines)


# ------------------------------------------------------------------- vibe match

VIBE_SYSTEM = """\
Dopasowujesz hotele do opisu wymarzonego wyjazdu ("vibe") jednej osoby.

ZASADY BEZWZGLĘDNE:
1. Opierasz się WYŁĄCZNIE na werdyktach hoteli podanych niżej. NIE WOLNO Ci
   używać własnej wiedzy o tych hotelach ani o okolicy.
2. Brak danych o aspekcie ważnym dla vibe'u to powód do oceny umiarkowanej
   (4-6), a NIE do wysokiej. Nie nagradzaj hotelu za to, że nic o nim nie wiemy.
3. Oceniasz KAŻDY hotel z listy — dokładnie raz, z jego oryginalnym hotel_id.

vibe_score: 0-10 (całkowite). 10 = dokładnie ten wyjazd, o którym mowa w opisie;
0 = kompletne przeciwieństwo.
why: jedno-dwa zdania po polsku, konkretnie — który element vibe'u jest
spełniony, a który nie. Bez ogólników w rodzaju "świetny wybór".\
"""

VIBE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "matches": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "hotel_id": {"type": "STRING"},
                    "vibe_score": {"type": "INTEGER", "description": "0-10"},
                    "why": {"type": "STRING"},
                },
                "required": ["hotel_id", "vibe_score", "why"],
            },
        }
    },
    "required": ["matches"],
}


def build_vibe_user(vibe: str, hotels: Iterable[dict]) -> str:
    """`hotels`: [{hotel_id, name, region, verdict: {...}}]. JEDNO wywołanie
    na całą shortlistę — model widzi wtedy hotele obok siebie i ocenia je
    względem siebie, a nie w oderwaniu."""
    lines = [
        "OPIS WYMARZONEGO WYJAZDU (vibe):",
        vibe.strip(),
        "",
        "HOTELE DO OCENY (werdykty powstały wyłącznie z opinii podróżnych):",
    ]
    for h in hotels:
        v = h.get("verdict") or {}
        beach = v.get("beach") or {}
        lines.append("")
        lines.append(f"--- hotel_id: {h.get('hotel_id')}")
        lines.append(f"nazwa: {h.get('name')} ({h.get('region') or 'region nieznany'})")
        lines.append(f"podsumowanie: {v.get('one_liner') or 'brak'}")
        lines.append(
            "oceny 1-5 (null = brak danych): "
            f"plaża={_fmt(beach.get('quality'))}, jedzenie={_fmt(v.get('food'))}, "
            f"czystość={_fmt(v.get('cleanliness'))}, cisza={_fmt(v.get('noise'))}, "
            f"dla rodzin={_fmt(v.get('family_friendly'))}"
        )
        if beach.get("notes"):
            lines.append(f"plaża — notatka: {beach['notes']}")
        flags = v.get("red_flags") or []
        if flags:
            lines.append("czerwone flagi: " + ", ".join(str(f) for f in flags))
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    return "null" if v is None else str(v)

"""Powiadomienia Telegram o okazjach.

To prywatny skrypt uruchamiany też lokalnie bez sekretów skonfigurowanych —
dlatego brak TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nigdy nie jest błędem.
Gdy Telegram nie jest skonfigurowany, wiadomość ląduje na konsoli z wyraźną
adnotacją, żeby nie zgubić informacji o okazji."""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from rich.console import Console

from . import config
from .deals import DealEvent

console = Console()

API_BASE = "https://api.telegram.org"


@dataclass
class SendResult:
    """Co się stało z próbą wysyłki — do zalogowania/wyświetlenia przez CLI."""
    ok: bool
    channel: str        # "telegram" | "console"
    detail: str = ""


def _money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _esc(s: str) -> str:
    """Telegram HTML parse_mode wymaga escapowania &, <, >."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_event(event: DealEvent) -> str:
    """Krótka, zwięzła wiadomość po polsku (HTML dla Telegrama) z linkiem."""
    place = f"{event.region} / {event.city}" if event.city else event.region
    note = f"\n<i>{_esc(event.note)}</i>" if event.note else ""

    if event.event_type == "NEW_OFFER":
        return (
            f"🆕 <b>Nowa oferta</b>: {_esc(event.hotel_name)} ({_esc(place)})\n"
            f"Cena: {_money(event.price_new)} zł/os\n"
            f"{event.url}"
        )

    if event.event_type == "PRICE_FLOOR":
        # Świadomie inny nagłówek niż przy zwykłym spadku: to nie jest
        # „taniej niż wczoraj", tylko „taniej niż kiedykolwiek".
        pct = f" ({event.pct_change:+.1f}%)" if event.pct_change is not None else ""
        return (
            f"🏷 <b>Historyczne minimum</b>: {_esc(event.hotel_name)} ({_esc(place)})\n"
            f"{_money(event.price_new)} zł/os — poniżej dotychczasowego minimum "
            f"{_money(event.price_old or 0)} zł/os{pct}{note}\n"
            f"{event.url}"
        )

    if event.event_type == "OFFER_VANISHED":
        head = ("⏳ <b>Oferta zniknęła po obniżce</b>" if event.is_sellout_signal
                else "👋 <b>Oferta zniknęła z wyników</b>")
        # Link zostaje, choć oferta wypadła z listy: strona hotelu zwykle
        # dalej działa i to jedyny sposób, żeby sprawdzić, czy naprawdę
        # nie ma już miejsc, czy tylko wypadła z pobieranego zakresu.
        return (
            f"{head}: {_esc(event.hotel_name)} ({_esc(place)})\n"
            f"Ostatnia cena: {_money(event.price_new)} zł/os{note}\n"
            f"{event.url}"
        )

    arrow = "📉" if event.event_type == "PRICE_DROP" else "📈"
    label = "Spadek ceny" if event.event_type == "PRICE_DROP" else "Wzrost ceny"
    pct = event.pct_change or 0.0
    return (
        f"{arrow} <b>{label}</b>: {_esc(event.hotel_name)} ({_esc(place)})\n"
        f"{_money(event.price_old or 0)} → {_money(event.price_new)} zł/os ({pct:+.1f}%)\n"
        f"{event.url}"
    )


class TelegramNotifier:
    """Wysyła wiadomości przez `POST https://api.telegram.org/bot<token>/sendMessage`.
    Bez skonfigurowanego tokena/chat_id nie rzuca wyjątku — działa jako
    czytelny fallback konsolowy."""

    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 timeout: float = 15.0):
        self.token = token or config.get_secret("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or config.get_secret("TELEGRAM_CHAT_ID")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> SendResult:
        """Wysyła `text` (HTML) na Telegram. Bez konfiguracji: wypisuje na
        konsolę z adnotacją i zwraca ok=True, channel="console" — wiadomość
        i tak dotarła do użytkownika, tylko innym kanałem."""
        if not self.configured:
            console.print(
                "[yellow]⚠ Telegram nieskonfigurowany "
                "(uzupełnij config/.env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) — "
                "wiadomość poniżej:[/]"
            )
            console.print(text)
            return SendResult(ok=True, channel="console", detail="brak konfiguracji Telegrama")

        url = f"{API_BASE}/bot{self.token}/sendMessage"
        try:
            r = httpx.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            body = r.json()
            if not body.get("ok"):
                return SendResult(ok=False, channel="telegram", detail=str(body.get("description")))
            return SendResult(ok=True, channel="telegram")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, channel="telegram", detail=str(exc))

    def notify_event(self, event: DealEvent) -> SendResult:
        return self.send(format_event(event))


def format_new_offers_digest(events, profile_name: str) -> str:
    """Jedna zbiorcza wiadomość zamiast osobnego pinga na każdą nową ofertę —
    przy godzinnym cyklu monitoringu lawina NEW_OFFER zabiłaby kanał."""
    top = sorted(events, key=lambda e: e.price_new)[:5]
    lines = [f"🆕 <b>{len(events)} nowych ofert</b> — {profile_name}", "Najtańsze:"]
    for e in top:
        lines.append(f'• <a href="{e.url}">{e.hotel_name}</a> ({e.region}/{e.city}) '
                     f"— {e.price_new} zł/os")
    if len(events) > 50:
        lines.append("<i>Duża liczba nowości — wygląda na pierwsze zasianie bazy; "
                     "kolejne przebiegi będą zgłaszać tylko realne nowości.</i>")
    return "\n".join(lines)


def format_vanished_digest(events, profile_name: str) -> str:
    """Zbiorcza wiadomość o ofertach, które zniknęły po obniżce.

    Zniknięcia lubią przychodzić grupami (jeden touroperator zamyka pulę na
    jeden termin), a każde z osobna byłoby serią prawie identycznych pingów.
    Do digestu trafiają wyłącznie zdarzenia przepuszczone przez
    `deals.notifiable`, czyli te ze spadkiem ceny przed zniknięciem."""
    if len(events) == 1:
        return format_event(events[0])
    lines = [f"⏳ <b>{len(events)} oferty zniknęły po obniżce</b> — {profile_name}",
             "<i>Wyprzedane ostatnie miejsca albo wycofana pula. "
             "Jeśli któryś hotel Cię interesuje, sprawdź go bezpośrednio.</i>"]
    for e in sorted(events, key=lambda e: e.pct_change or 0.0):
        pct = f" ({e.pct_change:+.1f}%)" if e.pct_change is not None else ""
        lines.append(f'• <a href="{e.url}">{_esc(e.hotel_name)}</a> '
                     f"({_esc(e.region)}) — ostatnio {_money(e.price_new)} zł/os{pct}")
    return "\n".join(lines)

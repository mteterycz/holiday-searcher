"""Lokalny dashboard webowy (faza 5) — wyłącznie odczyt danych z data/offers.db.

Zbudowany na http.server ze standardowej biblioteki (patrz docs/faza5-dashboard.md
po uzasadnienie). Moduły:

- ``server.py``        — ThreadingHTTPServer i routing po ścieżce,
- ``pages.py``         — strony (`/`, `/offers`, `/hotels`, `/drops`, `/kalendarz`, `/offer/<key>`),
- ``components.py``    — komponenty HTML/SVG (ocena z wiarygodnością, karta oferty, wykresy),
- ``data.py``          — zapytania SQL i normalizacja danych,
- ``styles.py``        — tokeny i arkusz CSS + szkielet strony,
- ``urls.py``          — adresy w dwóch trybach: serwer i eksport statyczny,
- ``static_export.py`` — `hs export`: statyczna migawka całego dashboardu,
- ``views.py``         — fasada zgodnościowa nad powyższymi.
"""

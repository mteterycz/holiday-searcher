"""Rozszerzenia CLI. Każdy moduł w tym pakiecie definiuje register(subparsers)
i dodaje własne podkomendy przez set_defaults(func=...). cli.py woła register_all."""
from __future__ import annotations

import importlib
import pkgutil


def register_all(subparsers) -> None:
    for m in pkgutil.iter_modules(__path__):
        mod = importlib.import_module(f"{__name__}.{m.name}")
        if hasattr(mod, "register"):
            mod.register(subparsers)

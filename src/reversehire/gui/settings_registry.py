"""Extension points of the settings dialog: pages and sections that feature modules add
without editing settings_dialog.py.

A feature module listed in SECTION_MODULES defines ``SECTIONS``: a list of `Section`.
Each section is a widget built by ``factory(settings, context)`` and placed on ``page``
(an existing page such as "registers", or a new one named in NEW_PAGES). The widget
offers ``values() -> dict`` (Settings field -> value as typed by the user) and may offer
``retranslate()``. ``validate(base, values) -> (changes, errors)`` turns those values into
checked Settings changes (only the fields the section owns); errors are shown and nothing
is saved.

``context`` holds what the window lends the dialog: ``window`` (the MainWindow or None),
``run`` (the background runner), ``dialogs``.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

# New pages, inserted after the page named in the second item (in this order).
NEW_PAGES: tuple[tuple[str, str], ...] = (("affinity", "search"), ("background", "affinity"))

SECTION_MODULES = (
    "reversehire.gui.affinity_settings",
    "reversehire.gui.background_settings",
    "reversehire.gui.register_settings",
)


@dataclass
class Section:
    page: str
    factory: Callable
    validate: Callable | None = None
    order: int = 100


def sections() -> list[Section]:
    """Every registered section, in page order then `order` (a module that fails to import
    is logged and skipped: the dialog must always open)."""
    found: list[Section] = []
    for name in SECTION_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                log.warning("Settings sections of %s not loaded", name, exc_info=True)
            continue
        except Exception:  # noqa: BLE001
            log.warning("Settings sections of %s not loaded", name, exc_info=True)
            continue
        found += list(getattr(module, "SECTIONS", []) or [])
    return sorted(found, key=lambda s: s.order)


def page_order(base: tuple[str, ...]) -> tuple[str, ...]:
    """The dialog's pages with NEW_PAGES inserted where they belong."""
    pages = list(base)
    for name, after in NEW_PAGES:
        if name in pages:
            continue
        index = pages.index(after) + 1 if after in pages else len(pages)
        pages.insert(index, name)
    return tuple(pages)


def validate(base, values: dict) -> tuple[dict, list[str]]:
    """Changes and errors of every section's validator."""
    changes: dict = {}
    errors: list[str] = []
    for section in sections():
        if section.validate is None:
            continue
        try:
            more, problems = section.validate(base, values)
        except Exception:  # noqa: BLE001
            log.warning("Settings validator failed", exc_info=True)
            continue
        changes.update(more or {})
        errors += list(problems or [])
    return changes, errors

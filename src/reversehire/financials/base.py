"""Shared pieces of the financial sources: identity, "why is it missing" reasons, base class.

Every user-visible text is a (key, English default) pair resolved through a `tr(key, default)`
callable supplied by the GUI; nothing here imports Qt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .strings import STRINGS

TrFunc = Callable[[str, str], str]
LEI_RE = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
QID_RE = re.compile(r"^Q[1-9][0-9]*$")


def default_tr(_key: str, default: str) -> str:
    return default


def clean(value) -> str:
    return " ".join(str(value or "").split())


@dataclass(frozen=True)
class Reason:
    """Why something is missing or was skipped; shown to the owner next to the chart."""

    key: str
    default: str
    params: tuple = ()  # ((name, value), ...)  frozen so reasons can be compared

    def text(self, tr: TrFunc | None = None) -> str:
        text = (tr or default_tr)(self.key, self.default)
        if self.params:
            try:
                return text.format(**dict(self.params))
            except (KeyError, IndexError, ValueError):
                return text
        return text


def reason(key: str, default: str | None = None, **params) -> Reason:
    """A Reason whose English text comes from strings.STRINGS (unless `default` is given)."""
    return Reason(key, STRINGS.get(key, key) if default is None else default,
                  tuple(sorted(params.items())))


@dataclass
class FinancialsIdentity:
    """What the sources need to know about a company (all optional; blank = unknown)."""

    name: str = ""
    lei: str = ""
    vat: str = ""        # partita IVA, with or without the "IT" prefix
    tax_code: str = ""   # codice fiscale
    wikidata: str = ""   # "Q123"
    country: str = "IT"

    def __post_init__(self) -> None:
        self.name = clean(self.name)
        self.lei = clean(self.lei).upper()
        self.vat = clean(self.vat).replace(" ", "").upper()
        self.tax_code = clean(self.tax_code).replace(" ", "").upper()
        self.wikidata = clean(self.wikidata).upper()
        self.country = clean(self.country).upper() or "IT"

    @property
    def valid_lei(self) -> str:
        return self.lei if LEI_RE.match(self.lei) else ""

    @property
    def valid_qid(self) -> str:
        return self.wikidata if QID_RE.match(self.wikidata) else ""

    @property
    def vat_digits(self) -> str:
        """The 11 digits of an Italian partita IVA (without "IT"), or ""."""
        digits = self.vat[2:] if self.vat.startswith("IT") else self.vat
        return digits if re.fullmatch(r"[0-9]{11}", digits) else ""

    @classmethod
    def from_card(cls, card) -> "FinancialsIdentity":
        """From a CompanyCard (duck-typed: legal_name/name, vat, tax_code and the registry
        values "lei" and "wikidata")."""
        def registry(name: str) -> str:
            getter = getattr(card, "registry_value", None)
            if callable(getter):
                try:
                    return clean(getter(name))
                except Exception:
                    return ""
            return ""

        return cls(name=getattr(card, "legal_name", "") or getattr(card, "name", ""),
                   lei=registry("lei"), vat=getattr(card, "vat", ""),
                   tax_code=getattr(card, "tax_code", ""), wikidata=registry("wikidata"))

    @classmethod
    def from_company(cls, company) -> "FinancialsIdentity":
        """From a models.Company (its `ids` dict)."""
        ids = getattr(company, "ids", None) or {}
        return cls(name=getattr(company, "name", ""), lei=ids.get("lei", ""), vat=ids.get("vat", ""),
                   tax_code=ids.get("tax_code", ""), wikidata=ids.get("wikidata", ""))


class SourceError(Exception):
    """A source could not answer (network, blocked, unreadable answer)."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


SOURCE_CLASSES: dict[str, type] = {}  # name -> FinancialSource subclass (see register_source)


def register_source(cls):
    """Class decorator: makes a FinancialSource subclass known to the app. A source with
    `automatic = True` (and not `paid`) is run on every card build by
    `collect.automatic_sources()`; its `authority` (if set) enters model.AUTHORITY, so the
    merge rules rank it. Nothing in the GUI has to change: the card shows any source by
    its name (text key "fin.source.<name>", falling back to the name itself)."""
    from .model import AUTHORITY

    SOURCE_CLASSES[cls.name] = cls
    if getattr(cls, "authority", None) is not None:
        AUTHORITY.setdefault(cls.name, int(cls.authority))
    return cls


class FinancialSource:
    """Base class: `fetch(identity) -> FinancialSeries | None`.

    `fetch` returns None when the source has nothing for this company and then sets
    `last_reason` (a Reason); it raises SourceError / any exception on failures (the
    collector catches them). `paid` sources are never run unless the caller allows it.
    `gateway` is the polite gateway of the discovery layer (anything with
    `get(url, params=..., headers=..., cache_ttl_s=...)` returning an object with
    `status`, `text` and `json()`); `collect_financials` binds it when not set.
    """

    name = "source"
    paid = False
    needs_network = True
    automatic = False        # run on every card build (free sources only)
    authority: int | None = None  # merge rank for model.AUTHORITY (None: already listed there)

    def __init__(self, gateway=None):
        self.gateway = gateway
        self.last_reason: Reason | None = None
        self.requests = 0

    def bind(self, gateway) -> None:
        if self.gateway is None:
            self.gateway = gateway

    def fetch(self, identity: FinancialsIdentity):  # pragma: no cover - interface
        raise NotImplementedError

    # helpers
    def _get(self, url: str, **kwargs):
        if self.gateway is None:
            raise SourceError("no_gateway", "no HTTP gateway given")
        self.requests += 1
        return self.gateway.get(url, **kwargs)

    def _json(self, url: str, **kwargs):
        reply = self._get(url, **kwargs)
        status = getattr(reply, "status", 0)
        if status == 404:
            return None
        if status != 200:
            raise SourceError("http", f"HTTP {status}")
        try:
            return reply.json()
        except ValueError as error:
            raise SourceError("bad_answer", "not JSON") from error

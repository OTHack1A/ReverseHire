"""Runs the financial sources politely and explains what is missing.

`collect_financials(identity, sources, gateway)` never raises: every failure is logged on
the logger "reversehire.financials" and returned in the CollectResult. Paid sources are
skipped unless the caller passes `allow_paid=True` (they cost money per call).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .base import FinancialSource, FinancialsIdentity, Reason, TrFunc, default_tr, reason
from .model import FinancialSeries, authority_of

log = logging.getLogger("reversehire.financials")

NOT_LISTED = reason(
    "fin.why.not_listed")
NETWORK = reason(
    "fin.why.network")
PAID_SKIPPED = reason(
    "fin.why.paid_skipped")


@dataclass
class SourceReport:
    source: str
    state: str                     # "ok" | "empty" | "skipped" | "error" | "cancelled"
    reason: Reason | None = None
    detail: str = ""               # short technical detail (never contains keys)
    years: int = 0
    requests: int = 0


@dataclass
class CollectResult:
    series: FinancialSeries = field(default_factory=FinancialSeries)
    reports: list[SourceReport] = field(default_factory=list)
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.series.empty

    @property
    def errors(self) -> list[SourceReport]:
        return [r for r in self.reports if r.state == "error"]

    @property
    def requests(self) -> int:
        return sum(r.requests for r in self.reports)

    def reasons(self) -> list[Reason]:
        """Why data is missing, most important first; [] when there is data."""
        if self.ok:
            return []
        network = [r for r in self.reports if r.state == "error" and r.reason is not None
                   and r.reason.key == NETWORK.key]
        errors = [r for r in self.reports if r.state == "error"]
        ran = [r for r in self.reports if r.state != "skipped"]
        out: list[Reason] = []
        if network and len(network) == len(errors) == len(ran):
            out.append(NETWORK)
        else:
            if any(r.reason is not None and r.reason.key in ("fin.why.no_lei", "fin.why.no_esef")
                   for r in self.reports if r.source == "esef") and not any(
                       r.state == "error" for r in self.reports if r.source == "esef"):
                out.append(NOT_LISTED)
            for report in self.reports:
                if report.reason is not None and report.reason not in out:
                    out.append(report.reason)
        if any(r.state == "skipped" and r.reason == PAID_SKIPPED for r in self.reports) \
                and PAID_SKIPPED not in out:
            out.append(PAID_SKIPPED)
        return out

    def why_missing(self, tr: TrFunc | None = None) -> str:
        """Text for the chart's empty state ("" when there is data)."""
        tr = tr or default_tr
        lines = []
        for item in self.reasons():
            text = item.text(tr)
            if text and text not in lines:
                lines.append(text)
        return "\n".join(lines)

    def notes(self, tr: TrFunc | None = None) -> list[str]:
        """Cautions to show next to a chart that HAS data."""
        tr = tr or default_tr
        out = []
        sources = {r.source.split(":")[0] for r in self.series.records.values()}
        if sources == {"wikidata"}:
            out.append(reason("fin.note.low_authority").text(tr))
        if self.series.conflicts:
            out.append(reason("fin.note.conflicts").text(tr))
        if len(self.series.scopes()) > 1:
            out.append(reason("fin.note.scopes").text(tr))
        return out


def automatic_sources() -> list[FinancialSource]:
    """A fresh instance of every registered free source marked `automatic` (base.register_source),
    in registration order: what a card build runs. A new source module only has to be
    imported by the package `__init__` to be included."""
    from . import base

    sources = []
    for cls in base.SOURCE_CLASSES.values():
        if getattr(cls, "automatic", False) and not getattr(cls, "paid", False):
            try:
                sources.append(cls())
            except Exception:  # noqa: BLE001 - a source that cannot start is skipped, logged
                log.warning("Financials: source %s not created", getattr(cls, "name", cls), exc_info=True)
    return sources


def _is_network_error(error: BaseException) -> bool:
    from ..discovery.gateway import GatewayError

    return isinstance(error, GatewayError)


def collect_financials(identity: FinancialsIdentity, sources, gateway=None, *,
                       allow_paid: bool = False, series: FinancialSeries | None = None,
                       authority: dict | None = None) -> CollectResult:
    """Runs `sources` (each `fetch(identity) -> FinancialSeries | None`) and merges what they
    return into `series` (a copy of the stored one, if given). Never raises."""
    from ..discovery.gateway import Cancelled

    result = CollectResult(series=series if series is not None else FinancialSeries())
    ordered = sorted(sources or [], key=lambda s: -authority_of(getattr(s, "name", ""), authority))
    for source in ordered:
        name = str(getattr(source, "name", type(source).__name__))
        report = SourceReport(name, "empty")
        result.reports.append(report)
        if result.cancelled:
            report.state = "cancelled"
            continue
        if getattr(source, "paid", False) and not allow_paid:
            report.state, report.reason = "skipped", PAID_SKIPPED
            continue
        try:
            if getattr(source, "needs_network", True) and gateway is not None and hasattr(source, "bind"):
                source.bind(gateway)
            before = getattr(source, "requests", 0)
            got = source.fetch(identity)
            report.requests = getattr(source, "requests", 0) - before
        except Cancelled:
            report.state, result.cancelled = "cancelled", True
            log.info("Financials: %s cancelled", name)
            continue
        except Exception as error:  # noqa: BLE001 - the collector must never raise
            kind = getattr(error, "kind", type(error).__name__)
            report.state = "error"
            report.detail = f"{type(error).__name__}: {error}"[:200]
            report.requests = getattr(source, "requests", 0)
            if _is_network_error(error) or kind in ("unavailable", "http", "no_gateway"):
                report.reason = NETWORK
            elif kind == "unauthorized":
                report.reason = reason("fin.why.unauthorized")
            else:
                report.reason = reason("fin.why.source_error",
                                       source=name)
            log.warning("Financials: source %s failed (%s)", name, report.detail)
            continue
        if got is None or got.empty:
            report.reason = getattr(source, "last_reason", None)
            continue
        result.series.merge(got, authority)
        report.state, report.years = "ok", len(got.years())
    log.info("Financials: %d years, %d conflicts, %d requests", len(result.series.years()),
             len(result.series.conflicts), result.requests)
    return result

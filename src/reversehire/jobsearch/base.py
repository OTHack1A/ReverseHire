"""Base class of the job sources and the per-source context given by the pipeline.

Every request goes through the app's `PoliteGateway` (discovery/gateway.py): pause per
host, identifying User-Agent, back-off on 429, cancellable waits. A source never raises
to the pipeline: it records errors in its report and returns what it found so far.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from ._util import CancelCheck, log
from .cache import DAY_S, JobSearchCache, default_cache
from .models import Posting, SourceReport


class SourceCancelled(Exception):
    """Raised inside a source when the search was cancelled or timed out."""


@dataclass
class SourceContext:
    gateway: object
    query: object
    cache: JobSearchCache = field(default_factory=default_cache)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    progress: object = None              # callable(message) -> None, one step done
    place_lookup: object = None
    today: date | None = None
    report: SourceReport | None = None
    found: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check(self) -> None:
        if self.cancelled():
            raise SourceCancelled()

    def step(self, message: str = "") -> None:
        if self.progress is not None:
            try:
                self.progress(message)
            except Exception:
                pass

    def add(self, postings) -> None:
        with self._lock:
            self.found.extend(postings)

    def error(self, message: str) -> None:
        if self.report is not None:
            with self._lock:
                if len(self.report.errors) < 20:
                    self.report.errors.append(message)
        log.info("Job source %s: %s", getattr(self.report, "source_id", "?"), message)

    def count_request(self, from_cache: bool = False) -> None:
        if self.report is not None:
            with self._lock:
                if from_cache:
                    self.report.cache_hits += 1
                else:
                    self.report.requests += 1

    def request(self, method: str, url: str, **kwargs):
        """One request through the gateway (cancellable). Raises the gateway errors."""
        from ..discovery.gateway import Cancelled

        self.check()
        try:
            reply = self.gateway.request(method, url, cancel=self.cancel_event, **kwargs)
        except Cancelled as error:
            raise SourceCancelled() from error
        self.count_request(getattr(reply, "from_cache", False))
        return reply

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    # --- robots.txt

    def robots_allows(self, url: str) -> bool:
        """robots.txt of the host (cached a day); unreachable or missing = allowed."""
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        cached = self.cache.get("robots", root, DAY_S)
        if cached is None:
            text = ""
            try:
                reply = self.get(root + "/robots.txt")
                text = reply.text if reply.status == 200 else ""
            except SourceCancelled:
                raise
            except Exception as error:
                log.info("robots.txt unavailable for %s: %s", parts.netloc, type(error).__name__)
            if "<html" in text[:500].lower():
                text = ""  # an HTML page instead of robots.txt: no rules
            cached = {"text": text}
            self.cache.put("robots", root, cached)
        parser = RobotFileParser()
        parser.parse(cached.get("text", "").splitlines())
        try:
            from ..discovery.gateway import user_agent

            agent = user_agent()
        except Exception:
            agent = "ReverseHire"
        return parser.can_fetch(agent, url)


class JobSource:
    """A place where postings come from. Subclasses implement `run(ctx)`."""

    id: str = "source"
    label: str = "Source"
    needs_key: bool = False
    enabled_by_default: bool = True
    allowed_by_terms: bool = True     # False: can never be enabled (terms forbid automated reading)
    terms_verdict: str = "not_addressed"  # "allowed" | "not_addressed" | "harmful_automation_only" | "forbidden" | "api_terms"
    terms_note: str = ""
    terms_url: str = ""
    terms_checked: str = ""
    attribution: str | None = None
    hosts: tuple[str, ...] = ()

    def has_key(self) -> bool:
        return True

    def estimate(self, query) -> int:
        """Estimated progress steps of one search."""
        return 1

    def run(self, ctx: SourceContext) -> list[Posting]:
        raise NotImplementedError

    def search(self, query, gateway, progress=None, cancel=None, *, cache: JobSearchCache | None = None,
               place_lookup=None, today: date | None = None) -> list[Posting]:
        """Stand-alone search (the pipeline uses `run` with its own context). Never raises."""
        report = SourceReport(self.id)
        is_cancelled = CancelCheck(cancel)
        event = threading.Event()
        done = [0]
        total = max(1, self.estimate(query))

        def step(message: str) -> None:
            done[0] += 1
            if is_cancelled():
                event.set()
            if progress is not None:
                try:
                    progress(min(done[0], total), total, message)
                except Exception:
                    pass

        ctx = SourceContext(gateway=gateway, query=query, cache=cache or default_cache(), cancel_event=event,
                            progress=step, place_lookup=place_lookup, today=today or date.today(),
                            report=report)
        if is_cancelled():
            return []
        started = time.monotonic()
        try:
            result = self.run(ctx)
        except SourceCancelled:
            result = list(ctx.found)
        except Exception as error:
            ctx.error(f"{type(error).__name__}")
            result = list(ctx.found)
        report.seconds = round(time.monotonic() - started, 2)
        self.last_report = report
        return result

    def describe(self) -> dict:
        """Static facts for the settings page."""
        return {"id": self.id, "label": self.label, "needs_key": self.needs_key,
                "enabled_by_default": self.enabled_by_default, "allowed_by_terms": self.allowed_by_terms,
                "terms_verdict": self.terms_verdict, "terms_note": self.terms_note, "terms_url": self.terms_url,
                "terms_checked": self.terms_checked, "attribution": self.attribution, "hosts": list(self.hosts)}

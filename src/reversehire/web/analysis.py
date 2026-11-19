"""Analysis of a company website: is it hiring? What does it do?

Posting extraction cascade: ATS -> JSON-LD -> heuristics on the careers page
-> open-application signal only.

Careers page discovery: links (text and path), then the most common paths,
then the careers URLs listed in the sitemap declared in robots.txt. When a
careers page exists but shows nothing readable and no ATS link, the ATS boards
named like the domain are probed (`ats.probe_ats`, a few API calls; a probed
board is accepted only when its data names the site's domain).
"""

import logging
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..config import Settings
from ..models import Posting, HiringStatus
from .ats import ATSReference, detect_ats, fetch_board, probe_ats
from .careers import (
    careers_urls_from_sitemap,
    extract_heuristic_postings,
    page_language,
    sort_by_listing,
    common_paths,
    open_application_signal,
    text_looks_like_careers,
    find_careers_link,
    find_about_us_link,
    url_looks_like_careers,
)
from .jsonld import extract_jobposting
from .posting_attrs import complete_posting
from .fetch import Page, Fetcher
from .text import analyze_html, url_key, normalize

log = logging.getLogger(__name__)

MAX_COMPANY_TEXT = 20000
MAX_ATS_CALLS = 2
MAX_COMMON_PATHS = 2
MAX_SITEMAPS = 1  # sitemap files read per site (each one counts as a page)


@dataclass
class SiteOutcome:
    status: HiringStatus
    postings: list[Posting] = field(default_factory=list)
    company_text: str = ""
    pages_read: int = 0
    careers_url: str | None = None
    postings_source: str = ""  # "ats:greenhouse", "jsonld", "heuristic", "unsolicited", ""


class _Visit:
    """Counts the pages requested from a site and avoids re-reading them."""

    def __init__(self, fetcher: Fetcher, maximum: int, cancel: threading.Event | None):
        self._fetcher = fetcher
        self._maximum = max(1, maximum)
        self._cancel = cancel
        self.attempts = 0
        self.pages_done = 0
        self._seen: set[str] = set()

    def cancelled(self) -> bool:
        return self._cancel is not None and self._cancel.is_set()

    def can_read(self) -> bool:
        return self.attempts < self._maximum and not self.cancelled()

    def already_seen(self, url: str) -> bool:
        return url_key(url) in self._seen

    def read(self, url: str) -> Page | None:
        if self.already_seen(url) or not self.can_read():
            return None
        self._seen.add(url_key(url))
        self.attempts += 1
        page = self._fetcher.fetch_page(url)
        if page is not None:
            self.pages_done += 1
            self._seen.add(url_key(page.final_url))
        return page


def _normalize_site_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url.lstrip("/")
    if url and not urlsplit(url).path:
        url += "/"
    return url


def _unique_ats(refs: list[ATSReference]) -> list[ATSReference]:
    seen: set[tuple[str, str]] = set()
    result = []
    for ref in refs:
        key = (ref.platform, ref.slug.lower())
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return result


def remove_duplicates(postings: list[Posting]) -> list[Posting]:
    """Drops repeated postings (same detail URL, or same title and location)."""
    result: list[Posting] = []
    seen_urls: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    generic_urls = {
        url_key(a.url) for a in postings
        if sum(1 for b in postings if url_key(b.url) == url_key(a.url)) > 1
    }
    for posting in postings:
        title_key = (normalize(posting.title), normalize(posting.location))
        key = url_key(posting.url)
        if title_key in seen_titles:
            continue
        if key not in generic_urls:
            if key in seen_urls:
                continue
            seen_urls.add(key)
        seen_titles.add(title_key)
        result.append(posting)
    return result


class _Analysis:
    def __init__(self, url: str, fetcher: Fetcher, settings: Settings, cancel,
                 roles: list[str] | None = None):
        self.url = _normalize_site_url(url)
        self.fetcher = fetcher
        self.roles = [r for r in roles or [] if r]
        self.visit = _Visit(fetcher, settings.max_pages_per_site, cancel)
        self.postings: list[Posting] = []
        self.source = ""
        self.careers_url: str | None = None
        self.careers_pages: list[Page] = []
        self.ats: list[ATSReference] = []
        self.tried_ats: set[tuple[str, str]] = set()

    # --- cascade steps ---

    def _try_ats(self) -> None:
        for ref in _unique_ats(self.ats):
            key = (ref.platform, ref.slug.lower())
            if key in self.tried_ats or len(self.tried_ats) >= MAX_ATS_CALLS:
                continue
            if self.visit.cancelled():
                return
            self.tried_ats.add(key)
            found = fetch_board(ref, self.fetcher, self.roles or None)
            if found is None:
                continue
            ref, postings = found
            self.careers_url = self.careers_url or ref.board_url
            if postings:
                self.postings = postings
                self.source = f"ats:{ref.platform}"
                return

    def _cascade(self, pages: list[Page], heuristic: bool = True) -> None:
        """ATS -> JSON-LD -> heuristics on the given pages."""
        for page in pages:
            self.ats.extend(detect_ats(page.html, page.final_url))
        self._try_ats()
        if self.postings:
            return
        matches: list[Posting] = []
        for page in pages:
            matches.extend(extract_jobposting(page.html, page.final_url))
        if matches:
            self.postings, self.source = matches, "jsonld"
            return
        if heuristic:
            for page in pages:
                matches.extend(extract_heuristic_postings(page.html, page.final_url))
            if matches:
                self.postings, self.source = matches, "heuristic"

    def _find_careers_page(self, home: Page, home_tree) -> list:
        base = home.final_url
        if url_looks_like_careers(base):
            self.careers_pages.append(home)
            self.careers_url = base
        candidates = find_careers_link(home.html, base, home_tree)
        for candidate in candidates:
            if candidate.ats:
                self.ats.extend(detect_ats(candidate.url))
                self.careers_url = self.careers_url or candidate.url
        internal = [c for c in candidates if not c.ats]
        if not self.careers_pages:
            for candidate in internal[:2]:
                page = self.visit.read(candidate.url)
                if page is not None:
                    self.careers_pages.append(page)
                    self.careers_url = page.final_url
                    break
        if not candidates and not self.careers_pages and not self.ats:
            # Common paths, only if the links gave nothing.
            home_key = url_key(base)
            # Only the most likely ones for the language: every attempt is an extra
            # request to the site and an extra wait for the user.
            for url in common_paths(base, page_language(home.html, home_tree))[:MAX_COMMON_PATHS]:
                if not self.visit.can_read():
                    break
                page = self.visit.read(url)
                if page is None or url_key(page.final_url) == home_key:
                    continue
                if text_looks_like_careers(page.text) or detect_ats(page.html):
                    self.careers_pages.append(page)
                    self.careers_url = page.final_url
                    break
            if not self.careers_pages:
                self._careers_from_sitemap(base)
        return candidates

    def _careers_from_sitemap(self, base: str) -> None:
        """Careers page listed in the sitemap declared by robots.txt (one sitemap file)."""
        sitemaps = getattr(self.fetcher, "sitemaps", None)
        if sitemaps is None:
            return
        try:
            declared = sitemaps(base)
        except Exception:
            return
        for sitemap in declared[:MAX_SITEMAPS]:
            if not self.visit.can_read():
                return
            self.visit.attempts += 1  # the sitemap counts as a page of the site
            xml = self.fetcher.download_data(sitemap)
            for url in careers_urls_from_sitemap(xml or "", base)[:1]:
                page = self.visit.read(url)
                if page is not None and (text_looks_like_careers(page.text) or detect_ats(page.html)
                                         or url_looks_like_careers(page.final_url)):
                    self.careers_pages.append(page)
                    self.careers_url = page.final_url
                    return

    def _probe(self) -> None:
        """Boards named like the domain, when a careers section exists but gave nothing."""
        if self.visit.cancelled():
            return
        found = probe_ats(self.url, self.fetcher)
        if found is None:
            return
        ref, postings = found
        self.postings, self.source = postings, f"ats:{ref.platform}"
        self.careers_url = self.careers_url or ref.board_url

    def _dig_deeper(self) -> None:
        """From the careers page, follows a link to a list of positions (e.g. "posizioni aperte")."""
        if not self.careers_pages or not self.visit.can_read():
            return
        page = self.careers_pages[-1]
        for candidate in sort_by_listing(find_careers_link(page.html, page.final_url)):
            if candidate.ats:
                self.ats.extend(detect_ats(candidate.url))
                continue
            if self.visit.already_seen(candidate.url):
                continue
            new_page = self.visit.read(candidate.url)
            if new_page is not None:
                self.careers_pages.append(new_page)
                self._cascade([new_page])
            return
        self._try_ats()

    def run(self) -> SiteOutcome:
        if not self.url or self.visit.cancelled():
            return SiteOutcome(HiringStatus.NOT_ASSESSED)
        home = self.visit.read(self.url)
        if home is None:
            status = HiringStatus.NOT_ASSESSED if self.visit.cancelled() else HiringStatus.UNREACHABLE
            return SiteOutcome(status, pages_read=self.visit.pages_done)

        home_tree = analyze_html(home.html)
        self.ats.extend(detect_ats(home.html, home.final_url))
        candidates = self._find_careers_page(home, home_tree)
        about_link = find_about_us_link(home.html, home.final_url, home_tree)

        pages = self.careers_pages + ([home] if home not in self.careers_pages else [])
        self._cascade(pages, heuristic=False)
        if not self.postings and self.careers_pages:
            self._cascade(self.careers_pages, heuristic=True)
        if not self.postings:
            self._dig_deeper()
        if (not self.postings and not self.ats and (self.careers_pages or candidates)
                and not any(c.ats for c in candidates)):
            self._probe()

        texts = [home.text]
        if about_link and self.visit.can_read():
            for url in about_link[:1]:
                page = self.visit.read(url)
                if page is not None:
                    texts.append(page.text)

        return self._outcome(home, candidates, texts)

    def _outcome(self, home: Page, candidates: list, texts: list[str]) -> SiteOutcome:
        company_text = "\n\n".join(t for t in texts if t)[:MAX_COMPANY_TEXT]
        # Every posting gets the attributes detectable from its own fields; the
        # producers have already filled them from structured data and full text.
        postings = [complete_posting(p) for p in remove_duplicates(self.postings)]
        if postings:
            return SiteOutcome(
                HiringStatus.HIRING, postings, company_text, self.visit.pages_done,
                self.careers_url, self.source,
            )
        unsolicited = any(open_application_signal(p.text) for p in self.careers_pages) or open_application_signal(home.text)
        invitation = bool(candidates) or bool(self.careers_pages) or self.careers_url is not None
        if unsolicited or invitation:
            return SiteOutcome(
                HiringStatus.MAYBE, [], company_text, self.visit.pages_done,
                self.careers_url, "unsolicited" if unsolicited else "",
            )
        return SiteOutcome(HiringStatus.NO_SIGNAL, [], company_text, self.visit.pages_done, None, "")


def analyze_site(
    url: str,
    fetcher: Fetcher,
    settings: Settings,
    cancel: threading.Event | None = None,
    *,
    roles: list[str] | None = None,
) -> SiteOutcome:
    """Hiring status, postings and descriptive text of a company website.

    Reads at most `settings.max_pages_per_site` pages of the site (calls to the
    ATS APIs, on other hosts, do not count and are limited to two). When
    `cancel` is set it stops and returns what was collected so far
    (NOT_ASSESSED if the home page was not read). `roles` (the candidate's
    role names, optional) decide which postings of a large ATS board get their
    full text first (see `ats.fetch_board`).
    """
    try:
        return _Analysis(url, fetcher, settings, cancel, roles).run()
    except Exception as error:  # never crash because of a strange site
        log.warning("Site analysis failed (%s): %s", type(error).__name__, urlsplit(str(url)).netloc)
        return SiteOutcome(HiringStatus.UNREACHABLE)

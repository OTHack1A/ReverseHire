"""mailto: links (RFC 6068) and the Gmail web compose link, with a length limit.

Neither can carry an attachment. When the whole URL would be too long, the body is
shortened at a word boundary and ``truncated`` is set: the caller then puts the full
text in the clipboard.
"""

from dataclasses import dataclass
from urllib.parse import quote

from reversehire.mail.compose import Draft

# Kept below the ~2 000 characters that Windows hands reliably to a mailto handler.
MAILTO_MAX_LENGTH = 2000
# The Gmail compose URL is opened in the browser; Google's front end refuses very long
# URLs (undocumented limit), so stay well below 8 KB.
GMAIL_MAX_LENGTH = 7500
GMAIL_COMPOSE_BASE = "https://mail.google.com/mail/"
ELLIPSIS = "…"


@dataclass(frozen=True)
class ComposeUrl:
    url: str
    truncated: bool = False
    body_used: str = ""  # the body actually in the URL (shortened when truncated)


def _normalise_newlines(textual: str) -> str:
    return (textual or "").replace("\r\n", "\n").replace("\r", "\n")


def _enc(value: str) -> str:
    """Percent-encoding of UTF-8; spaces as %20 (never "+"), "&", "=", "#", "?" encoded."""
    return quote(value, safe="")


def _mailto_from(draft: Draft, body: str) -> str:
    to = quote((draft.to or "").strip(), safe="@,")
    params = []
    if draft.subject:
        params.append("subject=" + _enc(draft.subject))
    if body:
        params.append("body=" + _enc(body.replace("\n", "\r\n")))
    return "mailto:" + to + ("?" + "&".join(params) if params else "")


def _gmail_from(draft: Draft, body: str, authuser: int | None) -> str:
    params = ["view=cm", "fs=1"]
    if authuser is not None:
        params.append(f"authuser={int(authuser)}")
    if draft.to:
        params.append("to=" + quote(draft.to.strip(), safe="@,"))
    if draft.subject:
        params.append("su=" + _enc(draft.subject))
    if body:
        params.append("body=" + _enc(body))
    return GMAIL_COMPOSE_BASE + "?" + "&".join(params)


def _shorten(body: str, build, max_length: int) -> ComposeUrl:
    full = build(body)
    if len(full) <= max_length:
        return ComposeUrl(full, False, body)
    # Longest prefix (in characters) whose URL still fits, then back to a word boundary.
    low, high = 0, len(body)
    while low < high:
        middle = (low + high + 1) // 2
        if len(build(body[:middle].rstrip() + ELLIPSIS)) <= max_length:
            low = middle
        else:
            high = middle - 1
    prefix = body[:low]
    cut = max(prefix.rfind(" "), prefix.rfind("\n"))
    if cut > low // 2:
        prefix = prefix[:cut]
    shortened = (prefix.rstrip() + ELLIPSIS) if prefix.strip() else ""
    url = build(shortened)
    if len(url) > max_length:  # even subject and recipient alone are too long
        shortened = ""
        url = build("")
    return ComposeUrl(url, True, shortened)


def mailto_url(draft: Draft, max_length: int = MAILTO_MAX_LENGTH) -> ComposeUrl:
    body = _normalise_newlines(draft.body)
    return _shorten(body, lambda b: _mailto_from(draft, b), max_length)


def gmail_compose_url(draft: Draft, max_length: int = GMAIL_MAX_LENGTH,
                      authuser: int | None = None) -> ComposeUrl:
    """``https://mail.google.com/mail/?view=cm&fs=1&to=…&su=…&body=…`` (newlines %0A).
    ``authuser`` picks the n-th signed-in Google account (0 = first)."""
    body = _normalise_newlines(draft.body)
    return _shorten(body, lambda b: _gmail_from(draft, b, authuser), max_length)

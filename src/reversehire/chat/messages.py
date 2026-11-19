"""Texts of the chat backend: i18n keys with their English text.

The keys are meant to be added to ``locales/*.json`` by the integrator. Until a
key exists there, :func:`text` returns the English text below, so the backend
never shows a bare key.
"""

from __future__ import annotations

ENGLISH: dict[str, str] = {
    # --- CLI detection -----------------------------------------------------------------
    "chat.cli.not_installed": "{cli} was not found on this computer. Install it, or set its path in the settings.",
    "chat.cli.path_missing": "The path set for {cli} does not exist: {path}",
    "chat.cli.not_runnable": "{cli} was found but does not start: {detail}",
    "chat.cli.not_logged_in": ("{cli} is installed but not signed in. Open a terminal, run {login_command} "
                               "and sign in, then try again."),
    "chat.cli.login_unknown": "{cli} {version} was found, but its sign-in state could not be checked.",
    "chat.cli.ready": "{cli} {version} is ready.",
    # --- MCP server ----------------------------------------------------------------------
    "chat.mcp.ready": "The ReverseHire MCP server can be started.",
    "chat.mcp.disabled": ("MCP access is switched off: the assistant cannot read your ReverseHire data. "
                          "Turn it on with the dot at the top right of the window."),
    "chat.mcp.package_missing": "The 'mcp' package is not installed for {python}. Install it with: pip install mcp",
    "chat.mcp.import_failed": "The ReverseHire MCP server cannot be loaded by {python}: {detail}",
    "chat.mcp.python_missing": "The Python interpreter for the MCP server was not found: {python}",
    # --- errors of a question -------------------------------------------------------------
    "chat.error.busy": "The assistant is still answering the previous question.",
    "chat.error.empty": "Write a question first.",
    "chat.error.start_failed": "{cli} could not be started: {detail}",
    "chat.error.timeout": "{cli} did not finish within {seconds} seconds and was stopped.",
    "chat.error.cancelled": "Stopped.",
    "chat.error.not_logged_in": ("{cli} is not signed in. Open a terminal, run {login_command} and sign in, "
                                 "then try again."),
    "chat.error.cli_failed": "{cli} stopped with an error (exit code {code}).",
    "chat.error.usage_limit": "Your {cli} plan has reached its usage limit; try again after {when}. Nothing was charged by ReverseHire.",
    "chat.error.usage_limit_no_time": "Your {cli} plan has reached its usage limit; try again later (the plan's page shows when it resets). Nothing was charged by ReverseHire.",
    "chat.error.cli_reported": "{cli} reported an error: {detail}",
    "chat.error.no_answer": "{cli} finished without an answer.",
    "chat.error.unsafe": ("{cli} was stopped because it started with permissions or tools that ReverseHire "
                          "does not allow ({detail})."),
    "chat.error.unsafe_args": ("A path passed to {cli} contains characters that cannot be passed safely "
                               "to a .cmd launcher: {detail}"),
    "chat.error.internal": "Unexpected error in the chat: {detail}",
    # --- notices during a question ---------------------------------------------------------
    "chat.notice.mcp_failed": ("The ReverseHire MCP server did not start in {cli} ({detail}): the answer "
                               "cannot use your data."),
    "chat.notice.retrying": "{cli} is retrying the request ({detail}).",
    "chat.notice.usage": "Questions are answered by your installed {cli} and count against your own plan.",
}

CLI_NAMES = {"claude": "Claude Code", "codex": "Codex"}
LOGIN_COMMANDS = {"claude": "claude (then /login)", "codex": "codex login"}


def text(key: str, **params) -> str:
    """Translated text of `key` (interface language), English when not translated yet."""
    template = None
    try:
        from .. import i18n

        translated = i18n.t(key)
        if translated and translated != key:
            template = translated
    except Exception:  # i18n missing or broken: English is always available
        template = None
    if template is None:
        template = ENGLISH.get(key, key)
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template

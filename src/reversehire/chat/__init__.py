"""Backend of the in-app chat: talks to the user's installed Claude Code or Codex CLI,
which answers from ReverseHire's own MCP server. No GUI imports.

Typical use (on a worker thread for the detection, which runs commands)::

    from reversehire import chat, config

    clis = chat.detect_all({"codex": settings_path_or_None})
    mcp = chat.check_mcp(enabled=config.mcp_enabled())
    session = chat.ChatSession(clis["claude"], data_dir=config.data_folder(), language="it",
                               callbacks=chat.ChatCallbacks(on_text=..., on_tool=..., on_done=...,
                                                            on_error=..., on_notice=...))
    session.send("Which companies did my last search find?")
    ...
    session.cancel()      # stop the running answer
    session.reset()       # new conversation
"""

from .commands import (CLAUDE_ALLOWED_TOOLS, McpLaunch, build_claude_command, build_codex_command,
                       claude_mcp_config, codex_prompt, preamble)
from .detect import (BROKEN, CLAUDE, CODEX, FOUND, NOT_INSTALLED, NOT_LOGGED_IN, READY, CliInfo,
                     check_cli, detect_all, find_cli)
from .events import ChatAnswer, ChatError, Notice, ToolNotice
from .mcp_status import McpStatus, check_mcp
from .messages import ENGLISH as MESSAGES_EN
from .session import ChatCallbacks, ChatSession

__all__ = [
    "BROKEN", "CLAUDE", "CODEX", "FOUND", "NOT_INSTALLED", "NOT_LOGGED_IN", "READY",
    "CLAUDE_ALLOWED_TOOLS", "ChatAnswer", "ChatCallbacks", "ChatError", "ChatSession", "CliInfo",
    "McpLaunch", "McpStatus", "MESSAGES_EN", "Notice", "ToolNotice", "build_claude_command",
    "build_codex_command", "check_cli", "check_mcp", "claude_mcp_config", "codex_prompt",
    "detect_all", "find_cli", "preamble",
]

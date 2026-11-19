"""Application entry point (script `reversehire`, `python -m reversehire`)."""

import logging
import os
import sys

from . import __version__, config, i18n, applog

log = logging.getLogger(__name__)


def _install_exception_handlers() -> None:
    """Unhandled exceptions (any thread, any Qt slot) and Qt's own messages go to the
    log file, never to the user's terminal; the window shows a non-blocking notice in
    its status bar (gui.safety), never a modal box and never a dead window."""
    from .gui import safety

    safety.install()


def log_data_location() -> config.DataLocation:
    """Writes to the log which data folder is used and why (and the warnings about it)."""
    location = config.data_location()
    folder = location.folder.resolve()
    log.info("Data folder %s (%s)", folder, location.reason)
    if location.program_folder is not None:
        log.info("Program folder %s", location.program_folder)
    if location.migrated_from is not None:
        log.info("Settings, store and log of an older version copied from %s", location.migrated_from)
    if location.mode == config.MODE_TEMP:
        log.warning("The data folder is temporary: set REVERSEHIRE_HOME to a writable folder")
    provider = config.cloud_sync_provider(folder)
    if provider:
        log.warning("The data folder is synchronised by %s: the database may be copied while "
                    "it is being written; prefer a folder that is not synchronised", provider)
    package = config.packaged_app_name()
    if package:
        log.warning("Started from inside the packaged app %s: Windows may redirect its "
                    "AppData writes", package)
    return location


def start_language(settings: config.Settings, save=config.save) -> str:
    """Every start begins in the start-up language (English by default), never in
    the language a previous session chose with the chips. The session language is
    saved when it changes, so that the MCP server (another process reading
    config.json) follows it."""
    previous = settings.language
    available = tuple(i18n.available_languages())
    changed = config.apply_startup_language(settings, available)
    try:
        i18n.set_language(settings.language)
    except ValueError:
        settings.language = i18n.DEFAULT_LANGUAGE
        i18n.set_language(settings.language)
        changed = settings.language != previous
    log.info("Interface language at start-up: %s (start-up option: %s; previous session: %s)",
             settings.language, getattr(settings, "startup_language", config.DEFAULT_STARTUP_LANGUAGE), previous)
    if changed:
        try:
            save(settings)
        except OSError as error:
            log.warning("Session language not saved: %s", error)
    return settings.language


def main(close_after_ms: int | None = None) -> None:
    """Starts the interface. `close_after_ms` closes the window by itself (start-up smoke tests)."""
    applog.safe_console()   # pythonw: no console; a cp1252 console: no UnicodeEncodeError
    settings = config.load()
    log_path = applog.configure()
    applog.register_secret(settings.firecrawl_key)
    applog.register_secret(settings.paid_registry_key)
    _install_exception_handlers()
    log.info("Starting ReverseHire %s (Python %s, %s)", __version__, sys.version.split()[0], sys.platform)
    log_data_location()
    # Before any HTTPS client exists: trust the system certificate store, so that
    # antivirus HTTPS scanning or a company proxy does not break the downloads.
    from .scoring.embedding import use_system_certificates

    use_system_certificates()
    start_language(settings)

    from .gui import theme

    # Before any window exists: Windows then shows the app's icon, not Python's.
    theme.set_app_user_model_id()
    app = theme.ensure_application(sys.argv[:1])

    from PySide6.QtCore import QTimer

    from .gui.main_window import MainWindow

    from .gui.safety import Watchdog

    window = MainWindow(settings)
    window.show()
    # A blocked event loop writes the GUI thread's stack to the log (freeze diagnosis).
    watchdog = Watchdog().start()
    if close_after_ms is not None:
        QTimer.singleShot(close_after_ms, window.close)
    app.exec()
    watchdog.stop()
    window.shutdown()
    log.info("Closed (log: %s)", log_path)


# Modules a ready-made build runs with "<executable> -m <module> [args]", as Python
# would: the MCP server is started that way by the assistant strip and by AI clients
# (the command is built from sys.executable, which is the program itself there).
# Value: (function to call, does it take the remaining arguments?).
RUNNABLE_MODULES = {
    "reversehire.mcp_server": ("main", False),
    "reversehire.diagnostics": ("main", True),
    "reversehire.cv.translate": ("_main", True),  # download of the translation models
}


def launch_target(argv: list[str]) -> str:
    """What a command line asks for: "module", "self-test", "version" or "gui"."""
    if len(argv) >= 2 and argv[0] == "-m" and argv[1] in RUNNABLE_MODULES:
        return "module"
    from .selftest import FLAG

    if any(arg == FLAG for arg in argv):
        return "self-test"
    if any(arg == "--version" for arg in argv):
        return "version"
    return "gui"


def run(argv: list[str] | None = None) -> int:
    """Entry point of the script, of `python -m reversehire` and of the ready-made
    builds: the window, or `--self-test`, `--version`, `-m reversehire.mcp_server`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    target = launch_target(argv)
    if target == "module":
        import importlib

        module_name, rest = argv[1], argv[2:]
        sys.argv = [sys.argv[0]] + rest
        function, takes_arguments = RUNNABLE_MODULES[module_name]
        module_main = getattr(importlib.import_module(module_name), function)
        try:
            return int((module_main(rest) if takes_arguments else module_main()) or 0)
        except SystemExit as stop:
            return stop.code if isinstance(stop.code, int) else 0
    if target == "self-test":
        from . import selftest

        code = selftest.run(argv)
        # Qt WebEngine can hang on interpreter shutdown: the report is already written.
        try:
            sys.stdout.flush()
        except (AttributeError, OSError, ValueError):
            pass
        os._exit(code)
    if target == "version":
        try:
            print(f"ReverseHire {__version__}")
        except (AttributeError, OSError, ValueError):
            pass
        return 0
    main()
    return 0


if __name__ == "__main__":
    sys.exit(run())

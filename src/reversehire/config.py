"""User settings, saved in config.json in the data folder."""

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import REPOSITORY_URL

CRAWL_ENGINES = ("local", "playwright", "firecrawl")
THEMES = ("dark", "light", "system")  # "system" follows the operating system's app theme
DEFAULT_THEME = "dark"
CONFIG_FILE_NAME = "config.json"
LANGUAGES = ("en", "it", "uk")  # interface/report languages accepted in config.json
DEFAULT_LANGUAGE = "en"
# Language every start begins with (Settings -> General, "Start-up language").
# "last" restores the language of the previous session (the behaviour of older
# versions, now only on explicit request).
STARTUP_LAST = "last"
STARTUP_LANGUAGES = ("en", "uk", "it", STARTUP_LAST)
DEFAULT_STARTUP_LANGUAGE = "en"
ASSISTANTS = ("claude", "codex")  # in-app chat: the user's installed CLIs
DEFAULT_ASSISTANT = "claude"
DEFAULT_USER_AGENT = f"ReverseHire/0.1 (personal desktop app; +{REPOSITORY_URL})"
# Earlier default without a contact URL: replaced by the current one on load.
OLD_DEFAULT_USER_AGENTS = ("ReverseHire/0.1 (personal desktop app)",)

# Company sites analysed at the same time (one thread each).
PARALLEL_DEFAULT = 8
PARALLEL_MIN = 1
PARALLEL_MAX = 16

WORKING_DAYS_PER_YEAR = 220  # reference for business-travel days (as in cv/mobility.py)

OCR_ADVANCED_MODES = ("ask", "always", "never")
OCR_ADVANCED_DEFAULT = "ask"

DEFAULT_OSM_REGION = "emilia-romagna"
DEFAULT_DISCOVERY_SOURCES = "osm_extract,overpass,wikidata"


def parse_discovery_sources(text) -> list[str]:
    """"a, b,,a" -> ["a", "b"]: trimmed, lower case, no empties, no duplicates, order kept."""
    if isinstance(text, (list, tuple)):
        text = ",".join(str(item) for item in text)
    result: list[str] = []
    for item in str(text or "").split(","):
        name = item.strip().lower()
        if name and name not in result:
            result.append(name)
    return result


def clamp_parallel(value) -> int:
    """Clamp a number of parallel workers to PARALLEL_MIN..PARALLEL_MAX.

    Non-numeric values (or booleans) fall back to PARALLEL_DEFAULT.
    """
    if isinstance(value, bool):
        return PARALLEL_DEFAULT
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return PARALLEL_DEFAULT
    return max(PARALLEL_MIN, min(PARALLEL_MAX, number))


# --- data folder ----------------------------------------------------------------------
#
# Resolution order (first match wins), see resolve_data_location:
#   1. REVERSEHIRE_HOME, when set (explicit choice, always used);
#   2. PORTABLE: "<program folder>/userdata" when the program folder can be written.
#      The program folder is the repository root of a source checkout, or the
#      install root (the folder that holds "venv") of an installed copy. Nothing has
#      to be marked: a writable program folder means portable. This keeps the SQLite
#      store "in the same folder as the program", and it is never under %APPDATA%,
#      which Windows silently redirects for programs started from a packaged (MSIX) app;
#   3. the per-user data folder of the operating system (no administrator rights):
#      Windows %LOCALAPPDATA%\ReverseHire (Local, not Roaming: models are large),
#      Linux $XDG_DATA_HOME/reversehire or ~/.local/share/reversehire,
#      macOS ~/Library/Application Support/ReverseHire;
#   4. last resort, a folder under the system temporary folder (logged as a warning:
#      its content may be deleted by the system).
# On the first start with a new folder, the settings, the store and the log of an
# older version (%APPDATA%\ReverseHire, ~/.reversehire) are COPIED there (never moved).

HOME_ENV = "REVERSEHIRE_HOME"
APP_FOLDER_NAME = "ReverseHire"
PORTABLE_SUBFOLDER = "userdata"
STORE_FILE_NAME = "store.sqlite"
LOG_FILE_NAME = "reversehire.log"  # same as applog.LOG_FILE_NAME (no import: applog imports config)
MIGRATION_NOTE = "migrated_from.txt"

MODE_ENV, MODE_PORTABLE, MODE_USER, MODE_TEMP = "env", "portable", "user", "temp"
MODE_TEXTS = {
    MODE_ENV: "set by the REVERSEHIRE_HOME environment variable",
    MODE_PORTABLE: "portable: inside the program folder, which is writable",
    MODE_USER: "per-user data folder of the operating system (the program folder is not writable)",
    MODE_TEMP: "TEMPORARY folder: no other folder could be written; data may be deleted by the system",
}


@dataclass(frozen=True)
class DataLocation:
    """Where the data folder is and why (shown in the log, About and Diagnostics)."""

    folder: Path
    mode: str                         # one of MODE_*
    program_folder: Path | None = None
    migrated_from: Path | None = None  # legacy folder copied on this start, if any

    @property
    def reason(self) -> str:
        return MODE_TEXTS.get(self.mode, self.mode)


def _source_checkout_root(package_file: Path | None = None) -> Path | None:
    """Repository root when this package runs from a source checkout (src layout)."""
    here = Path(package_file or __file__).resolve()
    root = here.parents[2] if len(here.parents) > 2 else None
    if root is not None and (root / "pyproject.toml").is_file() and (root / "src" / "reversehire").is_dir():
        return root
    return None


def is_frozen() -> bool:
    """True inside a ready-made build (PyInstaller sets ``sys.frozen``)."""
    return bool(getattr(sys, "frozen", False))


def frozen_program_folder(executable: str | Path | None = None, platform: str | None = None) -> Path | None:
    """The program folder of a ready-made build: the folder that holds the executable
    (Windows ``ReverseHire.exe``, Linux ``reversehire``). None for a macOS app bundle
    (``X.app/Contents/MacOS/<exe>``): a bundle is not a place for user data, so the
    per-user folder (~/Library/Application Support/ReverseHire) is used instead."""
    exe = Path(executable if executable is not None else sys.executable)
    try:
        exe = exe.resolve()
    except OSError:
        pass
    folder = exe.parent
    if (platform or sys.platform) == "darwin" and folder.name == "MacOS" \
            and folder.parent.name == "Contents" and folder.parent.parent.suffix.lower() == ".app":
        return None
    return folder


def program_folder(package_file: Path | None = None, prefix: str | Path | None = None,
                   frozen: bool | None = None) -> Path | None:
    """The folder of the program: the folder of the executable of a ready-made build
    (see frozen_program_folder), a source checkout's root, or the install root that
    holds the running ``venv`` (installer layout ``<install>/venv``); None otherwise
    (for example a package installed into some other Python environment)."""
    if is_frozen() if frozen is None else frozen:
        return frozen_program_folder()
    root = _source_checkout_root(package_file)
    if root is not None:
        return root
    environment = Path(prefix if prefix is not None else sys.prefix)
    name = environment.name.lower()
    if name == "venv":
        return environment.parent
    if name == ".venv" and (environment.parent / "pyproject.toml").is_file():
        return environment.parent
    return None


def is_writable_folder(folder: Path) -> bool:
    """True when a file can really be created and removed in `folder` (created if
    missing). Permission bits are not trusted: ACLs, read-only media and
    virtualised folders only show up on a real write."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = folder / f".write_test_{os.getpid()}_{id(folder):x}"
    try:
        handle = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(handle)
        os.remove(probe)
        return True
    except OSError:
        try:
            probe.unlink()
        except OSError:
            pass
        return False


def user_data_folder(platform: str | None = None, env: dict | None = None,
                     home: Path | None = None) -> Path:
    """The per-user data folder of the operating system (not created here)."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = home or Path.home()
    if platform == "win32":
        base = env.get("LOCALAPPDATA")
        root = Path(base) if base else home / "AppData" / "Local"
        return root / APP_FOLDER_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_FOLDER_NAME
    xdg = (env.get("XDG_DATA_HOME") or "").strip()
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".local" / "share"
    return root / APP_FOLDER_NAME.lower()


def legacy_data_folders(platform: str | None = None, env: dict | None = None,
                        home: Path | None = None) -> list[Path]:
    """Data folders used by older versions, newest first."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = home or Path.home()
    folders = []
    if platform == "win32":
        base = env.get("APPDATA")
        folders.append((Path(base) if base else home / "AppData" / "Roaming") / APP_FOLDER_NAME)
    folders.append(home / ".reversehire")
    return folders


def temp_data_folder() -> Path:
    import getpass
    import tempfile

    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    safe = "".join(c for c in user if c.isalnum() or c in "-_") or "user"
    return Path(tempfile.gettempdir()) / f"{APP_FOLDER_NAME}-{safe}"


def _same_folder(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _has_data(folder: Path) -> bool:
    return (folder / STORE_FILE_NAME).is_file() or (folder / CONFIG_FILE_NAME).is_file()


def migrate_legacy_data(target: Path, legacy: list[Path]) -> Path | None:
    """Copies config, store and log of the first legacy folder that has data into
    `target`, when `target` has neither a store nor a config. Never moves or deletes:
    the old folder stays as it was. Models are not copied (they are found through
    the model search folders). Returns the folder copied from, or None."""
    import logging
    import shutil

    log = logging.getLogger(__name__)
    if _has_data(target):
        return None
    for source in legacy:
        try:
            if not source.is_dir() or _same_folder(source, target) or not _has_data(source):
                continue
        except OSError:
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            for name in (CONFIG_FILE_NAME, MCP_USAGE_FILE_NAME, LOG_FILE_NAME):
                if (source / name).is_file():
                    shutil.copy2(source / name, target / name)
            if (source / STORE_FILE_NAME).is_file():
                _copy_sqlite(source / STORE_FILE_NAME, target / STORE_FILE_NAME)
            (target / MIGRATION_NOTE).write_text(
                f"Settings, store and log copied from {source} on first start.\n"
                "The old folder was left untouched and can be deleted once everything works.\n",
                encoding="utf-8")
        except Exception as error:  # noqa: BLE001 - never stop the start-up
            log.warning("Data of an older version not copied from %s: %s", source, error)
            return None
        log.info("Data of an older version copied from %s to %s", source, target)
        return source
    return None


def _copy_sqlite(source: Path, target: Path) -> None:
    """Consistent copy of a SQLite database (the backup API also takes the content of
    a -wal file that a plain file copy would miss)."""
    import sqlite3

    src = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def resolve_data_location(*, env: dict | None = None, platform: str | None = None,
                          program: Path | None | bool = True, home: Path | None = None,
                          writable=is_writable_folder, temp: Path | None = None,
                          migrate: bool = True) -> DataLocation:
    """Decides the data folder (see the rules at the top of this section) and creates it.

    Every input can be replaced for tests: `program=True` means "detect the program
    folder", `program=None` means "no program folder"."""
    import logging

    log = logging.getLogger(__name__)
    env = os.environ if env is None else env
    explicit = (env.get(HOME_ENV) or "").strip().strip('"')
    if explicit:
        folder = Path(explicit).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        return DataLocation(folder, MODE_ENV, None)
    program_dir = program_folder() if program is True else program
    chosen: Path | None = None
    mode = MODE_TEMP
    if program_dir is not None and writable(Path(program_dir) / PORTABLE_SUBFOLDER):
        chosen, mode = Path(program_dir) / PORTABLE_SUBFOLDER, MODE_PORTABLE
    else:
        if program_dir is not None:
            # A failed probe may leave an empty "userdata" behind: remove it (only if empty).
            try:
                (Path(program_dir) / PORTABLE_SUBFOLDER).rmdir()
            except OSError:
                pass
        user = user_data_folder(platform, env, home)
        if writable(user):
            chosen, mode = user, MODE_USER
    if chosen is None:
        chosen = temp or temp_data_folder()
        chosen.mkdir(parents=True, exist_ok=True)
        log.warning("No writable data folder: using the temporary folder %s; its content "
                    "may be deleted by the system. Set REVERSEHIRE_HOME to a writable folder.", chosen)
    migrated = migrate_legacy_data(chosen, legacy_data_folders(platform, env, home)) if migrate else None
    return DataLocation(chosen, mode, Path(program_dir) if program_dir is not None else None, migrated)


_location_cache: dict[str, DataLocation] = {}


def data_location() -> DataLocation:
    """The resolved data location, computed once per process (and again when
    REVERSEHIRE_HOME changes, as the tests do)."""
    key = os.environ.get(HOME_ENV, "")
    cached = _location_cache.get(key)
    if cached is not None:
        try:
            cached.folder.mkdir(parents=True, exist_ok=True)  # deleted while running
        except OSError:
            pass
        return cached
    location = resolve_data_location()
    _location_cache.clear()
    _location_cache[key] = location
    return location


def reset_data_location() -> None:
    """Forgets the resolved location (tests, or after REVERSEHIRE_HOME changed)."""
    _location_cache.clear()


def data_folder() -> Path:
    """User data folder (settings, store, log, downloaded models); created if missing.
    See resolve_data_location for the rules."""
    return data_location().folder


# --- cloud-synced folders ---------------------------------------------------------------

# Path parts that name a folder kept in sync by a cloud client. SQLite files there
# can be uploaded half-written or replaced by a remote copy while the app runs.
_CLOUD_MARKERS = {
    "onedrive": "OneDrive", "dropbox": "Dropbox", "google drive": "Google Drive",
    "googledrive": "Google Drive", "my drive": "Google Drive", "icloud drive": "iCloud Drive",
    "icloud": "iCloud Drive", "mobile documents": "iCloud Drive", "cloudstorage": "cloud storage",
    "pcloud": "pCloud", "pcloud drive": "pCloud", "nextcloud": "Nextcloud", "owncloud": "ownCloud",
    "mega": "MEGA", "megasync": "MEGA", "box": "Box", "box sync": "Box", "sync.com": "Sync.com",
    "seafile": "Seafile", "yandex.disk": "Yandex Disk",
}


def cloud_sync_provider(path: Path, env: dict | None = None) -> str | None:
    """Name of the cloud-sync service that seems to manage `path`, or None."""
    env = os.environ if env is None else env
    try:
        target = os.path.normcase(os.path.abspath(path))
    except (OSError, ValueError):
        return None
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = env.get(variable)
        if root:
            base = os.path.normcase(os.path.abspath(root))
            if target == base or target.startswith(base.rstrip("\\/") + os.sep):
                return "OneDrive"
    for part in Path(path).parts:
        lowered = part.lower().strip()
        if lowered in _CLOUD_MARKERS:
            return _CLOUD_MARKERS[lowered]
        if lowered.startswith("onedrive -") or lowered.startswith("onedrive-"):
            return "OneDrive"
        if lowered.startswith("dropbox (") or lowered.startswith("googledrive-"):
            return "Dropbox" if lowered.startswith("dropbox") else "Google Drive"
    return None


def packaged_app_name() -> str | None:
    """Windows: full name of the MSIX package this process runs inside (programs
    started from a packaged app inherit it), whose AppData writes are redirected;
    None when not packaged or elsewhere."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        length = ctypes.c_uint32(0)
        function = ctypes.windll.kernel32.GetCurrentPackageFullName
        result = function(ctypes.byref(length), None)
        if result == 15700:  # APPMODEL_ERROR_NO_PACKAGE
            return None
        buffer = ctypes.create_unicode_buffer(max(1, length.value))
        if function(ctypes.byref(length), buffer) != 0:
            return None
        return buffer.value or None
    except Exception:
        return None


JOBSEARCH_PAGES_DEFAULT = 20
JOBSEARCH_PAGES_RANGE = (1, 100)
JOBSEARCH_TIMEOUT_DEFAULT_S = 120.0
JOBSEARCH_TIMEOUT_RANGE_S = (10.0, 900.0)
JOBSEARCH_KEY_FIELDS = ("adzuna_app_id", "adzuna_app_key", "jooble_key", "careerjet_key")
# Fields of recruiter_sim.KnockoutAnswers and the types accepted for each (None = not answered).
KNOCKOUT_FIELDS = {"right_to_work": bool, "needs_sponsorship": bool, "max_commute_km": float,
                   "willing_to_relocate": bool, "notice_weeks": int, "min_salary_year": float,
                   "driving_licence": list, "own_car": bool, "max_travel_days": int, "shifts_ok": bool}


def clean_knockout_answers(raw) -> dict:
    """The saved screening answers with only known fields of the right type."""
    result: dict = {}
    if not isinstance(raw, dict):
        return result
    for name, kind in KNOCKOUT_FIELDS.items():
        value = raw.get(name)
        if value is None:
            continue
        if kind is bool and isinstance(value, bool):
            result[name] = value
        elif kind is int and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[name] = value
        elif kind is float and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            result[name] = float(value)
        elif kind is list and isinstance(value, list):
            items = [str(v).strip().upper() for v in value if isinstance(v, str) and v.strip()]
            if items:
                result[name] = items
    return result


@dataclass
class Settings:
    # Language of the running session ("en", "it", "uk"): changed by the EN/UK/IT chips
    # and saved so that the MCP server and the assistant follow it, but NOT restored at
    # the next start: see startup_language and choose_startup_language.
    language: str = DEFAULT_LANGUAGE
    # Language every start begins with: one of STARTUP_LANGUAGES. A config.json written
    # by an older version has no such field: it starts in English whatever its saved
    # `language` (the one-time migration to the new default).
    startup_language: str = DEFAULT_STARTUP_LANGUAGE
    crawl_engine: str = "local"  # "local", "playwright", "firecrawl"
    firecrawl_key: str = ""
    radius_km: float = 15.0
    max_companies: int = 0  # 0 = no limit
    request_delay_s: float = 2.0
    max_pages_per_site: int = 5
    user_agent: str = DEFAULT_USER_AGENT
    overpass_server: str = "https://overpass-api.de/api/interpreter"
    include_without_site: bool = True  # also assess (from OSM tags only) companies without a site
    theme: str = DEFAULT_THEME  # "dark", "light", "system"
    # Company sites visited at the same time; clamped to 1..16 (see clamp_parallel).
    # 1 = one company at a time, as in older versions.
    parallel_companies: int = PARALLEL_DEFAULT
    # MCP access for AI clients (Claude, Codex, Gemini). Off by default: privacy first.
    mcp_enabled: bool = False
    # Candidate preferences (desirability score, see scoring/preferences.py).
    prefer_permanent: bool = True  # permanent > permanent possible > other contracts
    prefer_remote: bool = True  # remote/hybrid work is a plus
    min_salary_year: int = 0  # gross yearly minimum; 0 = not set
    max_travel_days_year: int = -1  # business-travel days a year accepted; -1 = use the CV value
    # Optional job boards as an extra postings source (read by the pipeline). Off by default.
    job_boards_enabled: bool = False
    # Paid company registry (optional): provider id (free text, e.g. "openapi.it") and its
    # API key. The key is a secret: registered with applog.register_secret, never logged.
    paid_registry_provider: str = ""
    paid_registry_key: str = ""
    # Region of the OpenStreetMap extract used to discover companies.
    osm_region: str = DEFAULT_OSM_REGION
    # Company discovery sources, comma separated, in order (see discovery_source_list).
    discovery_sources: str = DEFAULT_DISCOVERY_SOURCES
    # Minimum pause between two requests to the same host (registry/discovery sources).
    request_pause_s: float = 2.0
    # Application e-mail: mail.MailSettings.to_dict() (templates, reference PDF, the
    # user's own name / e-mail / phone, preferred method). {} = defaults of the language.
    mail: dict = field(default_factory=dict)
    # In-app chat: which installed assistant CLI answers ("claude" or "codex") and
    # optional explicit paths of the CLIs ("" = find them automatically).
    assistant: str = DEFAULT_ASSISTANT
    claude_path: str = ""
    codex_path: str = ""
    chat_notice_shown: bool = False  # "answered by your own CLI, counts against your plan" shown once
    chat_expanded: bool = False      # the conversation of the chat strip is open
    # Job search (jobsearch package): the user's own keys of the optional keyed APIs
    # (secrets: registered with applog.register_secret, never logged), the sources the
    # user switched on or off ({source id: bool}; absent = the source's default), the
    # posting pages read per agency and search, and how long one source may take.
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    jooble_key: str = ""
    careerjet_key: str = ""
    jobsearch_sources: dict = field(default_factory=dict)
    jobsearch_max_pages: int = JOBSEARCH_PAGES_DEFAULT
    jobsearch_timeout_s: float = JOBSEARCH_TIMEOUT_DEFAULT_S
    # Map (mapview package): "" = OpenStreetMap tiles; True = no tiles at all.
    map_tile_url: str = ""
    map_offline: bool = False
    # Recruiter analysis: the user's own answers to the usual screening questions
    # (recruiter_sim.KnockoutAnswers.to_dict(); {} = nothing answered).
    knockout_answers: dict = field(default_factory=dict)
    # Advanced OCR reader (cv/ocr_models.py), used when a scan reads poorly: "ask" (offer
    # the download once), "always" (download and use it without asking), "never".
    ocr_advanced: str = OCR_ADVANCED_DEFAULT
    # Affinity (affinity package): the user's directions, company types, distance limit
    # (affinity.AffinityPreferences.to_dict(); {} = defaults) and the personal weights
    # from "Calibrate from my judgements" (affinity.Weights.to_dict(); {} = defaults).
    affinity_preferences: dict = field(default_factory=dict)
    affinity_weights: dict = field(default_factory=dict)
    # Assistant suggestions (suggest package): web search for the assistant (opt-in) and
    # whether the brief preview / cost notice was already accepted once.
    suggest_allow_web_search: bool = False
    suggest_notice_shown: bool = False
    # Idle analysis (background package): on/off, minutes of inactivity before it starts,
    # requests a day, only on mains power; the user's pause switch.
    idle_enabled: bool = True
    idle_minutes: int = 5
    idle_daily_budget: int = 600
    idle_only_on_mains: bool = True
    idle_paused: bool = False
    idle_queue_agreed: bool = False   # the user agreed once to queue companies for the idle analysis
    # Regional register (registers package): regions downloaded, months of RNA aid,
    # check for updates at start-up, official sources switched on.
    register_regions: list = field(default_factory=lambda: ["emilia-romagna"])
    register_rna_months: int = 24
    register_auto_check: bool = True
    register_sources: list = field(default_factory=lambda: ["rna", "gleif", "opencoesione", "agcm"])

    def discovery_source_list(self) -> list[str]:
        return parse_discovery_sources(self.discovery_sources)


def _config_path() -> Path:
    return data_folder() / CONFIG_FILE_NAME


def config_path() -> Path:
    """Full path of config.json (shown to the user in messages)."""
    return _config_path()


# --- MCP switch and usage record ------------------------------------------------------

MCP_USAGE_FILE_NAME = "mcp_usage.json"
_mcp_cache: dict[str, tuple] = {}


def mcp_enabled() -> bool:
    """Current value of `mcp_enabled` in config.json, re-read only when the file changes.

    Cheap enough to call before every MCP tool call (one stat per call).
    """
    path = _config_path()
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return Settings().mcp_enabled
    cached = _mcp_cache.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    value = load().mcp_enabled
    _mcp_cache[str(path)] = (stamp, value)
    return value


def mcp_usage_path() -> Path:
    return data_folder() / MCP_USAGE_FILE_NAME


def read_mcp_usage(path: Path | None = None) -> dict:
    """{"last_used": epoch seconds or None, "calls": int}; missing or corrupt file -> empty."""
    empty = {"last_used": None, "calls": 0}
    try:
        raw = json.loads((path or mcp_usage_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict):
        return empty
    last = raw.get("last_used")
    calls = raw.get("calls")
    return {
        "last_used": float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else None,
        "calls": calls if isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0 else 0,
    }


def record_mcp_use(path: Path | None = None, now: float | None = None) -> dict:
    """Records one served MCP tool call: time and a counter only (no tool name, no data)."""
    import time

    target = path or mcp_usage_path()
    usage = read_mcp_usage(target)
    usage = {"last_used": time.time() if now is None else float(now), "calls": usage["calls"] + 1}
    try:
        target.write_text(json.dumps(usage), encoding="utf-8")
    except OSError:
        pass
    return usage


def _compatible_type(value, default) -> bool:
    """Accepts only values of the same type as the default (int allowed for float)."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if isinstance(default, float):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


MAP_BACKGROUND_KEY = "map_background"   # config.json key of "Show the map background" (Settings.map_offline inverted)


def load() -> Settings:
    """Reads the settings; missing or corrupt file -> default values."""
    defaults = Settings()
    try:
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if not isinstance(raw, dict):
        return defaults

    values = {}
    # The map background is stored as the POSITIVE "map_background" (default on). Files of
    # older versions only had the negative "map_offline": it is not carried over (the
    # owner's decision: maps start with their background, whatever an older file said).
    background = raw.get(MAP_BACKGROUND_KEY)
    raw = {k: v for k, v in raw.items() if k != "map_offline"}
    if isinstance(background, bool):
        raw["map_offline"] = not background
    for field in fields(Settings):
        if field.name not in raw:
            continue
        value = raw[field.name]
        if not _compatible_type(value, getattr(defaults, field.name)):
            continue
        if field.name == "crawl_engine" and value not in CRAWL_ENGINES:
            continue
        if field.name == "theme" and value not in THEMES:
            continue
        if field.name == "language" and value not in LANGUAGES:
            continue
        if field.name == "startup_language" and value not in STARTUP_LANGUAGES:
            continue
        if field.name == "assistant" and value not in ASSISTANTS:
            continue
        if field.name == "ocr_advanced" and value not in OCR_ADVANCED_MODES:
            continue
        if field.name == "mail":
            value = {str(k): v for k, v in value.items() if isinstance(v, str)}
        if field.name in ("claude_path", "codex_path"):
            value = value.strip()
        if field.name == "user_agent" and (not value.strip() or value.strip() in OLD_DEFAULT_USER_AGENTS):
            continue  # empty or the old default: use the current default (with a contact URL)
        if field.name == "parallel_companies":
            value = clamp_parallel(value)
        if field.name == "min_salary_year":
            value = max(0, value)
        if field.name == "max_travel_days_year":
            value = -1 if value < 0 else min(value, WORKING_DAYS_PER_YEAR)
        if field.name in ("osm_region", "discovery_sources") and not value.strip():
            continue  # empty: use the default
        if field.name == "discovery_sources":
            value = ",".join(parse_discovery_sources(value)) or DEFAULT_DISCOVERY_SOURCES
        if field.name == "request_pause_s" and not (0 <= value <= 3600):
            continue  # negative, NaN or absurd: use the default
        if field.name in ("paid_registry_provider", "paid_registry_key", "osm_region", "map_tile_url",
                          *JOBSEARCH_KEY_FIELDS):
            value = value.strip()
        if field.name == "jobsearch_sources":
            value = {str(k): v for k, v in value.items() if isinstance(v, bool)}
        if field.name == "knockout_answers":
            value = clean_knockout_answers(value)
        if field.name == "jobsearch_max_pages" and not (
                JOBSEARCH_PAGES_RANGE[0] <= value <= JOBSEARCH_PAGES_RANGE[1]):
            continue
        if field.name == "jobsearch_timeout_s" and not (
                JOBSEARCH_TIMEOUT_RANGE_S[0] <= value <= JOBSEARCH_TIMEOUT_RANGE_S[1]):
            continue
        if isinstance(getattr(defaults, field.name), float):
            value = float(value)
        values[field.name] = value
    return Settings(**values)


def choose_startup_language(settings: Settings, available: tuple[str, ...] | list[str] = LANGUAGES) -> str:
    """The interface language a new start begins with.

    English by default; the Start-up language option may name another language, or
    "last" for the language of the previous session. A saved `language` alone (as
    written by older versions, which restored it) never decides it."""
    choice = getattr(settings, "startup_language", DEFAULT_STARTUP_LANGUAGE)
    if choice == STARTUP_LAST:
        choice = settings.language
    return choice if choice in available else DEFAULT_LANGUAGE


def apply_startup_language(settings: Settings, available: tuple[str, ...] | list[str] = LANGUAGES) -> bool:
    """Sets `settings.language` to the start-up language; True when it changed (the
    caller then saves, so that the MCP server follows the session language)."""
    code = choose_startup_language(settings, available)
    if code == settings.language:
        return False
    settings.language = code
    return True


def save(cfg: Settings) -> None:
    """Writes the settings to config.json."""
    data = asdict(cfg)
    data["parallel_companies"] = clamp_parallel(data.get("parallel_companies"))
    data[MAP_BACKGROUND_KEY] = not bool(data.pop("map_offline", False))
    text = json.dumps(data, ensure_ascii=False, indent=2)
    _config_path().write_text(text, encoding="utf-8")

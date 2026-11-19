"""Look of ReverseHire: dark/light palettes, one QSS stylesheet generated from the
palette, the JetBrains Mono font, the window icon and the Windows title bar.
The "system" theme reads Windows' registry, macOS' `defaults` or the GNOME settings.

Every colour of the interface comes from here. Widgets that need a special look
get a `role` dynamic property ("accent", "muted", "error", "success", "warning",
"title") that the stylesheet matches; `set_role` changes it at runtime.
Calls into Windows (registry, DWM) are always guarded: on other systems, or on
error, nothing happens.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import tempfile
from dataclasses import dataclass, fields
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QStyleFactory, QToolTip, QWidget

from .. import DEVELOPER
from ..config import DEFAULT_THEME, THEMES  # noqa: F401  (THEMES re-exported below)

log = logging.getLogger(__name__)

APP_NAME = "ReverseHire"
APP_USER_MODEL_ID = "0THack1A.ReverseHire"  # developer name: the first character is the digit zero
RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"
FONTS_DIR = RESOURCES_DIR / "fonts"
FONT_FILES = ("JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf", "JetBrainsMono-Italic.ttf")
# Fallbacks after the bundled font: Windows, macOS, then Linux monospace families.
FONT_FAMILIES = ("JetBrains Mono", "Cascadia Code", "Cascadia Mono", "Consolas", "Menlo", "Monaco",
                 "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", "monospace")
FONT_POINT_SIZE = 9
# macOS counts 72 points per inch (Windows and Linux 96): the same look needs a bigger size.
MACOS_FONT_POINT_SIZE = 12
DESKTOP_FILE_NAME = "reversehire"  # Wayland/X11 application id (icon and taskbar grouping)
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
THEMES = THEMES  # config values: "dark", "light", "system" (follow the operating system)
DEFAULT_THEME = DEFAULT_THEME

_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19


@dataclass(frozen=True)
class Palette:
    background: str
    surface: str
    surface2: str
    border: str
    text: str          # ordinary labels
    strong: str        # data: fields, tables, lists (the site's "primary")
    muted: str         # hints and secondary notes
    accent: str
    on_accent: str
    selection: str
    hiring_bg: str
    hiring_fg: str
    maybe_bg: str
    maybe_fg: str
    error: str
    warning: str
    remote_bg: str     # remote/hybrid work: blue, distinct from hiring green and "maybe" amber
    remote_fg: str

    def as_dict(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# Dark: the owner's site (background #050806, accent #39D98A, primary #8FB89A,
# text #7F9184). Light: the palette derived earlier for the tkinter interface.
PALETTES: dict[str, Palette] = {
    "dark": Palette(
        background="#050806", surface="#0B120E", surface2="#111A14", border="#1E2E24",
        text="#7F9184", strong="#8FB89A", muted="#66786C", accent="#39D98A", on_accent="#04130A",
        selection="#17402B", hiring_bg="#123622", hiring_fg="#7CFFB5",
        maybe_bg="#3A3212", maybe_fg="#F2D675", error="#FF6B6B", warning="#E0B341",
        remote_bg="#0F2A3D", remote_fg="#7CC4FF",
    ),
    "light": Palette(
        background="#F4F7F5", surface="#FFFFFF", surface2="#E9EFEB", border="#C5D2C9",
        text="#1F2D24", strong="#0B140E", muted="#55655B", accent="#0E8F55", on_accent="#FFFFFF",
        selection="#CDEBDC", hiring_bg="#D5F2E2", hiring_fg="#0A5A36",
        maybe_bg="#FFF3C9", maybe_fg="#5F4A00", error="#B3261E", warning="#8A6100",
        remote_bg="#D8EAFB", remote_fg="#0B4A80",
    ),
}


def status_colours(p: Palette) -> tuple[str, str]:
    """(off, on) colours of a status dot: the palette's red and green."""
    return p.error, p.accent


def severity_colour(p: Palette, severity: str) -> str:
    """Colour of a recruiter finding / AI-signal level ("problem", "warning", "info", "good")."""
    return {"problem": p.error, "high": p.error, "warning": p.warning, "medium": p.warning,
            "good": p.accent, "low": p.accent}.get(severity, p.muted)


# ---------------------------------------------------------------------- system


def _on_windows() -> bool:
    return sys.platform == "win32"


def _windll():
    return ctypes.windll  # Windows only: AttributeError elsewhere, handled by callers


def _run_quietly(command: list[str]) -> str | None:
    """stdout of a short system command, or None (missing program, error, timeout)."""
    import subprocess

    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=2)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return result.stdout if result.returncode == 0 else ""


def desktop_theme(platform: str | None = None, runner=_run_quietly) -> str | None:
    """"light"/"dark" from the desktop settings on macOS and Linux, or None if unknown.

    macOS: `defaults read -g AppleInterfaceStyle` prints "Dark" in dark mode and fails
    in light mode. Linux: GNOME's color-scheme ("prefer-dark"/"prefer-light"/"default"),
    else the GTK theme name ("...-dark")."""
    platform = platform or sys.platform
    if platform == "darwin":
        output = runner(["defaults", "read", "-g", "AppleInterfaceStyle"])
        if output is None:
            return None
        return "dark" if "dark" in output.lower() else "light"
    if platform.startswith("linux") or "bsd" in platform:
        scheme = runner(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"])
        if scheme:
            if "dark" in scheme.lower():
                return "dark"
            if "light" in scheme.lower():
                return "light"
        gtk = runner(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"])
        if gtk:
            return "dark" if "dark" in gtk.lower() else "light"
    return None


def system_theme() -> str:
    """"light" or "dark" following the system app theme (Windows registry, macOS and
    Linux desktop settings); on error "dark"."""
    if not _on_windows():
        try:
            return desktop_theme() or DEFAULT_THEME
        except Exception:
            return DEFAULT_THEME
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY) as key:
            value, _type = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) == 1 else "dark"
    except Exception:
        return DEFAULT_THEME


def resolve_theme(name: str | None) -> str:
    """Effective theme ("dark"/"light"): "system" reads the OS setting, anything else falls back."""
    if name == "system":
        return system_theme()
    return name if name in PALETTES else DEFAULT_THEME


def set_app_user_model_id(identifier: str = APP_USER_MODEL_ID) -> bool:
    """Taskbar identity on Windows (the app's icon instead of Python's)."""
    if not _on_windows():
        return False
    try:
        result = _windll().shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(identifier))
        return result == 0
    except Exception:
        log.debug("AppUserModelID not set", exc_info=True)
        return False


def set_title_bar(window: QWidget, dark: bool) -> bool:
    """Dark/light native title bar on Windows 10/11 (DWM); does nothing elsewhere."""
    if not _on_windows():
        return False
    if QGuiApplication.platformName() != "windows":
        return False  # offscreen and other platform plugins have no native frame
    try:
        dll = _windll()
        hwnd = int(window.winId())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (_DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            result = dll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), ctypes.c_uint(attribute), ctypes.byref(value), ctypes.sizeof(value),
            )
            if result == 0:
                return True
        return False
    except Exception:
        log.debug("Title bar colour not changed", exc_info=True)
        return False


# ---------------------------------------------------------------------- resources


def resource(name: str) -> Path | None:
    """Path of a file in resources/, or None if it is missing."""
    try:
        path = Path(RESOURCES_DIR) / name
        return path if path.is_file() else None
    except OSError:
        return None


def load_pixmap(name: str) -> QPixmap | None:
    path = resource(name)
    if path is None:
        return None
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        log.warning("Image cannot be loaded: %s", path)
        return None
    return pixmap


def app_icon() -> QIcon:
    """Window icon from icon.ico plus the PNG sizes (empty icon if none exists)."""
    icon = QIcon()
    ico = resource("icon.ico")
    if ico is not None:
        icon.addFile(str(ico))
    for size in ICON_SIZES:
        png = resource(f"icon_{size}.png")
        if png is not None:
            icon.addFile(str(png))
    return icon


_loaded_fonts: list[str] = []


def load_fonts() -> list[str]:
    """Registers the bundled JetBrains Mono files; returns the families loaded."""
    if _loaded_fonts:
        return list(_loaded_fonts)
    for name in FONT_FILES:
        path = Path(FONTS_DIR) / name
        if not path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            log.warning("Font cannot be loaded: %s", path)
            continue
        for family in QFontDatabase.applicationFontFamilies(font_id):
            if family not in _loaded_fonts:
                _loaded_fonts.append(family)
    return list(_loaded_fonts)


def app_font() -> QFont:
    """JetBrains Mono, with Cascadia Code / Consolas / monospace as fallbacks."""
    load_fonts()
    font = QFont()
    font.setFamilies(list(FONT_FAMILIES))
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(font_point_size())
    return font


def font_point_size(platform: str | None = None) -> int:
    return MACOS_FONT_POINT_SIZE if (platform or sys.platform) == "darwin" else FONT_POINT_SIZE


def section(text: str) -> str:
    """Terminal-style section title: "> TITLE"."""
    return "> " + (text or "").upper()


# ---------------------------------------------------------------------- colours


def mix(colour1: str, colour2: str, share: float) -> str:
    """Colour between two "#RRGGBB" values (share 0 = first, 1 = second)."""
    a = [int(colour1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(colour2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * share):02X}" for x, y in zip(a, b))


_ARROW_PATHS = {"down": "M1 3 L5 7 L9 3", "up": "M1 7 L5 3 L9 7"}


def arrow_image(colour: str, direction: str) -> str:
    """Path (forward slashes, for QSS url()) of a small arrow SVG in `colour`.

    QSS cannot draw triangles itself and styled combo/spin boxes lose the
    style's arrows, so the arrows are written once per colour to a cache folder.
    """
    folder = Path(tempfile.gettempdir()) / "reversehire_qss"
    path = folder / f"arrow_{direction}_{colour.lstrip('#').lower()}.svg"
    if not path.is_file():
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
               f'<path d="{_ARROW_PATHS[direction]}" fill="none" stroke="{colour}" stroke-width="1.6" '
               f'stroke-linecap="round" stroke-linejoin="round"/></svg>')
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(svg, encoding="utf-8")
        except OSError:
            log.debug("Arrow image not written", exc_info=True)
            return ""
    return path.as_posix()


def build_stylesheet(p: Palette) -> str:
    """The whole application stylesheet for one palette."""
    hover = mix(p.surface2, p.accent, 0.18)
    accent_hover = mix(p.accent, p.strong, 0.25)
    disabled_bg = p.surface
    down = arrow_image(p.strong, "down")
    up = arrow_image(p.strong, "up")
    down_muted = arrow_image(p.muted, "down")
    up_muted = arrow_image(p.muted, "up")
    return f"""
* {{ outline: none; }}
QWidget {{ background-color: {p.background}; color: {p.text}; }}
QMainWindow, QDialog, QMessageBox {{ background-color: {p.background}; }}
QLabel {{ background: transparent; }}
QLabel[role="muted"] {{ color: {p.muted}; }}
QLabel[role="error"] {{ color: {p.error}; }}
QLabel[role="success"] {{ color: {p.accent}; }}
QLabel[role="warning"] {{ color: {p.warning}; }}
QLabel[role="title"] {{ color: {p.accent}; font-weight: bold; }}
QLabel[role="strong"] {{ color: {p.strong}; }}

QToolTip {{ background-color: {p.surface2}; color: {p.strong}; border: 1px solid {p.accent};
           padding: 4px; }}

QMenuBar {{ background-color: {p.surface}; color: {p.text}; border-bottom: 1px solid {p.border}; }}
QMenuBar::item {{ background: transparent; padding: 4px 10px; }}
QMenuBar::item:selected {{ background-color: {p.selection}; color: {p.strong}; }}
QMenuBar::item:pressed {{ background-color: {p.selection}; color: {p.accent}; }}
QMenu {{ background-color: {p.surface}; color: {p.text}; border: 1px solid {p.border}; padding: 4px; }}
QMenu::item {{ padding: 5px 24px 5px 26px; background: transparent; }}
QMenu::item:selected {{ background-color: {p.selection}; color: {p.strong}; }}
QMenu::item:disabled {{ color: {p.muted}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 6px; }}
QMenu::indicator {{ width: 10px; height: 10px; left: 8px; border: 1px solid {p.border};
                   border-radius: 5px; background: {p.surface}; }}
QMenu::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}

QTabWidget::pane {{ border: 1px solid {p.border}; top: -1px; background: {p.background}; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: {p.surface}; color: {p.muted}; border: 1px solid {p.border};
               border-bottom: none; padding: 6px 10px; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {p.background}; color: {p.accent};
                        border-top: 2px solid {p.accent}; }}
QTabBar::tab:hover:!selected {{ background: {p.surface2}; color: {p.strong}; }}

QGroupBox {{ border: 1px solid {p.border}; border-radius: 3px; margin-top: 14px;
            padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 8px;
                   padding: 0 4px; color: {p.accent}; font-weight: bold; }}

QPushButton {{ background-color: {p.surface2}; color: {p.strong}; border: 1px solid {p.border};
              border-radius: 3px; padding: 5px 12px; }}
QPushButton:hover {{ background-color: {hover}; border-color: {p.accent}; }}
QPushButton:pressed {{ background-color: {p.selection}; }}
QPushButton:focus {{ border-color: {p.accent}; }}
QPushButton:disabled {{ background-color: {disabled_bg}; color: {p.muted}; border-color: {p.border}; }}
QPushButton[role="accent"] {{ background-color: {p.accent}; color: {p.on_accent}; border-color: {p.accent};
                             font-weight: bold; padding: 6px 16px; }}
QPushButton[role="small"] {{ padding: 4px 7px; }}
QPushButton[role="accent"]:hover {{ background-color: {accent_hover}; }}
QPushButton[role="accent"]:disabled {{ background-color: {p.surface2}; color: {p.muted};
                                      border-color: {p.border}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QTextBrowser, QListWidget, QListView, QSpinBox,
QDoubleSpinBox, QComboBox {{
    background-color: {p.surface}; color: {p.strong}; border: 1px solid {p.border};
    border-radius: 2px; selection-background-color: {p.selection}; selection-color: {p.strong};
}}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ padding: 3px 5px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QListWidget:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {p.accent}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {p.muted}; background-color: {p.surface2}; }}
QLineEdit[invalid="true"] {{ border-color: {p.error}; }}
QListWidget::item {{ padding: 2px 4px; }}
QListWidget::item:selected {{ background-color: {p.selection}; color: {p.strong}; }}
QComboBox {{ padding-right: 22px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 20px;
                       border: none; }}
QComboBox::down-arrow {{ image: url("{down}"); width: 10px; height: 10px; }}
QComboBox::down-arrow:disabled {{ image: url("{down_muted}"); }}
QSpinBox, QDoubleSpinBox {{ padding-right: 20px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 18px; background: {p.surface2}; border-left: 1px solid {p.border}; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border;
    subcontrol-position: bottom right; width: 18px; background: {p.surface2};
    border-left: 1px solid {p.border}; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover {{ background: {hover}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{up}"); width: 8px; height: 8px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{down}"); width: 8px; height: 8px; }}
QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off {{ image: url("{up_muted}"); }}
QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off {{ image: url("{down_muted}"); }}
QComboBox QAbstractItemView {{ background-color: {p.surface}; color: {p.strong};
                              border: 1px solid {p.border}; selection-background-color: {p.selection};
                              selection-color: {p.strong}; }}

QCheckBox {{ background: transparent; spacing: 6px; color: {p.text}; }}
QCheckBox:disabled {{ color: {p.muted}; }}
QCheckBox::indicator {{ width: 12px; height: 12px; border: 1px solid {p.border}; background: {p.surface};
                       border-radius: 2px; }}
QCheckBox::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
QRadioButton {{ background: transparent; spacing: 6px; color: {p.text}; }}
QRadioButton:disabled {{ color: {p.muted}; }}
QRadioButton::indicator {{ width: 12px; height: 12px; border: 1px solid {p.muted}; background: {p.surface};
                          border-radius: 7px; }}
QRadioButton::indicator:hover {{ border-color: {p.accent}; }}
QRadioButton::indicator:checked {{ background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
                                  stop:0 {p.accent}, stop:0.55 {p.accent}, stop:0.65 {p.surface}, stop:1 {p.surface});
                                  border-color: {p.accent}; }}

QSlider {{ background: transparent; }}
QSlider::groove:horizontal {{ height: 4px; background: {p.surface2}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {mix(p.accent, p.background, 0.45)}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {p.accent}; width: 12px; margin: -5px 0; border-radius: 6px; }}
QSlider::handle:horizontal:hover {{ background: {accent_hover}; }}

QProgressBar {{ background-color: {p.surface2}; border: 1px solid {p.border}; border-radius: 2px;
               color: {p.strong}; text-align: center; }}
QProgressBar::chunk {{ background-color: {mix(p.accent, p.background, 0.35)}; }}

QTableView, QTableWidget {{ background-color: {p.surface}; alternate-background-color: {p.surface2};
                           color: {p.strong}; gridline-color: {p.border}; border: 1px solid {p.border};
                           selection-background-color: {p.selection}; selection-color: {p.strong}; }}
QTableView::item {{ padding: 0 6px; }}
QHeaderView {{ background-color: {p.surface2}; border: none; }}
QHeaderView::section {{ background-color: {p.surface2}; color: {p.accent}; font-weight: bold;
                       padding: 4px 6px; border: none; border-right: 1px solid {p.border};
                       border-bottom: 1px solid {p.border}; }}
QHeaderView::section:hover {{ background-color: {hover}; }}
QTableCornerButton::section {{ background-color: {p.surface2}; border: none; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: {p.background}; width: 12px; margin: 0; border: none; }}
QScrollBar:horizontal {{ background: {p.background}; height: 12px; margin: 0; border: none; }}
QScrollBar::handle:vertical {{ background: {p.border}; min-height: 24px; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border}; min-width: 24px; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {mix(p.border, p.accent, 0.5)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QSplitter::handle {{ background-color: {p.border}; }}
QSplitter::handle:horizontal {{ width: 5px; }}
QFrame[role="separator"] {{ background-color: {p.border}; max-height: 1px; min-height: 1px; border: none; }}
QWidget#bottomBar {{ background-color: {p.surface}; }}
QWidget#header {{ background-color: {p.background}; }}
QWidget#bottomBar QLabel, QWidget#header QLabel {{ background: transparent; }}
QPushButton#statusDot {{ background: transparent; color: {p.text}; border: 1px solid transparent;
                        border-radius: 3px; padding: 2px 8px; margin: 1px 4px; }}
QPushButton#statusDot:hover {{ background-color: {p.selection}; color: {p.strong}; border-color: {p.border}; }}
QPushButton#statusDot:focus {{ border-color: {p.accent}; }}
QPlainTextEdit[role="code"] {{ background-color: {p.surface2}; color: {p.strong}; }}
QLabel[role="score"] {{ color: {p.accent}; font-weight: bold; font-size: 15pt; }}
QLabel[role="remote"] {{ background-color: {p.remote_bg}; color: {p.remote_fg}; padding: 1px 6px;
                       border-radius: 2px; }}
QProgressBar[role="area"] {{ max-height: 12px; }}
QPushButton#favouritesButton {{ background: transparent; color: {p.warning}; border: 1px solid transparent;
                               border-radius: 3px; padding: 2px 8px; margin: 1px 2px; }}
QPushButton#favouritesButton:hover {{ background-color: {p.selection}; border-color: {p.border}; }}
QWidget#dropTarget {{ border: 1px dashed {p.accent}; }}
{hover_rules(p)}
"""


def hover_colours(p: Palette) -> dict[str, str]:
    """Hover backgrounds: controls, table rows, selected rows under the mouse."""
    return {"control": mix(p.surface2, p.accent, 0.18), "row": mix(p.surface, p.accent, 0.14),
            "row_selected": mix(p.selection, p.accent, 0.22), "frame": mix(p.border, p.accent, 0.65)}


def hover_rules(p: Palette) -> str:
    """Hover and keyboard-focus highlight of every clickable control and box (the
    growing animation is gui/hover.py). Only colours and borders change: never the
    padding or the size, so nothing moves."""
    c = hover_colours(p)
    return f"""
QToolButton {{ border: 1px solid transparent; border-radius: 3px; }}
QToolButton:hover {{ background-color: {c["control"]}; border-color: {p.accent}; color: {p.strong}; }}
QToolButton:focus {{ border-color: {p.accent}; }}
QToolButton#themeButton:hover {{ background-color: {p.selection}; border-color: {p.accent}; }}
QPushButton:focus {{ border: 1px solid {p.accent}; }}
QPushButton:flat:hover {{ background-color: {c["control"]}; border: 1px solid {p.accent}; }}
QPushButton#statusDot:hover, QPushButton#favouritesButton:hover {{ border-color: {p.accent}; }}
QTabBar::tab:hover:!selected {{ background: {c["control"]}; color: {p.strong}; border-color: {p.accent}; }}
QTabBar::tab:selected:hover {{ color: {p.strong}; }}
QGroupBox:hover {{ border-color: {c["frame"]}; }}
QTextBrowser:hover, QListWidget:hover, QTableView:hover, QTableWidget:hover {{ border-color: {c["frame"]}; }}
QTableView::item:hover, QListWidget::item:hover {{ background-color: {c["row"]}; color: {p.strong}; }}
QTableView::item:selected:hover {{ background-color: {c["row_selected"]}; }}
"""


def qt_palette(p: Palette) -> QPalette:
    """QPalette matching the stylesheet, for parts Qt draws without QSS."""
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: p.background, QPalette.ColorRole.WindowText: p.text,
        QPalette.ColorRole.Base: p.surface, QPalette.ColorRole.AlternateBase: p.surface2,
        QPalette.ColorRole.Text: p.strong, QPalette.ColorRole.Button: p.surface2,
        QPalette.ColorRole.ButtonText: p.strong, QPalette.ColorRole.Highlight: p.selection,
        QPalette.ColorRole.HighlightedText: p.strong, QPalette.ColorRole.ToolTipBase: p.surface2,
        QPalette.ColorRole.ToolTipText: p.strong, QPalette.ColorRole.PlaceholderText: p.muted,
        QPalette.ColorRole.Link: p.accent, QPalette.ColorRole.LinkVisited: p.strong,
        QPalette.ColorRole.BrightText: p.accent, QPalette.ColorRole.Light: p.surface2,
        QPalette.ColorRole.Midlight: p.border, QPalette.ColorRole.Mid: p.border,
        QPalette.ColorRole.Dark: p.background, QPalette.ColorRole.Shadow: p.background,
    }
    for role, colour in roles.items():
        pal.setColor(role, QColor(colour))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(p.muted))
    return pal


def set_role(widget: QWidget, role: str | None) -> None:
    """Changes the `role` property and re-polishes the widget so QSS applies."""
    if widget.property("role") == role:
        return
    widget.setProperty("role", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_invalid(widget: QWidget, invalid: bool) -> None:
    if bool(widget.property("invalid")) == invalid:
        return
    widget.setProperty("invalid", invalid)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class _TitleBarFilter(QObject):
    """Gives every top-level window (dialogs and message boxes too) the theme's title bar."""

    def __init__(self, app: QApplication):
        super().__init__(app)
        self.dark = True

    def eventFilter(self, obj, event):  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            set_title_bar(obj, self.dark)
        return False


def apply_theme(app: QApplication, name: str | None) -> str:
    """Applies theme `name` ("dark", "light" or "system") and returns the effective one.

    Can be called again at any time: the stylesheet and palette are application-wide,
    so open dialogs follow too.
    """
    effective = resolve_theme(name)
    p = PALETTES[effective]
    # Once only: with a stylesheet the reported style name is no longer "fusion",
    # and setStyle again would re-polish every widget of the application.
    if not getattr(app, "_reversehire_fusion", False):
        fusion = QStyleFactory.create("Fusion")
        if fusion is not None:
            app.setStyle(fusion)
        app._reversehire_fusion = True
    stylesheet = build_stylesheet(p)
    if app.styleSheet() != stylesheet:  # unchanged theme: no costly re-polish
        pal = qt_palette(p)
        app.setPalette(pal)
        QToolTip.setPalette(pal)
        app.setStyleSheet(stylesheet)
    dark = effective == "dark"
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)
    except (AttributeError, TypeError):
        pass
    title_filter = getattr(app, "_reversehire_title_filter", None)
    if title_filter is None:
        title_filter = _TitleBarFilter(app)
        app.installEventFilter(title_filter)
        app._reversehire_title_filter = title_filter
    title_filter.dark = dark
    for window in app.topLevelWidgets():
        if window.isVisible():
            set_title_bar(window, dark)
    app._reversehire_theme = effective
    return effective


def active_palette(app: QApplication | None = None) -> Palette:
    app = app or QApplication.instance()
    return PALETTES.get(getattr(app, "_reversehire_theme", DEFAULT_THEME), PALETTES[DEFAULT_THEME])


def ensure_application(argv: list[str] | None = None) -> QApplication:
    """The QApplication (created if needed) with the app name, font and icon."""
    app = QApplication.instance()
    if app is None:
        # Qt WebEngine (the map, mapview package) needs shared OpenGL contexts, set
        # before the application object exists; harmless when the map is never opened.
        from PySide6.QtCore import QCoreApplication, Qt

        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        app = QApplication(argv if argv is not None else sys.argv[:1])
    # No applicationDisplayName: some platforms append it to every window title.
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(DEVELOPER)
    if sys.platform.startswith("linux"):
        app.setDesktopFileName(DESKTOP_FILE_NAME)
    app.setFont(app_font())
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    return app

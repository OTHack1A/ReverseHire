# PyInstaller spec of the ready-made builds (one folder, windowed).
#
#     python -m PyInstaller tools/packaging/reversehire.spec --noconfirm \
#         --distpath <out> --workpath <work>
#
# Output:
#   Windows  <out>/ReverseHire/ReverseHire.exe (+ _internal)
#   Linux    <out>/ReverseHire/reversehire     (+ _internal)
#   macOS    <out>/ReverseHire.app
#
# Bundled: the reversehire package with all its data (locales, ATECO table, skills
# dictionary, simulator profiles, affinity knowledge, map web page with Leaflet,
# fonts, icons, the two small ONNX models), Qt (widgets, PDF, WebEngine with its
# helper process and resources), onnxruntime, RapidOCR with its wheel models,
# OpenCV, tokenizers, truststore, pypdf, python-docx, trafilatura and the MCP
# library (assistant strip). NOT bundled (downloaded by the app on first use into
# the data folder): embedding model, translation models, advanced OCR models, map
# data and the company register.
# Build with the project installed: pip install ".[mcp]" pyinstaller

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821 - SPECPATH is set by PyInstaller
PACKAGE = ROOT / "src" / "reversehire"
RESOURCES = PACKAGE / "resources"
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"',
                    (PACKAGE / "_version.py").read_text(encoding="utf-8")).group(1)

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
APP_NAME = "ReverseHire"
EXE_NAME = "reversehire" if sys.platform.startswith("linux") else APP_NAME


def package_data(name, **options):
    try:
        return collect_data_files(name, **options)
    except Exception as error:  # a package missing on this platform
        print(f"[spec] no data collected for {name}: {error}")
        return []


def submodules(name, **options):
    try:
        return collect_submodules(name, **options)
    except Exception as error:
        print(f"[spec] no submodules collected for {name}: {error}")
        return []


datas = []
datas += package_data("reversehire", excludes=["**/__pycache__/**"])
for name in ("rapidocr", "trafilatura", "justext", "docx", "htmldate", "courlan", "fastembed",
             "lingua", "tokenizers", "certifi", "tzdata"):
    datas += package_data(name)

hiddenimports = []
hiddenimports += submodules("reversehire")
hiddenimports += submodules("mcp", filter=lambda m: ".cli" not in m)
hiddenimports += submodules("rapidocr")
hiddenimports += ["truststore", "onnxruntime", "tokenizers", "cv2", "docx", "pypdf",
                  "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtPrintSupport",
                  "PySide6.QtWebChannel", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"]
if WINDOWS:
    hiddenimports += ["win32com", "win32com.client", "pythoncom", "pywintypes"]

excludes = [
    # development and test tools
    "pytest", "_pytest", "pluggy", "iniconfig", "fpdf", "IPython", "jupyter", "notebook",
    # optional extras of the source installation, not part of the ready-made builds
    "playwright", "jobspy", "duckdb", "pandas", "torch", "tensorflow", "matplotlib",
    # GUI toolkits the app does not use
    "tkinter", "_tkinter", "PyQt5", "PyQt6", "PySide2",
    # Qt modules the app does not use. Qt Quick and QML: Qt WebEngine's libraries link
    # them (they stay as libraries), but the QML plugins (about 200 MB with their
    # dependencies) are only collected through these Python modules.
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtWebEngineQuick", "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtLocation", "PySide6.QtPositioning", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtSpatialAudio",
    "PySide6.QtHttpServer", "PySide6.QtWebSockets", "PySide6.QtWebView", "PySide6.QtCanvasPainter",
]

if WINDOWS:
    icon = str(RESOURCES / "icon.ico")
else:
    icon = str(RESOURCES / "icon_256.png")  # converted to .icns on macOS (Pillow)

a = Analysis(  # noqa: F821
    [str(ROOT / "tools" / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

# Chromium locale files of Qt WebEngine: only those of the interface languages.
WEBENGINE_LOCALES = {"en-US", "en-GB", "it", "uk"}


def wanted(destination):
    path = destination.replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if "/qml/" in f"/{path}" and "PySide6" in path:
        return False  # QML plugins (see excludes)
    if ".debug." in name and "PySide6" in path:
        return False  # Qt WebEngine debug resources (devtools symbols)
    if "qtwebengine_locales/" in path and name.endswith(".pak") and name[:-4] not in WEBENGINE_LOCALES:
        return False
    if name.lower().startswith("opencv_videoio_ffmpeg"):
        return False  # video input/output of OpenCV: never used (images only)
    return True


a.datas = [entry for entry in a.datas if wanted(entry[0])]
a.binaries = [entry for entry in a.binaries if wanted(entry[0])]

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=icon,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

if MACOS:
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier="io.github.othack1a.reversehire",
        version=VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "MIT licence - 0THack1A",
            # The PySide6 6.12 and onnxruntime arm64 wheels are built for macOS 14.
            "LSMinimumSystemVersion": "14.0",
        },
    )

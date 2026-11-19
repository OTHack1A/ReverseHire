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

datas += package_data("onnxruntime", includes=["LICENSE", "ThirdPartyNotices.txt", "Privacy.md"])

# Licence notices: the project's notices file and licence texts, plus the licence
# files that every installed distribution ships in its *.dist-info folder.
PACKAGING = ROOT / "tools" / "packaging"
datas += [(str(PACKAGING / "THIRD-PARTY-NOTICES.txt"), "."), (str(PACKAGING / "licenses"), "licenses")]


def distribution_licence_files():
    from importlib.metadata import distributions

    found, seen = [], set()
    for dist in distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name or name.lower() in seen or name.lower() in ("pyinstaller", "pyinstaller-hooks-contrib",
                                                                  "pip", "setuptools", "altgraph", "pefile",
                                                                  "pywin32-ctypes", "macholib", "packaging"):
            continue
        seen.add(name.lower())
        for item in dist.files or []:
            base = item.name.lower()
            if any(word in base for word in ("licen", "copying", "notice", "authors")) and ".dist-info" in str(item):
                source = Path(item.locate())
                if source.is_file():
                    relative = str(item).split(".dist-info/", 1)[-1].replace("\\", "/")
                    target = f"licenses/packages/{name}/" + relative.rsplit("/", 1)[0] if "/" in relative \
                        else f"licenses/packages/{name}"
                    found.append((str(source), target))
    return found


datas += distribution_licence_files()

hiddenimports = []
hiddenimports += submodules("reversehire")
hiddenimports += submodules("mcp", filter=lambda m: ".cli" not in m)
hiddenimports += submodules("rapidocr")
hiddenimports += ["truststore", "onnxruntime", "tokenizers", "cv2", "docx", "pypdf",
                  "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtPrintSupport",
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
    # Qt modules the app does not use. PySide6.QtQuick (and QtQml, which it needs) IS
    # bundled: mapview/webbackend.py imports it so that PySide knows the real types of
    # Qt WebEngine's internal Qt Quick objects (without it the map crashed on Linux and
    # macOS). Its QML plugins and the libraries only they need are filtered out below
    # ("/qml/" and UNUSED_QT).
    "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2", "PySide6.QtWebEngineQuick", "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtLocation", "PySide6.QtPositioning", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtSpatialAudio",
    "PySide6.QtHttpServer", "PySide6.QtWebSockets", "PySide6.QtWebView", "PySide6.QtCanvasPainter",
]

version_info = None
if WINDOWS:
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                     StringTable, VarFileInfo, VarStruct, VSVersionInfo)

    numbers = tuple(int(part) for part in VERSION.split(".")[:3]) + (0,)
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "0THack1A"),
                StringStruct("FileDescription", "ReverseHire"),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", "ReverseHire"),
                StringStruct("LegalCopyright", "Copyright (c) 0THack1A. MIT licence."),
                StringStruct("OriginalFilename", "ReverseHire.exe"),
                StringStruct("ProductName", "ReverseHire"),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

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


# Qt libraries that only QML plugins need (the app loads no QML). Some are GPL-only
# (Qt Graphs, Quick 3D, Virtual Keyboard...): none of them must end up in a build.
UNUSED_QT = re.compile(
    r"(?:^|/|lib)Qt6?(?:Quick3D|3D|Charts|DataVisualization|Graphs|Multimedia|Location|Sensors|Scxml|"
    r"StateMachine|RemoteObjects|TextToSpeech|SpatialAudio|WebView|WebSockets|VirtualKeyboard|"
    r"QuickControls2|QuickDialogs2|QuickTemplates2|QuickParticles|QuickShapes|QuickTimeline|QuickEffects|"
    r"QuickLayouts|QuickVectorImage|QuickTest|QuickNativeStyle|Test|Labs|Sql|QmlLocalStorage|"
    r"QmlXmlListModel|QmlNetwork|QmlCore|QmlDesignSupport|ShaderTools|WebEngineQuick|WebChannelQuick|"
    r"PdfQuick|PositioningQuick|SensorsQuick|MultimediaQuick|WebViewQuick|Lottie|Help|Designer|UiTools|"
    r"Bluetooth|Nfc|SerialPort|SerialBus|HttpServer|Protobuf|Grpc|CanvasPainter)")  # name prefixes


def wanted(destination):
    path = destination.replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if UNUSED_QT.search(path):  # Windows puts the Qt DLLs at the top of _internal
        return False
    if "/qml/" in f"/{path}" and "PySide6" in path:
        return False  # QML plugins (see excludes)
    if ".debug." in name and "PySide6" in path:
        return False  # Qt WebEngine debug resources (devtools symbols)
    if "qtwebengine_locales/" in path and name.endswith(".pak") and name[:-4] not in WEBENGINE_LOCALES:
        return False
    if "virtualkeyboard" in name.lower():
        return False  # Qt Virtual Keyboard: GPL-3.0 only, and not used (input method plugin)
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
    version=version_info,
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

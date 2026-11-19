## How to install

This release contains the **Windows build only** (zip and installer). Pick the file, check it against `SHA256SUMS.txt` if you like, unpack and start. No Python needed.

**Linux and macOS builds are not published yet.** Their automatic start-up test crashes (segmentation fault) as soon as the web map (Qt WebEngine) starts with PySide6 6.12, also when run from the sources, so it is a bug of the app that is still to be fixed. Install from the sources on Linux and macOS for now (docs/INSTALLATION.md); on those systems the web map can crash the program, see README Limits.

The build contains third-party open-source components (Python, Qt/PySide6 under LGPL-3.0 with Chromium through Qt WebEngine, onnxruntime, OpenCV and others), each under its own licence. The notices are inside the package: `THIRD-PARTY-NOTICES.txt` and the `licenses` folder. The Qt libraries are shipped unmodified as separate, replaceable shared libraries.

| System | File | How to start |
|---|---|---|
| Windows 10/11, 64-bit | `ReverseHire-@VERSION@-windows-x64.zip` | Unpack the whole `ReverseHire` folder (for example into `Documents`, not into `C:\Program Files`), double-click `ReverseHire.exe`. |
| Windows 10/11, 64-bit, installer | `ReverseHire-@VERSION@-setup.exe` (if present) | Run it: per-user installation into `%LOCALAPPDATA%\Programs\ReverseHire Standalone`, no administrator rights, with a Start menu entry. |
| Linux, macOS | not published yet | Install from the sources (docs/INSTALLATION.md). |

**Intel Macs are not supported by the ready-made build** (only Apple Silicon is built; the ONNX runtime has no current release for Intel Macs).

### System requirements

| | Minimum |
|---|---|
| Windows | Windows 10 version 1809 (build 17763) or later, or Windows 11; x64 only (Python 3.14: Windows 10+; Qt 6.12: Windows 10 1809+) |
| Linux | source installation only in this release (Python 3.12 or later, X11 or Wayland desktop with the Qt libraries; docs/INSTALLATION.md); ready-made build not published yet |
| macOS | source installation only in this release: macOS 14 or later (PySide6 6.12 wheels), Apple Silicon; a future ready-made build: Qt 6.12 declares macOS 14.4, not verified on a real Mac |
| From the sources | Python 3.12 or later; the Windows build carries Python 3.14 |
| Processor, memory | 64-bit; about 4 GB of free RAM (8 GB of RAM recommended), more while the advanced text recognition runs |
| Disk | about 1 GB for the unpacked program, about 470 MB for the scoring model, plus the optional downloads below |
| Screen | 1366 × 768 or larger |
| Internet | only for searches and first-time downloads |
| Optional | Claude Code or Codex CLI (assistant strip, suggestions); classic or new Outlook for e-mail drafts on Windows |

### The build is not code-signed

- **Windows SmartScreen** says "Windows protected your PC" and "Unknown publisher": click **More info**, then **Run anyway**.
- **Windows with Smart App Control on** blocks unsigned programs outright and offers no "run anyway": on such PCs install from the sources with `start.bat` instead (docs/INSTALLATION.md, section 3).

### Where the data goes

- Windows zip: the `userdata` folder next to the program, when that folder can be written; otherwise the per-user folder (`%LOCALAPPDATA%\ReverseHire`).
- Windows installer: `userdata` inside the installation folder.
- `REVERSEHIRE_HOME` overrides all of them.

### Downloaded on first use (not in the download)

The scoring model `intfloat/multilingual-e5-small` (about 470 MB, the first time the scoring needs it), and only on request: the advanced text recogniser (about 147 MB), the map data of a region (about 205 MB for Emilia-Romagna), the regional register (from several hundred MB to a few GB of raw files) and the translation models. They go into the data folder.

### How this build was checked

The Windows build was started on GitHub's Windows Server runner with `--self-test`: it loads the locales, the bundled ONNX models, the face detector, the text recognition, creates the main window offscreen, opens the database in a temporary folder and shows the web map (Qt WebEngine) with tiles from a local test server; then its built-in MCP server answered an `initialize` request. **No person has run it yet** on a real desktop; please report problems on the Issues page.

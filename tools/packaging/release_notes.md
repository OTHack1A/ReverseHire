## How to install

Ready-made builds: no Python needed. Pick the file for your system, check it against `SHA256SUMS.txt` if you like, unpack and start.

| System | File | How to start |
|---|---|---|
| Windows 10/11, 64-bit | `ReverseHire-@VERSION@-windows-x64.zip` | Unpack the whole `ReverseHire` folder (for example into `Documents`, not into `C:\Program Files`), double-click `ReverseHire.exe`. |
| Windows 10/11, 64-bit, installer | `ReverseHire-@VERSION@-setup.exe` (if present) | Run it: per-user installation into `%LOCALAPPDATA%\Programs\ReverseHire Standalone`, no administrator rights, with a Start menu entry. |
| Linux x86_64, glibc 2.35 or later (Ubuntu 22.04+, Debian 12+, Fedora 36+) | `ReverseHire-@VERSION@-linux-x86_64.tar.gz` | `tar xzf ReverseHire-@VERSION@-linux-x86_64.tar.gz`, then `ReverseHire/reversehire`. `ReverseHire/install-desktop-entry.sh` adds it to the applications menu. |
| macOS 14 Sonoma or later, Apple Silicon (M1 and later) | `ReverseHire-@VERSION@-macos-arm64.dmg` | Open the disk image, drag `ReverseHire.app` to `Applications`. |

**Intel Macs are not supported by the ready-made build** (only Apple Silicon is built; the ONNX runtime has no current release for Intel Macs).

### System requirements

| | Minimum |
|---|---|
| Windows | Windows 10 version 1809 (build 17763) or later, or Windows 11; x64 only (Python 3.14: Windows 10+; Qt 6.12: Windows 10 1809+) |
| Linux | x86_64, glibc 2.35 or later (built on Ubuntu 22.04; Ubuntu 22.04+, Debian 12+, Fedora 36+), X11 or Wayland desktop with the Qt libraries listed below |
| macOS | macOS 14 Sonoma or later, Apple Silicon only (the PySide6 6.12 and onnxruntime arm64 wheels target macOS 14) |
| From the sources | Python 3.12 or later; these builds carry Python 3.14 |
| Processor, memory | 64-bit; about 4 GB of free RAM (8 GB of RAM recommended), more while the advanced text recognition runs |
| Disk | about 1 GB for the unpacked program, about 470 MB for the scoring model, plus the optional downloads below |
| Screen | 1366 × 768 or larger |
| Internet | only for searches and first-time downloads |
| Optional | Claude Code or Codex CLI (assistant strip, suggestions); classic or new Outlook for e-mail drafts on Windows |

### The builds are not code-signed

- **Windows SmartScreen** says "Windows protected your PC" and "Unknown publisher": click **More info**, then **Run anyway**.
- **Windows with Smart App Control on** blocks unsigned programs outright and offers no "run anyway": on such PCs install from the sources with `start.bat` instead (docs/INSTALLATION.md, section 5).
- **macOS Gatekeeper** refuses the app the first time ("cannot be opened because Apple cannot check it"): right-click (Control-click) `ReverseHire.app` and choose **Open**, or open **System Settings > Privacy & Security** and click **Open Anyway** (on macOS 15 only the second way works). Alternative in Terminal: `xattr -dr com.apple.quarantine /Applications/ReverseHire.app`.
- **Linux** needs the usual desktop libraries of Qt (X11/XCB or Wayland, EGL/OpenGL, fontconfig, NSS). On Debian/Ubuntu: `sudo apt install libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libasound2` (`libasound2t64` on Ubuntu 24.04).

### Where the data goes

- Windows and Linux (zip or tar.gz): the `userdata` folder next to the program, when that folder can be written; otherwise the per-user folder (`%LOCALAPPDATA%\ReverseHire`, `~/.local/share/reversehire`).
- Windows installer: `userdata` inside the installation folder.
- macOS: `~/Library/Application Support/ReverseHire` (an app bundle is not a place for data).
- `REVERSEHIRE_HOME` overrides all of them.

### Downloaded on first use (not in the download)

The scoring model `intfloat/multilingual-e5-small` (about 470 MB, the first time the scoring needs it), and only on request: the advanced text recogniser (about 147 MB), the map data of a region (about 205 MB for Emilia-Romagna), the regional register (from several hundred MB to a few GB of raw files) and the translation models. They go into the data folder.

### How these builds were checked

Each build was started on GitHub's machines (Windows Server, Ubuntu 22.04 under a virtual X server, macOS 14 on Apple Silicon) with `--self-test`: it loads the locales, the bundled ONNX models, the face detector, the text recognition, the web map (Qt WebEngine, tiles from a local test server), creates the main window offscreen and opens the database in a temporary folder; then the built-in MCP server answered an `initialize` request. **No person has run these builds yet** on a real Windows, Linux or macOS desktop; please report problems on the Issues page.

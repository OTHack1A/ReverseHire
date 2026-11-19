## How to install

Pick the file for your system, check it against `SHA256SUMS.txt` if you like, and start it. No Python needed.

**Fixed in this release: the web map no longer crashes the program on Linux and macOS.**

The builds contain third-party open-source components (Python, Qt/PySide6 under LGPL-3.0 with Chromium through Qt WebEngine, onnxruntime, OpenCV and others), each under its own licence. The notices are inside every package: `THIRD-PARTY-NOTICES.txt` and the `licenses` folder (in the macOS disk image next to the app, and inside the app under `Contents/Resources`). The Qt libraries are shipped unmodified as separate, replaceable shared libraries.

| System | File | How to start |
|---|---|---|
| Windows 10/11, 64-bit | `ReverseHire-@VERSION@-windows-x64.zip` | Unpack the whole `ReverseHire` folder (for example into `Documents`, not into `C:\Program Files`), double-click `ReverseHire.exe`. |
| Windows 10/11, 64-bit, installer | `ReverseHire-@VERSION@-setup.exe` (if present) | Run it: per-user installation into `%LOCALAPPDATA%\Programs\ReverseHire Standalone`, no administrator rights, with a Start menu entry. |
| Linux x86_64 | `ReverseHire-@VERSION@-linux-x86_64.tar.gz` | `tar xzf` the file, run `ReverseHire/reversehire`; `ReverseHire/install-desktop-entry.sh` adds a menu entry. |
| macOS, Apple Silicon | `ReverseHire-@VERSION@-macos-arm64.dmg` | Open the disk image and drag `ReverseHire.app` to Applications. |

**Intel Macs are not supported by the ready-made build** (only Apple Silicon is built; the ONNX runtime has no current release for Intel Macs).

### System requirements

| | Minimum |
|---|---|
| Windows | Windows 10 version 1809 (build 17763) or later, or Windows 11; x64 only (Python 3.14: Windows 10+; Qt 6.12: Windows 10 1809+) |
| Linux | x86_64 only; glibc 2.35 or later (measured: the highest glibc symbol version the binaries need is GLIBC_2.35; Ubuntu 22.04+, Debian 12+, Fedora 36+); X11 or Wayland desktop with the Qt libraries listed below |
| macOS | Apple Silicon only (no Intel Macs); macOS 14.4 or later declared by Qt 6.12; started only on macOS 15 (GitHub runner), not verified on a real Mac or on macOS 14 |
| From the sources | Python 3.12 or later; the ready-made builds carry Python 3.14 |
| Processor, memory | 64-bit; about 4 GB of free RAM (8 GB of RAM recommended), more while the advanced text recognition runs |
| Disk | about 1 GB for the unpacked program, about 470 MB for the scoring model, plus the optional downloads below |
| Screen | 1366 x 768 or larger |
| Internet | only for searches and first-time downloads |
| Optional | Claude Code or Codex CLI (assistant strip, suggestions); classic or new Outlook for e-mail drafts on Windows |

### The builds are not code-signed

- **Windows SmartScreen** says "Windows protected your PC" and "Unknown publisher": click **More info**, then **Run anyway**.
- **Windows with Smart App Control on** blocks unsigned programs outright and offers no "run anyway": on such PCs install from the sources with `start.bat` instead (docs/INSTALLATION.md, section 3).
- **macOS Gatekeeper** refuses the unsigned app the first time: Control-click `ReverseHire.app` and choose **Open**, or open System Settings > Privacy & Security and click **Open Anyway** (on macOS 15 only this second way works). In Terminal: `xattr -dr com.apple.quarantine /Applications/ReverseHire.app`.
- **Linux** needs the desktop libraries that Qt uses; a desktop installation normally has them. Debian/Ubuntu: `sudo apt install libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libasound2` (`libasound2t64` on Ubuntu 24.04).

### Where the data goes

- Windows zip and Linux tar.gz: the `userdata` folder next to the program, when that folder can be written; otherwise the per-user folder (`%LOCALAPPDATA%\ReverseHire` on Windows, `~/.local/share/reversehire` on Linux).
- Windows installer: `userdata` inside the installation folder.
- macOS: `~/Library/Application Support/ReverseHire`.
- `REVERSEHIRE_HOME` overrides all of them.

### Downloaded on first use (not in the download)

The scoring model `intfloat/multilingual-e5-small` (about 470 MB, the first time the scoring needs it), and only on request: the advanced text recogniser (about 147 MB), the map data of a region (about 205 MB for Emilia-Romagna), the regional register (from several hundred MB to a few GB of raw files) and the translation models. They go into the data folder.

### How these builds were checked

The three builds were made and started on GitHub's runners (Windows Server, Ubuntu 22.04 under a virtual X server, macOS 15 on Apple Silicon) with `--self-test`: it loads the locales, the bundled ONNX models, the face detector and the text recognition, creates the main window offscreen with the database in a temporary folder, and then shows the web map (Qt WebEngine) with tiles from a local test server while the main window is open. Each build's built-in MCP server then answered an `initialize` request. **No person has run these builds yet on a real Windows, Linux or macOS desktop**; please report problems on the Issues page.

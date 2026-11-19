# ReverseHire — Installation Manual

This manual explains how to install ReverseHire on Windows, on Linux and on macOS. How to use the app is covered in the [User Manual](USER_MANUAL.md).

There are three ways to install it:

| Way | For whom | Internet needed |
|---|---|---|
| **A. A ready-made build** (Windows, Linux, macOS on Apple Silicon): see [Download a ready-made build](#download-a-ready-made-build) just below | Anyone. No Python and nothing else to install. | Only to download the build |
| **B. Windows: from the source code** (`start.bat`, [section 3](#3-windows-from-the-source-code)) | Developers, or anyone who wants to change the code. | Yes |
| **C. Linux and macOS: from the source code** (`start.sh`, `start.command`, [section 4](#4-linux-and-macos-what-is-needed)) | Anyone on Linux or macOS who prefers the source. | Yes |

No ready-made installation disc (ISO) is distributed. Maintainers can build an offline installation disc themselves, for PCs without internet: see [section 11](#11-building-an-offline-installation-disc-yourself-maintainer-procedure).

None of the ways needs administrator rights.

## Download a ready-made build

Every release on the [Releases page](https://github.com/OTHack1A/ReverseHire/releases/latest) carries ready-made builds made by GitHub Actions (workflow `.github/workflows/release.yml`, PyInstaller, Python 3.14). They contain Python, Qt and every library; nothing has to be installed first.

| System | File | Start |
|---|---|---|
| Windows 10/11, 64-bit | `ReverseHire-<version>-windows-x64.zip` | Unpack the whole `ReverseHire` folder somewhere you can write (for example `Documents`; not `C:\Program Files`) and double-click `ReverseHire.exe`. |
| Windows 10/11, 64-bit | `ReverseHire-<version>-setup.exe` (when the release has it) | Run it: it installs for your user only into `%LOCALAPPDATA%\Programs\ReverseHire Standalone`, without administrator rights, with a Start menu entry; uninstall from Settings > Apps. |
| Linux x86_64 with glibc 2.35 or later (Ubuntu 22.04+, Debian 12+, Fedora 36+, recent openSUSE and Arch) | `ReverseHire-<version>-linux-x86_64.tar.gz` | `tar xzf ReverseHire-<version>-linux-x86_64.tar.gz`, then `ReverseHire/reversehire`. `ReverseHire/install-desktop-entry.sh` adds it to the applications menu of your user (run it again after moving the folder; `--remove` takes it away). |
| macOS 14 Sonoma or later, Apple Silicon (M1 and later) | `ReverseHire-<version>-macos-arm64.dmg` | Open the disk image and drag `ReverseHire.app` onto `Applications`. |

`SHA256SUMS.txt` lists the SHA-256 checksum of every file (`certutil -hashfile <file> SHA256` on Windows, `sha256sum` on Linux, `shasum -a 256` on macOS).

**Not available:** Intel Macs are **not supported by the ready-made build**: only Apple Silicon is built (the ONNX runtime used for text recognition and scoring has no release for macOS x86_64 after version 1.23, and none for Python 3.14; the source installation with Python 3.12 or 3.13, [section 6](#6-macos-from-the-source-code), may work, untested). Linux on ARM (aarch64): install from the sources. The minimum versions are in [section 1](#1-requirements).

**The builds are not code-signed.** There is no code-signing certificate, so the systems treat them as programs of an unknown publisher:

- **Windows SmartScreen** shows "Windows protected your PC" with "Unknown publisher": click **More info**, then **Run anyway**. It asks once per downloaded copy.
- **Smart App Control** (Windows 11, *Settings > Privacy & security > Windows Security > App & browser control*) blocks unsigned programs outright when it is **On**, with no "run anyway". On such a PC use only the installation from the source code ([section 3](#3-windows-from-the-source-code), `start.bat`): it runs Python from python.org, which is signed. Do not switch Smart App Control off just for this: on many Windows versions, once it is off it cannot be switched on again without reinstalling Windows.
- **macOS Gatekeeper** refuses the app the first time ("ReverseHire cannot be opened because Apple cannot check it for malicious software"). Then either Control-click (right-click) `ReverseHire.app` in Applications and choose **Open**, or open **System Settings > Privacy & Security**, scroll to the message about ReverseHire and click **Open Anyway** (on macOS 15 Sequoia only this second way works). In Terminal the same is `xattr -dr com.apple.quarantine /Applications/ReverseHire.app`. Do it only for a file downloaded from this project's Releases page.
- **Linux** needs the desktop libraries that Qt uses, which a desktop installation normally has. If the program does not open and the terminal mentions `xcb`, `EGL` or a "platform plugin", install them (Debian/Ubuntu): `sudo apt install libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libasound2` (on Ubuntu 24.04 `libasound2t64`). If the map stays empty on a system that restricts user namespaces (Ubuntu 24.04 and later), start it with `QTWEBENGINE_DISABLE_SANDBOX=1 ReverseHire/reversehire`.

**Where the data goes.** The same rules as every installation ([section 2](#2-where-your-data-is)): the zip and tar.gz builds use the `userdata` folder next to the program when that folder can be written (so the whole program with its data can be moved or copied as one folder), otherwise the per-user folder; the Windows installer uses `userdata` inside its installation folder; the macOS app always uses `~/Library/Application Support/ReverseHire`, because an app bundle is not a place for data. `REVERSEHIRE_HOME` overrides them. Updating: replace the program files and keep `userdata` (the Windows installer does this by itself).

**Downloaded on first use, not part of the download** (into the data folder, [section 10](#10-first-run-and-what-is-recreated-or-downloaded)): the scoring model `intfloat/multilingual-e5-small` (about 470 MB, the first time the scoring needs it) and, only when you ask: the advanced text recogniser (about 147 MB), the map data of a region (about 205 MB for Emilia-Romagna), the regional register (from several hundred MB to a few GB of raw files), the translation models. The download itself is about 400 to 500 MB and unpacks to about 1 GB. The translation models are fetched from a terminal: `ReverseHire.exe -m reversehire.cv.translate download it uk` on Windows (it prints nothing; the files appear in `userdata\models\translation`), `ReverseHire/reversehire -m reversehire.cv.translate download it uk` on Linux, `/Applications/ReverseHire.app/Contents/MacOS/ReverseHire -m reversehire.cv.translate download it uk` on macOS.

**The assistant strip and AI clients** start the built-in MCP server with the program itself (`ReverseHire.exe -m reversehire.mcp_server`); the MCP window shows the exact command. `--self-test` checks a copy without touching your data (see `src/reversehire/selftest.py`).

**How far these builds were checked.** Each one was built and started on GitHub's machines (Windows Server, Ubuntu 22.04 under a virtual X server, macOS 15 on Apple Silicon) with `--self-test`: locales, the bundled ONNX models, the face detector, the text recognition, the web map (Qt WebEngine with tiles from a local test server), the main window (offscreen) and the database in a temporary folder; then the built-in MCP server had to answer an `initialize` request. **No person has used these builds on a real desktop yet**, on any of the three systems; in particular the first-use downloads, the e-mail drafts and the file dialogs were not tried in a ready-made build. On Windows the developer's PC has Smart App Control on, so the Windows build could not be run there either.

## 1. Requirements

| Item | Requirement |
|---|---|
| Windows | Windows 10 version 1809 (build 17763) or later, or Windows 11; x64 only. Python 3.14 supports Windows 10 and newer, and Qt 6.12 (PySide6 6.12) Windows 10 1809 or later. Tested on Windows 11 (developer's PC) and on GitHub's Windows Server runner. |
| Linux | Ready-made build: x86_64 with glibc 2.35 or later (Ubuntu 22.04+, Debian 12+, Fedora 36+); it is built on Ubuntu 22.04, and PySide6 6.12 itself needs glibc 2.34. From source: also aarch64. A graphical desktop (X11 or Wayland) with the Qt libraries of [section 5.1](#51-system-packages). Ready-made build tested only on GitHub's Ubuntu 22.04 runner (virtual X server). |
| macOS | macOS 14 Sonoma or later, Apple Silicon only: the PySide6 6.12 and onnxruntime wheels for Apple Silicon are built for macOS 14. **Intel Macs are not supported by the ready-made build**; from source see [section 6](#6-macos-from-the-source-code). Ready-made build tested only on GitHub's macOS 15 runner (Apple Silicon). |
| Python | Not needed by the ready-made builds (they carry Python 3.14). From source: Python 3.12 or later (`requires-python`; tested with 3.14) |
| Processor | 64-bit |
| Screen | 1366 × 768 or larger (the window is designed for 1366 pixels) |
| Optional programs | Claude Code or Codex CLI for the assistant strip and the suggestions; classic or new Outlook for e-mail drafts on Windows (otherwise Thunderbird, Apple Mail, the default mail program or Gmail in the browser) |
| Free disk space | Ready-made build: about 1 GB unpacked (measured: 972 MB on Windows) plus the scoring model (about 470 MB) and the optional downloads below. From source: about 3 GB (program 1.4 GB, of which about 150 MB for the local text recognition (OCR) of scanned CVs; embedding model 0.5 GB; Python 0.2 GB; working space), plus about 147 MB for the optional advanced reader of the text recognition (downloaded on request), plus about 205 MB for the map data of a region such as Emilia-Romagna (downloaded on request, together with the index built from it), the local translation models if you use them, and the files of the optional regional register (downloaded on request; the program shows the size before it downloads, and the developer's notes in the code put the largest files at between several hundred MB and a few GB: see [section 8](#8-where-everything-is)) |
| Memory | About 4 GB of free RAM for normal use (8 GB of RAM recommended); more while the advanced text recognition runs |
| Administrator rights | Not needed |
| Internet | Ready-made build: only to download it. From source: needed to install. In both cases needed when you run a search (maps, company websites). |

ReverseHire is developed on Windows. Linux and macOS run the same code; the ready-made builds are started and checked on all three systems by `.github/workflows/release.yml`.

## 2. Where your data is

ReverseHire keeps your settings (`config.json`, which also holds the text and details of your application e-mails and the assistant choice), saved searches, company cards and caches (`store.sqlite`, a local SQLite database), the log (`reversehire.log`, rolling, 100 MB at most in total, see the user manual), the downloaded map data and its index (`osm`), the regional register and the files downloaded for it (`registers`), the tracker of your applications and the exact copies of the CVs you sent (`applications`), the queue of the idle analysis (`idle_analysis.json`), the copies of your CV files (`cv`), the working folder of the assistant strip (`assistant`), the MCP usage counter (`mcp_usage.json`) and downloaded models (`models`) in one **data folder**. It is chosen when the app starts, with these rules (the first that applies wins):

1. **`REVERSEHIRE_HOME`**: if this environment variable is set, that folder is used.
2. **Portable**: if the program folder can be written, the data goes into a folder named **`userdata` inside the program folder**, next to the program. The program folder is the project folder when you run from the source code, the folder that holds `ReverseHire.exe` / `reversehire` for a [ready-made build](#download-a-ready-made-build) (a macOS `ReverseHire.app` has no program folder for data: it always uses the per-user folder). Nothing has to be configured: a writable program folder means portable. This is the normal case for every installation described here.
3. **Per-user folder** of the operating system, when the program folder is read-only (for example a copy in a system folder):

   | System | Folder |
   |---|---|
   | Windows | `%LOCALAPPDATA%\ReverseHire` (normally `C:\Users\<you>\AppData\Local\ReverseHire`) |
   | Linux | `$XDG_DATA_HOME/reversehire`, normally `~/.local/share/reversehire` |
   | macOS | `~/Library/Application Support/ReverseHire` |

4. As a last resort, a folder in the system's temporary folder. The app writes a warning in the log and **Diagnostics** shows it: data there may be deleted by the system, so set `REVERSEHIRE_HOME`.

The **?** menu, **Diagnostics…**, shows the data folder, which rule chose it and whether it can be written (the **About** window no longer lists it), and the log records it at every start.

**Older versions** kept the data in `%APPDATA%\ReverseHire` (Windows) or `~/.reversehire` (Linux, macOS). On the first start with a new data folder that has no settings and no database yet, ReverseHire **copies** (never moves) the settings, the database and the log from the old folder, and writes `migrated_from.txt` to say so. The old folder is left as it was: delete it yourself once everything works. Models and map data are not copied: the app still finds a model in the old folder (it is the last of the folders searched, see [section 8](#8-where-everything-is)).

**Cloud-synced folders.** Do not put the data folder inside OneDrive, Dropbox, Google Drive, iCloud Drive and similar: the sync client can upload or replace `store.sqlite` while the app writes it, which gives conflicting copies or lost searches. Diagnostics warns when it detects such a folder. If the program folder is inside one (for example a project cloned into a synced Documents folder), set `REVERSEHIRE_HOME` to a folder outside it.

**Moving the data folder.** Close ReverseHire, move or copy the whole data folder where you want it, and set `REVERSEHIRE_HOME` to the new place:

- Windows: **Settings > System > About > Advanced system settings > Environment Variables**, *User variables*, **New…**: name `REVERSEHIRE_HOME`, value the folder (or in a terminal: `setx REVERSEHIRE_HOME "D:\ReverseHire data"`). No administrator rights are needed for user variables.
- Linux: add `export REVERSEHIRE_HOME="$HOME/ReverseHireData"` to `~/.profile` (or `~/.bashrc`), then log in again.
- macOS: add the same line to `~/.zprofile`; for the app started from Finder with `start.command`, the Terminal reads it.

To go back to the portable folder, remove the variable and move the files back into `userdata`.

## 3. Windows: from the source code

Use this way if you want to read or change the code. It needs an internet connection.

### 3.1 Tools needed

| Tool | Version | Where to get it | Notes |
|---|---|---|---|
| Python | 3.12 or later (tested with 3.14), 64-bit | <https://www.python.org/downloads/windows/> | During setup, tick **Add python.exe to PATH**. The `py` launcher installed with it is enough. |
| Git (optional) | any recent | <https://git-scm.com/download/win> | Only to clone the repository; you can download the ZIP from GitHub instead. |

### 3.2 Steps

Get the code (with Git, or by unpacking the ZIP from GitHub):

```
git clone https://github.com/OTHack1A/ReverseHire.git
```

Then double-click **`start.bat`** in the project folder. The first time it finds Python (3.14, 3.13 or 3.12 through the `py` launcher, else `python`), creates the environment `.venv`, installs the dependencies and the app (a few minutes), then starts ReverseHire. Later starts open the app at once. If something fails, delete `.venv` and run `start.bat` again.

The same by hand, in a terminal opened in the project folder:

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\pythonw.exe -m reversehire
```

Your data goes to `userdata` in the project folder (ignored by Git). With this installation the embedding model (about 470 MB) is downloaded the first time a search reaches the scoring step, into `userdata\models`. The local translation models for non-English CVs and the map data of a region are separate downloads, described in [section 8](#8-where-everything-is). To avoid the download (for example without internet, or behind a proxy), copy the `models` folder of another installation into the project folder as `models\`: it is found there (see [Where everything is](#8-where-everything-is)). HTTPS connections, the model download included, trust the system certificate store (through the `truststore` package), so antivirus HTTPS scanning and company proxies with their own certificate work as in the browser.

## 4. Linux and macOS: what is needed

Both use the source code and the scripts in the project folder: **`start.sh`** on Linux, **`start.command`** on macOS (it runs `start.sh`). The first start finds Python 3.12 or later (`python3.14`, `python3.13`, `python3.12`, `python3`), creates `.venv`, installs the dependencies with `pip` (internet, a few minutes, about 1.5 GB) and starts the app; later starts open it at once. Your data goes to `userdata` in the project folder ([section 2](#2-where-your-data-is)). Nothing is installed outside the project folder and your home folder, so no administrator rights are needed — except, on Linux, for the system packages below if they are missing.

All the Python packages have ready-made binary wheels for Linux x86_64 and aarch64 and for macOS on Apple silicon, for Python 3.12, 3.13 and 3.14: nothing is compiled.

## 5. Linux: from the source code

### 5.1 System packages

Python 3.12 or later with the `venv` module, and the libraries Qt needs to open a window. Most desktop installations already have the libraries. If they are missing (the app stops with an error that mentions `xcb`, `EGL` or `libxkbcommon`), ask the administrator to install them, or install them yourself if you can:

| Distribution | Command |
|---|---|
| Debian, Ubuntu, Mint | `sudo apt install python3 python3-venv libegl1 libgl1 libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libfontconfig1 libdbus-1-3` |
| Fedora | `sudo dnf install python3 mesa-libEGL mesa-libGL libxkbcommon libxkbcommon-x11 xcb-util-cursor fontconfig dbus-libs` |
| Arch, Manjaro | `sudo pacman -S python libglvnd libxkbcommon libxkbcommon-x11 xcb-util-cursor fontconfig` |

On a distribution whose Python is older than 3.12, install a newer one without administrator rights with `uv` (<https://docs.astral.sh/uv/>, `uv python install 3.13`) or `pyenv`, and make sure `python3.13` is on the `PATH`.

### 5.2 Steps

```
git clone https://github.com/OTHack1A/ReverseHire.git
cd ReverseHire
bash start.sh
```

(`./start.sh` works too when the file is executable: `chmod +x start.sh`.) To start it from the desktop menu, create `~/.local/share/applications/reversehire.desktop` with:

```
[Desktop Entry]
Type=Application
Name=ReverseHire
Exec=/path/to/ReverseHire/start.sh
Icon=/path/to/ReverseHire/src/reversehire/resources/icon_256.png
Terminal=false
StartupWMClass=reversehire
```

"Open log folder" opens the data folder with your desktop's file manager (`xdg-open`). The **system** theme follows GNOME's light/dark setting; on other desktops choose dark or light in the settings.

## 6. macOS: from the source code

### 6.1 Python

Install Python 3.12 or later from <https://www.python.org/downloads/macos/> (installs for all users, asks for the password), or without administrator rights with Homebrew in your home folder or `uv python install 3.13`. Apple's `/usr/bin/python3` is too old.

**Intel Macs:** `onnxruntime` (the scoring engine) has no build for Intel Macs after version 1.23.2, which supports Python up to 3.13. Use Python 3.12 or 3.13 there; with Python 3.14 the installation fails. Apple-silicon Macs have no such limit.

### 6.2 Steps

Download the code (ZIP from GitHub, or `git clone https://github.com/OTHack1A/ReverseHire.git`) and double-click **`start.command`** in Finder. A Terminal window shows the first-time setup, then the app opens.

- If macOS says the file cannot be opened because it is from an unidentified developer, right-click it, choose **Open**, then **Open** again.
- If it opens in a text editor or says it has no permission, run once in Terminal, in the project folder: `chmod +x start.command start.sh` (files unpacked from a ZIP lose this flag).

"Open log folder" opens the data folder in Finder. The **system** theme follows the macOS appearance (light or dark).

## 7. Optional components

None of these is needed for normal use. On Linux and macOS replace `.venv\Scripts\python.exe` with `.venv/bin/python`.

| Component | What it is for | How to add it |
|---|---|---|
| **MCP server** | Lets Claude (Code and Desktop), Codex or Gemini CLI use ReverseHire's functions. | The ready-made builds include the `mcp` library; it is **off by default**: turn it on with the red "MCP off" dot at the top right of the app window. The same window shows the configuration to paste into each client and the exact command (in a ready-made build the program itself: `ReverseHire.exe -m reversehire.mcp_server`; from source: `.venv/bin/reversehire-mcp` or `.venv\Scripts\reversehire-mcp.exe`). It uses the same data folder as the app. See [MCP.md](MCP.md). From source: `pip install -e .[mcp]`. |
| **Assistant strip** | Questions about your searches and companies, answered by **your own** Claude Code or Codex. | ReverseHire does not install those programs: install one of them and sign in (follow its own instructions), have the `mcp` library (included in the ready-made builds; from source `pip install -e .[mcp]`), switch MCP on with the dot at the top right, and choose the assistant in **Settings > Assistant and MCP**. Questions count against your own plan. |
| **Job boards** (experimental) | Also looks for hiring companies on Indeed and LinkedIn, through the JobSpy library. Automated access may violate those sites' terms of use and can be blocked: in a test on the development PC LinkedIn answered (without job descriptions) and Indeed failed with a certificate error. | **Not included in the ready-made builds** (JobSpy is not bundled): use the installation from the source code and run `pip install -e .[jobboards]`. To use it, tick **Also search job boards (Indeed/LinkedIn)** in the app's settings; it is off by default. |
| **Playwright engine** | Reads websites that show their content only through JavaScript. | Not included in the ready-made builds: use the source installation. Needs internet and several hundred MB. In a terminal in the project folder: `.venv\Scripts\python.exe -m pip install playwright`, then `.venv\Scripts\python.exe -m playwright install chromium` (on Linux also `-m playwright install-deps chromium`, which needs administrator rights). Then choose it in the app's settings. |
| **Firecrawl engine** | External service that reads websites for you. | No installation: enter your own Firecrawl API key in the app's settings. |
| **Advanced reader of the text recognition** | A more accurate (and slower) set of OCR models for photos and difficult scans. About 147 MB, from Hugging Face, checked against fixed fingerprints. | Nothing to install: the program offers it the first time a scan reads poorly, or use **Tools > Advanced reader (OCR) > Download the advanced reader** or the OCR part of **Settings > General**. It is stored in `models\ocr\advanced-v1` of the data folder (or of any model folder, see [section 8](#8-where-everything-is)). On a PC without internet, copy that folder from another installation. |
| **GPU for the advanced reader** (Windows) | Runs the advanced reader on any DirectX 12 graphics card (about three times faster than the processor). Not needed, and **not included in the ready-made builds**: it needs the source installation. | `onnxruntime-directml` replaces the files of `onnxruntime`, so swap them, do not add it: `.venv\Scripts\python.exe -m pip uninstall -y onnxruntime`, then `.venv\Scripts\python.exe -m pip install onnxruntime-directml==1.24.4` (or, in the project folder, uninstall `onnxruntime`, then `pip install -e .[gpu]`). Detected at run time; without it, or if the test at start fails, the processor is used. **Diagnostics** warns when both packages are installed. |
| **Overture Maps places** | An extra, optional source of companies on the location tab (off by default). | Not included in the ready-made builds (`duckdb` is not bundled): use the source installation, `pip install -e .[overture]` (installs `duckdb`), then tick the source on the location tab. |

## 8. Where everything is

| What | Ready-made build | From source (any system) |
|---|---|---|
| Program | the unpacked `ReverseHire` folder (zip, tar.gz); `%LOCALAPPDATA%\Programs\ReverseHire Standalone` (Windows installer); `ReverseHire.app` (macOS) | the project folder (environment `.venv`) |
| Data folder: settings (`config.json`), saved searches (`store.sqlite`), log (`reversehire.log`) | `userdata` next to the program (inside the installation folder for the Windows installer); macOS: `~/Library/Application Support/ReverseHire` | `<project>/userdata` |
| Embedding model | `models` in the data folder, downloaded on first use (about 470 MB) | downloaded into `<project>/userdata/models`, or copied by you into `<project>/models` |
| Local translation models (Italian and Ukrainian CVs into English) | `models/translation` in one of the model folders below (not in the download) | the same, `models/translation` |
| Advanced reader of the text recognition (optional, about 147 MB) | `models/ocr/advanced-v1` in one of the model folders below | the same, `models/ocr/advanced-v1` |
| Map data of a region and its index | `osm` in the data folder | `<data folder>/osm` |
| Regional register (database and downloaded source files, optional) | `registers` in the data folder | `<data folder>/registers` |
| Applications sent (records, cover letters, copies of the CVs sent) | `applications` in the data folder | `<data folder>/applications` |
| Queue of the idle analysis | `idle_analysis.json` in the data folder | `<data folder>/idle_analysis.json` |
| Caches of the job search and of the map tiles | `cache/jobsearch`, `cache/map` in the data folder | `<data folder>/cache/...` |
| Copies of your CV files ("My CV") | `cv` in the data folder | `<data folder>/cv` |
| Installation log | none | none |
| Python | included in the build | your Python |
| Shortcuts | the Start menu entry made by the Windows installer; on Linux `ReverseHire/install-desktop-entry.sh`; on macOS the app in Applications | `start.bat`, `start.sh`, `start.command` |

The data folder follows the rules of [section 2](#2-where-your-data-is): the table shows the normal (portable) case.

The embedding model is looked for in these folders, in this order; the first one that holds a complete copy is used, read-only, without any internet access:

1. `models` in the data folder (for example `userdata\models`);
2. the folder named by the environment variable `REVERSEHIRE_MODELS`, if set;
3. `models` in the project folder, when the app runs from the source code (`<project>/models`, ignored by Git);
4. `models` next to the program (the unpacked `ReverseHire` folder of a ready-made build, or next to the `venv` of an installation made from a [disc you built yourself](#11-building-an-offline-installation-disc-yourself-maintainer-procedure));
5. `models` in the data folder of older versions (`%APPDATA%\ReverseHire\models`, `~/.reversehire/models`).

The local translation models are looked for the same way: in `models/translation` of the data folder, or, when that folder does not exist, of the first of these folders that has one, or in the folder named by `REVERSEHIRE_TRANSLATION_MODELS`. They are not in the download of the ready-made builds nor in the repository; to get them run once `python -m reversehire.cv.translate download it uk` in the environment of a source installation, or in a ready-made build `ReverseHire.exe -m reversehire.cv.translate download it uk` (`reversehire` on Linux; see [Download a ready-made build](#download-a-ready-made-build)) (internet needed; the files are checked against pinned SHA-256 values). Without them a non-English CV is translated only with the built-in tables and the rest stays in the original language, marked "(original language)".

The map data is not looked for in the model folders: it lives in `osm` inside the data folder. It is downloaded only when you click **Download / refresh map data** on the location tab (about 205 MB for Emilia-Romagna, from `download.openstreetmap.fr`), indexed once, and reused for 30 days.

A folder may contain the copy made by the downloader (`models--intfloat--multilingual-e5-small\...`) or a plain folder `multilingual-e5-small` with `onnx\model.onnx`, `tokenizer.json` and `tokenizer_config.json`. Only when no folder has the model is it downloaded (about 470 MB) into the first one. The **?** menu, **Diagnostics…**, shows which folder is used.

**About the database file.** Every thread of the app has its own connection to `store.sqlite`, so the window never waits behind a background job: if the window itself has to wait for the file, it waits about 2 seconds at most and then shows a "database is busy" notice instead of freezing. On a normal local folder SQLite runs in WAL mode (`store.sqlite-wal` and `store.sqlite-shm` files appear next to the database while the app runs). Where WAL is unsafe - a network folder, a folder synchronised by OneDrive, Dropbox and similar, or a Windows packaged-app folder redirection - the classic rollback journal is used instead (a short-lived `store.sqlite-journal` file appears during a write), and the database stays one self-contained file. When the app and the MCP server write at the same time, a background writer waits up to 10 seconds for the other. If the folder becomes read-only (permissions, a full or disconnected drive), the error names the file and suggests moving the data folder with `REVERSEHIRE_HOME`. Copy or back up the database with the app closed.

## 9. Troubleshooting

| Problem | What to do |
|---|---|
| `start.bat`, `start.sh` or `start.command` stops during the first start. | Read the message: usually Python is missing or too old, there is no internet, or (Debian/Ubuntu) `python3-venv` is missing. Fix it, delete the `.venv` folder and start the script again. |
| Linux: the app does not open and the terminal mentions `xcb`, `EGL`, `libxkbcommon` or "Could not load the Qt platform plugin". | Install the system packages of [section 5.1](#51-system-packages). On Wayland, `QT_QPA_PLATFORM=xcb bash start.sh` is a workaround if the window misbehaves. |
| The app says that "a Windows component is missing". | Download and install the runtime from Microsoft: <https://aka.ms/vs/17/release/vc_redist.x64.exe>, then start ReverseHire again. Most PCs already have it. |
| The Results tab shows **"Reduced accuracy: the scoring model could not be loaded (…)"**. | The search goes on and gives results, but the scores are approximate. Click **Why?** next to the notice: it names the cause and what to do, and **Details** has the technical text to copy. Typical causes: *the model is not on this PC and could not be downloaded* (no internet, or a proxy: copy the `models` folder of another installation into the data folder or the program folder, or connect and start a new search); *the secure connection was refused* (an antivirus that scans HTTPS connections, or a company proxy: allow ReverseHire or huggingface.co, or copy the model as above); *blocked by Windows security* (Smart App Control or the antivirus blocked a program file); *a Windows component is missing* (the Visual C++ runtime, see above). The model is tried again at every new search, so full accuracy comes back by itself once the problem is solved. |
| A search keeps saying it is waiting for the online company sources, or the status line of a source says "not available". | Without downloaded map data the search falls back on the public Overpass service, which is often slow or busy. Click **Download / refresh map data** on the location tab once: the local data answers at once and Overpass is then not needed. **Cancel** stops a search at once. |
| A search finds nothing. | The Results tab says why (no companies on the map within the radius, or none with a website while companies without one are excluded) and what to change: a larger radius, a province or region area, other sectors or categories, an imported company list, or the websites you know added by hand in tab 2. |
| The app does not find your saved searches after an update. | **Diagnostics…** shows the data folder now used. Older versions used `%APPDATA%\ReverseHire` (Windows) or `~/.reversehire`: their data is copied automatically only when the new folder is still empty. Otherwise close the app and copy `config.json` and `store.sqlite` from the old folder into the data folder by hand. |
| The app does not find the model, or your saved searches, although you are sure they exist (Windows). | Programs started from inside a packaged app (for example a terminal or an assistant opened from an app installed from the Microsoft Store) may see a *redirected* AppData folder: what they write in `%APPDATA%` or `%LOCALAPPDATA%` actually ends up in `%LOCALAPPDATA%\Packages\<app>\LocalCache\...`, which ReverseHire started normally does not see. The portable `userdata` folder avoids this for the data; **Diagnostics** lists such copies and says when ReverseHire itself runs inside a packaged app. Copy their `models` folder into the program folder, or set `REVERSEHIRE_MODELS` to it. |
| Diagnostics warns that the data folder is synchronised by OneDrive, Dropbox, iCloud… | Move the data folder outside the synced folder and set `REVERSEHIRE_HOME` ([section 2](#2-where-your-data-is)). |
| An error says "The ReverseHire database … cannot be used: the file or its folder is read-only". | The data folder cannot be written (permissions, read-only media, a full or disconnected disk). Fix it, or move the data folder with `REVERSEHIRE_HOME`. |
| A CV that is a photo or a scan says that the text-recognition (OCR) engine is not installed. | The `rapidocr` package is missing from the program's environment (for example after a partial installation). Reinstall the ready-made build; from source, run `.venv\Scripts\python.exe -m pip install -r requirements.txt` (`.venv/bin/python` on Linux and macOS). The **?** menu, **About**, shows "OCR engine: available" when it is installed. |
| The advanced reader cannot be downloaded, or the program says its files are damaged. | Check the internet connection (the site is `huggingface.co`) and start the download again from **Tools > Advanced reader (OCR)**: it resumes where it stopped. For damaged files choose **Remove the advanced reader…** and download it again. Without it the normal reading is used. |
| The first search says it is loading the model and takes a long time. | Normal the first time (a few seconds when the model is on the PC). If no model folder has it, it is downloaded (about 470 MB): keep the connection active. Without a connection the search continues at once with reduced accuracy. |
| The window stopped answering for a while. | The log (`reversehire.log`) records where it was stuck, after 5 seconds without an answer: send that part of the log. Keep the data folder outside synchronised folders. |
| The assistant strip says it cannot answer. | The line under the strip says why: the assistant program is not installed or not found (install it, or set its path in **Settings > Assistant and MCP**), it is not signed in (run it once in a terminal and sign in), or MCP is off (click the red dot). **Check again** in the settings repeats the check. |
| "Write e-mail…" opens nothing, or classic Outlook is not used. | On Windows classic Outlook is driven through the `pywin32` package, installed with the program; check that the Outlook program (not only new Outlook) is installed and has a mail profile, or choose another way in **Open with**. With the environment variable `REVERSEHIRE_MAIL_DRY_RUN=1` the program refuses to launch any mail program (it is meant for tests). |
| You need to send information about a problem. | In the app: **?** menu, **Diagnostics…** runs the checks (data folder, scoring engine, model folders and offline load, OCR, Visual C++ runtime, Smart App Control, connection to the online services) and **Copy** puts the result on the clipboard; **?** menu, **Open log folder** opens the folder with `reversehire.log`. Send both. The log lines carry the name of your computer and the labels of what you clicked, never the text of your CV or e-mails. They contain folder paths and versions, never the text of your CV. Without starting the app: from source `.venv/bin/python -m reversehire.diagnostics` (`.venv\Scripts\python.exe` on Windows); in a ready-made build `ReverseHire.exe -m reversehire.diagnostics`. |
| You want to start again from scratch. | Ready-made build: delete the program folder (or uninstall the Windows installer version from Settings > Apps) and its `userdata`, then install again. From source: delete `.venv` and `userdata`. For an installation from a disc you built yourself see [section 11.3](#113-installing-from-the-disc). |

## 10. First run, and what is recreated or downloaded

**None of the following is in the repository unless stated.** The repository holds only the source code, the documentation, the tests, the sample data with invented content and two small bundled model files (see `docs/PUBLISHING.md`). Everything else is created or downloaded on the PC where the program runs, at installation or on first use, and is listed in `.gitignore` where it could appear inside the project folder.

### 10.1 What happens at the first start

1. The data folder is chosen ([section 2](#2-where-your-data-is)) and created: `config.json` with the default settings, an empty `store.sqlite` (schema 5), `reversehire.log`.
2. The window opens in English. Nothing is downloaded and no network request is made by the start itself, apart from the optional start-up check of the regional register, which does nothing while you have never downloaded the register.
3. You choose a CV (tab **1. CV and profile**); it is read at once on your computer. If the CV is a scan or a photo, the built-in text recognition is used; the larger "advanced reader" is only offered, never downloaded without your answer.
4. The first time the scoring needs the embedding model and no model folder has it, it is downloaded (about 470 MB). If you cannot download it, the program works with reduced accuracy and says so.

### 10.2 Created or downloaded on a new PC

| What | When | How big | Where it lives | In the repository? |
|---|---|---|---|---|
| Data folder: settings, saved searches, company cards, log, caches, copies of your CV, applications | created at the first start and as you work | grows with use (the log is capped at 100 MB) | `userdata` (or `REVERSEHIRE_HOME`) | No (`userdata/` is ignored) |
| The ready-made build itself | downloaded by you from the Releases page | about 400 to 500 MB download, about 1 GB unpacked | the folder you unpack it into (or `%LOCALAPPDATA%\Programs\ReverseHire Standalone`) | No (a release asset) |
| Python environment and packages | by `start.bat`, `start.sh`, `start.command` (from source) | about 1.4 GB | `.venv` (from source) | No (`.venv/` is ignored) |
| Scoring (embedding) model `intfloat/multilingual-e5-small` | first use, or copied from another installation | about 470 MB | `models` | No (`models/` and `*.onnx` are ignored) |
| Local translation models (Italian, Ukrainian CVs) | only on request: `python -m reversehire.cv.translate download it uk` | not stated in the program | `models/translation` | No |
| Advanced reader of the text recognition | only when you accept the offer or use the menu | about 147 MB | `models/ocr/advanced-v1` | No |
| Map data of a region (OpenStreetMap extract) and the index built from it | only when you click **Download / refresh map data**; reused for 30 days | about 205 MB for Emilia-Romagna | `osm` | No |
| Map pictures (tiles) cache | when a map shows its background online | at most 200 MB | `cache/map` | No |
| Regional register: files of the four official sources and the register database | only when you click **Download / update** in the Register manager; the program shows the size first | from several hundred MB to a few GB for the raw files (developer's estimate, not measured here); the register itself keeps only your chosen regions; **Delete downloaded files** frees the raw files | `registers` | No |
| Job-search caches | during use | small, kept from hours to about a week | `cache/jobsearch` | No |
| Optional Playwright browser | only if you install it | several hundred MB | the Playwright folder | No |

Two small model files ARE in the repository on purpose, because the program needs them without any download: the face detector `face_detection_yunet_2023mar.onnx` and the Cyrillic text recogniser `ocr_cyrillic_rec.onnx` (with its key file), both in `src/reversehire/cv/data/` with their licence files (see `ATTRIBUTIONS.md`).

### 10.3 Starting on another PC with your data

Close the program and copy the whole data folder (and, if you wish to avoid new downloads, the `models` folder). Caches, the map index and search fingerprints are rebuilt when needed. The register can be copied with the data folder or downloaded again.

## 11. Building an offline installation disc yourself (maintainer procedure)

No ready-made ISO is distributed: this is an optional procedure for maintainers who need to install ReverseHire on PCs without internet; it assembles the disc from the sources. Everyone else should use a [ready-made build](#download-a-ready-made-build) or the installation from the source code. The procedure is for Windows 64-bit and needs the development environment of [section 3](#3-windows-from-the-source-code).

### 11.1 What the disc you build contains

The disc contains every tool the program needs, so nothing has to be downloaded on the target PC.

| Folder or file | Content | Size |
|---|---|---|
| `install.bat` | Starts the installation. This is the only file you need to run. | — |
| `README.txt` | Short instructions. | — |
| `docs\` | This manual and the User Manual as PDF, the licence and the list of third-party components. | 2 MB |
| `python\` | Official installer of Python 3.14.x (64-bit), digitally signed by the Python Software Foundation. Used only if Python 3.14 is not already on the PC. | 32 MB |
| `wheels\` | ReverseHire (the version printed by the build, see [section 11.2](#112-building-the-disc)) and all the Python packages it uses (about 94 files). | 575 MB |
| `models\` | The embedding model used for scoring, `intfloat/multilingual-e5-small`. | 465 MB |
| `setup\` | Installation script, uninstaller and program icon. | — |
| `redist\` (optional) | Microsoft's Visual C++ runtime, `vc_redist.x64.exe`, installed only on PCs where it is missing. If your disc has no such folder and the installer reports the runtime as missing, download it from Microsoft (<https://aka.ms/vs/17/release/vc_redist.x64.exe>, about 25 MB) and run it, or copy the disc content to a folder and put the file in a `redist` folder next to `install.bat` before installing. | 25 MB |

Main components installed from `wheels\`:

| Component | Version | Used for |
|---|---|---|
| PySide6 (Qt) | 6.12.0 | The window interface |
| fastembed, onnxruntime | 0.9.1, 1.31.1 | Local embeddings for scoring |
| httpx, trafilatura, lxml | 0.28.1, 2.3.1, 6.1.3 | Reading company websites |
| pypdf, python-docx | 7.0.1, 1.2.0 | Reading the CV (PDF and DOCX) |
| rapidocr (with opencv-python-headless, shapely, pyclipper, omegaconf) | 3.10.0 | Local text recognition (OCR) of CVs sent as images or scanned PDFs; its models are inside the package, and ReverseHire itself carries the 8 MB Cyrillic recogniser, so it works offline for Latin and Cyrillic text |
| lingua-language-detector | 2.2.0 | Detecting the language of the CV |
| rapidfuzz, numpy | 3.14.6, 2.5.4 | Text matching and calculations |
| pywin32 | 312 | Windows only: opens a draft in classic Outlook with the PDF attached (application e-mail) |
| mcp | 2.3.0 | Optional MCP server for Claude, Codex and Gemini, and the connection of the assistant strip (switched off in the app until you turn it on) |

The licences of these components are listed in `docs\ATTRIBUTIONS.md`.

### 11.2 Building the disc

From the project folder, with the development environment in `.venv`:

1. Build the program package: `.venv\Scripts\python.exe tools\build.py --out dist\iso_staging\wheels`. The script **raises the third number of the version** (the build number, for example 0.1.1 to 0.1.2) in `src\reversehire\_version.py`, writes the build date next to it, builds the wheel into that folder and prints the new version as its last line; the wheel and the disc image are named after it. If the wheel cannot be built the old version is written back, so a failed build does not use up a number. Options: `--dry-run` shows the next version without changing anything, `--no-wheel` only raises the number. The version file is a change in your working copy: commit it if you keep the numbering.
2. Collect the dependencies as wheel files for Windows 64-bit and Python 3.14 into the same folder: `.venv\Scripts\python.exe -m pip download "dist\iso_staging\wheels\reversehire-<version>-py3-none-any.whl[mcp,jobboards]" -d dist\iso_staging\wheels --only-binary=:all:`
3. Copy `tools\installer\install.bat` and `README.txt` to `dist\iso_staging\`, and `install.ps1`, `uninstall.bat` and `src\reversehire\resources\icon.ico` to `dist\iso_staging\setup\` (all of them are in `tools\installer\`, except the icon).
4. Put the official Python installer (`python-3.14.x-amd64.exe` from python.org) in `dist\iso_staging\python\`.
5. Copy the folder `models--intfloat--multilingual-e5-small` from the data folder's `models` (for example `userdata\models`, after one real search) or from `models\` to `dist\iso_staging\models\`. **Optional:** to have the advanced reader of the text recognition on the disc (about 147 MB more), also copy the folder `ocr\advanced-v1` from the same `models` folder (it exists after you download the reader once) to `dist\iso_staging\models\ocr\advanced-v1\`; the installer copies the whole `models` folder, and the program finds the reader next to the installed program. The GPU package (`onnxruntime-directml`) is **not** installed by the installer and stays optional ([section 7](#7-optional-components)).
6. Create the PDF manuals with `python tools\build_manuals.py dist\iso_staging\docs` (needs the `markdown-it-py` package and Microsoft Edge), and copy `LICENSE` (as `LICENSE.txt`) and `ATTRIBUTIONS.md` to the same folder.
7. Optionally put Microsoft's `vc_redist.x64.exe` (from <https://aka.ms/vs/17/release/vc_redist.x64.exe>) in `dist\iso_staging\redist\`: the installer runs it only on PCs where the runtime is missing.
8. Create the image: `powershell -ExecutionPolicy Bypass -File tools\installer\make_iso.ps1 -Source dist\iso_staging -Output dist\ReverseHire-<version>-Setup.iso` (`<version>` is the version printed in step 1).

The `dist\` folder is not part of the repository, and the image is never published: it stays with whoever built it. The wheel list includes `pywin32` (a Windows-only dependency of the program).

### 11.3 Installing from the disc

**Open the image.** Copy the `.iso` file to the target PC (for example with a USB stick) and double-click it: Windows opens it as a new drive, like a DVD (if Windows asks "Do you want to open this file?", choose **Open**). You can also burn it to a DVD (right-click, **Burn disc image**) or copy its content to a USB stick: the installation works from any folder.

**Run the installation.** In the drive that opened, double-click **`install.bat`**. If Windows shows "Windows protected your PC", choose **More info** and then **Run anyway**. A black window shows the progress; do not close it. The installation does the following, in this order:

| Step | What happens | Where |
|---|---|---|
| 1 | Looks for Python 3.14. If it is missing, installs it for the current user only; a small progress window appears for a minute or two. | `%LOCALAPPDATA%\Programs\Python\Python314` |
| 2 | Creates a private Python environment for ReverseHire, separate from anything else on the PC. | `%LOCALAPPDATA%\Programs\ReverseHire\venv` |
| 3 | Installs ReverseHire and its packages from the `wheels\` folder, without using the internet. | same folder |
| 4 | Copies the embedding model, so that the first search does not have to download it. | `%LOCALAPPDATA%\Programs\ReverseHire\models` |
| 5 | Copies the manuals, the icon and the uninstaller, and creates the **ReverseHire** shortcuts on the Desktop and in the Start menu. Files copied from the disc lose their read-only flag. | `%LOCALAPPDATA%\Programs\ReverseHire` |
| 6 | If the Microsoft Visual C++ runtime is missing, installs it from `redist\` (Windows asks for permission), or tells you where to download it. | Windows |
| 7 | **Self-test** with the installed program: loads the model from the copied folder without using the internet, computes one embedding and loads the text-recognition (OCR) engine. It prints `[PASS]` or `[FAIL]` for each check and a final `RESULT`. | — |

When you read **"Installation completed"**, press any key to close the window (about 3 to 10 minutes, depending on the PC). **"Installation completed with a warning"** means that only the scoring model failed its self-test (the cause is printed just above, for example a missing Visual C++ runtime or Smart App Control): ReverseHire works anyway, with *reduced accuracy*, until the problem is solved; see [Troubleshooting](#9-troubleshooting). Everything the window shows is also saved in `%LOCALAPPDATA%\Programs\ReverseHire\install.log`. You can then eject the disc drive.

The program folder is in your user profile and writable, so the app keeps your data in **`%LOCALAPPDATA%\Programs\ReverseHire\userdata`** (portable rule, [section 2](#2-where-your-data-is)). `%LOCALAPPDATA%` is normally `C:\Users\<your name>\AppData\Local`; you can type it as it is in the address bar of File Explorer. Double-click the **ReverseHire** icon on the Desktop: the window opens in a few seconds, in English, with an empty data folder and the scoring model already in place. See [section 10](#10-first-run-and-what-is-recreated-or-downloaded) for what is created or downloaded later. Windows Firewall does not need any permission: the app only makes outgoing connections and opens no network port.

**Updating or reinstalling.** Run `install.bat` again from a newer disc (or from the same one, to repair the installation). Only the Python environment is replaced; your data in `userdata` and the model in `models` are kept.

**Removing ReverseHire.**

1. Close ReverseHire.
2. Open `%LOCALAPPDATA%\Programs\ReverseHire` and double-click **`uninstall.bat`**.
3. Answer **Y** to remove the program and its shortcuts.
4. Answer the question about your data: **Y** deletes `userdata` with everything else; **N** removes the program but keeps the `userdata` folder (a new installation in the same folder uses it again).
5. If an older version left a `%APPDATA%\ReverseHire` folder, a third question offers to delete it too.

Python is left installed because other programs may use it. To remove it, open Windows **Settings > Apps > Installed apps** and uninstall "Python 3.14".

**Problems with the disc installation.**

- Double-clicking the image does nothing, or another program opens it: right-click it and choose **Open with > Windows Explorer**.
- "Windows protected your PC" when starting `install.bat`: choose **More info**, then **Run anyway**.
- "The Python installer ended with code ...": run `python\python-3.14.x-amd64.exe` from the disc by hand, choose **Install Now**, then run `install.bat` again.
- "The installation of the packages failed": check that there are at least 3 GB free on drive C: and that the antivirus is not blocking the installation, then run `install.bat` again. If you copied the disc content to a folder, make sure the copy is complete (94 files in `wheels\`).
- The installation works but the icon does not start the app: open `%LOCALAPPDATA%\Programs\ReverseHire\userdata\reversehire.log` and read the last lines. Running `install.bat` again repairs a damaged installation.
- At the end the installer says the "Microsoft Visual C++ runtime was not found": see the Visual C++ row of [Troubleshooting](#9-troubleshooting).
- To start again from scratch, run `uninstall.bat` answering **Y** to the questions, then install again.

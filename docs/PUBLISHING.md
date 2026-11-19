# Publishing the project on GitHub

**Nothing described here has been done.** No commit was made (the local Git repository has no commits yet), no remote is set, and nothing was uploaded. The folder is ready, but the decision and the commands are the owner's. This file is the checklist to follow, and the findings of the last check of the working tree (19 November 2026) are in [section 3](#3-findings-of-the-check-of-19-november-2026).

Language rule: all files uploaded to GitHub are in English. The interface strings in `src/reversehire/locales/` include Ukrainian and Italian translations, which are part of the product. The developer is written `0THack1A` (first character the digit zero); the GitHub address is held in one place in the code, `REPOSITORY_URL` in `src/reversehire/__init__.py` (the GitHub account is currently spelled with the letter O).

## 1. A few words about Git

- **Commit**: a "snapshot" of the project files, saved in the history on your computer with a message. It stays local.
- **Push**: sends the commits to GitHub. If the repository is public, anyone can then see the files.
- **`git add -A`**: chooses which files go into the next snapshot. Files listed in `.gitignore` are skipped. `git add -A -n` shows what would be added without adding anything.

## 2. What is in the repository, and what is deliberately not

### 2.1 In the repository

| Path | What |
|---|---|
| `src/reversehire/` | The program, its locales (`en`, `it`, `uk`), the dictionaries and tables (`cv/data`, `discovery/data`, `affinity/data`, `recruiter_sim/data`), the icons and the JetBrains Mono font (SIL OFL 1.1), the map web files and the bundled Leaflet libraries (`mapview/web`) |
| `docs/` | This documentation, the pictures (`docs/images/`), the LaTeX sources and the PDF edition of the manual (`docs/manual/`) |
| `tools/` | Build and maintenance scripts (packaging of the ready-made builds, the optional offline-disc scripts, manuals, locale keys, screenshots, logo resources) |
| `README.md`, `ATTRIBUTIONS.md`, `LICENSE` (MIT), `pyproject.toml`, `requirements.txt`, `start.bat`, `start.sh`, `start.command`, `.gitignore`, `.gitattributes`, `.mcp.json.example`, `.github/workflows/tests.yml` | Project files |

**The two small model files that are in the repository on purpose**, because the program needs them without any download (both are listed in `pyproject.toml` as package data and in `ATTRIBUTIONS.md`):

| File (`src/reversehire/cv/data/`) | Size | Licence | Used for |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` (YuNet face detector, from the OpenCV Zoo), with `face_detection_yunet_LICENSE.txt` | 232,589 bytes | MIT | Finding the portrait in a CV so that it can be taken out (detection only, no recognition) |
| `ocr_cyrillic_rec.onnx` (PP-OCRv5 Cyrillic recogniser, PaddlePaddle / PaddleOCR) and its key list `ocr_cyrillic_keys.txt`, with `ocr_models_LICENSE.txt` | 8,048,799 bytes | Apache-2.0 | Reading Ukrainian text in scans and photos |

`.gitignore` keeps every other `*.onnx` out, with the single exception `!src/reversehire/cv/data/*.onnx`.

### 2.2 Deliberately not in the repository

Each of these is either recreated or downloaded at installation or on first use, or is the owner's private material. All are in `.gitignore` except where noted.

| Ignored | What it is | Why it is not published | How it comes back |
|---|---|---|---|
| `.venv/` | The Python virtual environment (about 1.6 GB here) | Machine-specific, large | `start.bat`, `start.sh` or `start.command` creates it at the first start; or `pip install -r requirements.txt` and `pip install -e .` |
| `userdata/` | The portable data folder: settings (`config.json`, with any key you typed, in plain text), saved searches, company cards, applications, the log, copies of your CV, downloaded map data, the register, caches | Personal data and keys | Created empty at the first start |
| `models/` and every `*.onnx` except the two bundled files; `.cache/`, `fastembed_cache/` | The scoring model, the translation models, the advanced OCR reader | Large (the scoring model is about 470 MB), downloaded from their own hubs under their own licences | Downloaded on first use or on request, see [INSTALLATION.md](INSTALLATION.md#10-first-run-and-what-is-recreated-or-downloaded) |
| `private/` | The owner's real CVs, used for local end-to-end checks | Personal data | Not recreated: it is the owner's |
| `fonti/` | Research notes and third-party repositories downloaded for reference | Third-party code and private notes | Not needed at run time |
| `assetts/` | The original logo supplied by the owner, which contains third-party company names (see the note at the end) | Trademarks of other companies | Not needed: the cropped versions are in `docs/images/banner.png` and `src/reversehire/resources/` |
| `dist/` and `build/` | The built wheels and the staging folder; the installation ISO is a by-product only of the optional maintainer procedure and is never published | Build by-products (about 2.9 GB here) | `python tools/build.py`, see [INSTALLATION.md](INSTALLATION.md#112-building-the-disc) |
| `__pycache__/`, `*.pyc`, `*.egg-info/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/` | Python and tool caches | Generated | Generated by Python and the tools |
| `data/cv_reali/`, `data/esco/` | Real CVs and a local copy of ESCO data, if present | Personal data / third-party data | Not needed (the program reads ESCO online when a CV is read) |
| Register and map downloads: `userdata/registers/`, `userdata/osm/`, `userdata/cache/` | The four official register sources and the register database; the OpenStreetMap extract and its index; map pictures and job-search caches | Large, third-party data under its own licence, downloaded by the user | When the user clicks **Download / update** (register) or **Download / refresh map data**; the program shows the size first |
| `*.sqlite`, `*.sqlite3`, `*.sqlite-wal`, `*.sqlite-shm`, `*.sqlite-journal`, `*.db` | Databases | Personal data | Created by the program |
| `*.log` | `reversehire.log` and its rotations | Carries the computer name and the labels of what was clicked | Created by the program |
| `.env`, `.env.*` (except `.env.example`), `.mcp.json` | Local configuration and secrets | Secrets and local paths | `.mcp.json.example` is published instead |
| `.vscode/`, `.idea/`, `Thumbs.db`, `desktop.ini`, `.DS_Store` | Editor and operating-system files | Noise | - |

If you set `REVERSEHIRE_HOME` to a folder inside the project, add that folder to `.gitignore`.

> **Decision of 19 November 2026 (owner):** the repository carries sources only. The automatic tests (`tests/`), every sample or fixture file (`data/`, `tests/data/`), and the development scripts that contain invented data (`tools/make_manual_screenshots.py`, `tools/ui_check_wiring.py`) are ignored by Git and stay on the developer's PC. Program resources inside `src/reversehire/` (tables, dictionaries, translations, the two small models) are part of the program and are published.

## 3. Findings of the check of 19 November 2026

The check was made on the working tree with `git ls-files -o --exclude-standard` (what `git add -A` would add): 700 files, about 25 MB (the largest: the Cyrillic model 8.0 MB, the PDF manual 1.1 MB, `banner.png` 0.4 MB, `ateco_2025.csv` 0.4 MB, `uk.json` 0.3 MB). No file over 8.1 MB. The files were read for the owner's user name, e-mail addresses and absolute paths.

### 3.1 Would be published but should not (or needs a decision)

Items marked resolved were fixed on the same day; the others are decisions left to the owner.

1. **Three empty files in the project root** (created by accident): resolved, they were moved out of the repository.
2. **`tools/ui_check_wiring.py`**: a development check of the new interface wiring (it opens a real window against a temporary data folder and saves PNGs for a human to look at). It is harmless and has no personal data, but it is not a user tool and nothing refers to it. Keep it as a developer tool or delete it.
3. **A local absolute path in `tests/test_chat_commands.py`**: resolved, replaced with a neutral placeholder path.
4. **Real company names in the plan and in three test docstrings**: resolved, replaced with neutral wording and invented names.
5. **Test fixtures derived from real third-party data** in `tests/data/`: `financials/` (annual-report data of three listed companies, public filings), `jobsearch/` (pages and sitemaps of three staffing agencies and of other sites, apparently real: they carry real posting text), `registers/` (invented), `geo/` and `discovery/` (labelled invented). Public filings are fine; the agency pages are third-party text, so replace them with short invented pages if you want the repository to carry no third-party content. There is no personal data in them (no e-mail address or phone number was found).
6. **`docs/manual/reversehire_manual.pdf`** (1.1 MB) is a generated file (the PDF edition of the LaTeX manual). The README links it, so it is kept; regenerate it whenever the manual changes, or delete the link and the file.
7. **Contact data found in the text of the published files** (all acceptable, listed for completeness): the author's address in the YuNet licence file (`face_detection_yunet_LICENSE.txt`, a third-party licence text that must be kept as it is), invented addresses on `example.*`, `.invalid` and `legalmail.it` placeholder domains in tests, and the address of the Italian data-protection authority in one test (`pec.gpdp.it`, a public institution).
8. **Windows paths with a user name**: none. The placeholders `C:\Users\alex`, `C:\Users\x` and `/home/alex` appear in `tools/make_manual_screenshots.py`, `docs/codex_config.example.toml` and tests; they are neutral.

### 3.2 Needed at run time but ignored (cannot be recreated)

**None found.** The check was: every non-Python file under `src/` (locales, dictionaries, tables, icons, fonts, map web files, the two bundled models) is not ignored and appears in the `package-data` list of `pyproject.toml`; no module of `src/` reads anything from `data/`, `docs/`, `fonti/`, `private/` or `assetts/`; the only ignored things the program uses (`models/`, `userdata/`, `.venv/`) are created or downloaded by the program itself. **If a future change makes the program read a file from an ignored path, that is a blocking defect: it would work on this PC and fail on every other.** Re-run the check in step 7 below before every release.

## 4. Checklist before publishing

1. **Licence.** `LICENSE` is the MIT licence with `Copyright (c) 2026 0THack1A` (first character the digit zero); `pyproject.toml` says the same. It is a proposal: MIT lets anyone use, copy and modify the code, including commercially, as long as the notice is kept. If you want other rules, change `LICENSE` and the `license` field.
2. **Language.** README, `docs/`, the licence and the attributions are in English; the Python package, its identifiers and comments are in English.
3. **Personal data.** Repeat the search for personal traces over what would be published:
   ```
   git add -A -n
   git ls-files -o --exclude-standard
   ```
   then search those files for your system user name, your own name, e-mail addresses, `C:\Users`, `/home/` and any personal website. Remember that the pictures are files too:
   - **The pictures of the user manual** (`docs/images/manual/`, 42 of them) are made by `tools/make_manual_screenshots.py` with invented data, a throwaway data folder (a short folder at the root of the drive, named `tmp...`, deleted at the end) and neutral PC details (user "alex", computer "workstation", one screen, one disk, a generic graphics card and processor). The script opens no mail program, starts no assistant program (a small script prints a prepared answer), runs the job search on a fake runner, draws the maps with the painter fallback (`REVERSEHIRE_MAP_FALLBACK=1`), uses a tiny invented register, invented pasted figures and an invented application, reads the CV with the ESCO lookup switched off, and refuses any network connection made by Python (it stops with an error if one is attempted). After running it again, **look at every picture** (no real name, e-mail address, path with a user name, computer name or company; English only; no clipped text) and check that every PNG is linked from `docs/USER_MANUAL.md` and every link resolves.
   - **The two README pictures** are `docs/images/readme/interest-flow.png` (a copy of `35_interest_flow.png`) and `docs/images/readme/company-card-affinity.png` (the card side of `36_affinity_card.png`, cropped); recreate them from the new pictures when those change.
   - **The version file.** `src/reversehire/_version.py` holds the version and the build date, and `python tools/build.py` rewrites it at every build. Check that the number you publish is the one you mean.
   - **Keys and answers in the data folder.** `userdata/config.json` holds, in plain text, any Firecrawl, Openapi, Adzuna, Jooble or Careerjet key and your screening answers. It is ignored; never copy it into an issue.
   - **The log** (`reversehire.log` in the data folder) carries the name of the computer on every line and the labels of everything clicked; do not paste it into a public issue without reading it.
4. **Repository name and address.** The proposed name is `ReverseHire`. The address is `REPOSITORY_URL` in `src/reversehire/__init__.py` (change it there only); the non-code files that contain it (`pyproject.toml`, `ATTRIBUTIONS.md`, `README.md`, `docs/*.md`) must be updated by hand if it changes.
5. **Third-party trademarks.** See the note at the end.
6. **Files that must not go online.** Section 2.2 is the list. Check with `git add -A -n` that nothing from it appears.
7. **Nothing needed is ignored.** Run the check of section 3.2 again: list the non-Python files of `src/`, confirm each is covered by `package-data`, and confirm with a fresh clone (step 9) that the program starts.
8. **GitHub description and topics.** Proposed description: "Desktop app that starts from a CV and finds the companies that fit the profile, and may be worth writing to." Topics: `cv`, `job-search`, `openstreetmap`, `esco`, `pyside6`, `mcp`.
9. **Try a fresh clone.** In an empty folder: clone the repository, run `start.bat` (or `start.sh`), and check that the window opens, a CV of your own is read, and nothing is missing.
10. **Declared status.** The README says honestly that the project works but is not tuned on many real cases, and which things were not tried on a real PC. Keep it that way: change those sentences only after trying the thing. The same goes for three more limits stated there: the assistant may not know small local companies, the pause of the idle analysis was verified only with simulated idle signals, and two short stalls of the window (about 5 seconds, during heavy background work) are not fixed yet.
11. **Third-party licences.** `ATTRIBUTIONS.md` lists data, services, models and libraries with their licences (Qt/PySide6 under LGPL-3.0, `pywin32` under the PSF licence, JetBrains Mono under SIL OFL 1.1, the register sources under CC BY 4.0 and CC0). Wherever it says "to be verified", check the official source before publishing.
12. **Internal planning documents.** `docs/PLAN.md` and `docs/INTERFACES.md` are published and are kept up to date; early internal plans are not part of the repository.

## 5. Commands to run when you decide

### 5.1 Create the empty repository on github.com

On the site: "New repository" button, name `ReverseHire`, visibility Public. Do **not** tick "Add a README", ".gitignore" or "license": the project already has them, and an empty repository avoids conflicts.

### 5.2 From the terminal, in the project folder

First check that the Git identity is yours (`git config user.name` and `git config user.email`; to avoid showing your personal email you can use the "noreply" address that GitHub shows in your account settings).

```
git add -A
git status
```

`git status` lists what is about to be saved: read it carefully. If something appears that you do not want to publish, remove it with `git rm --cached <file>` (and add it to `.gitignore`) before continuing.

```
git commit -m "Initial commit"
git branch -M main
git remote add origin <REPOSITORY_URL>.git
git push -u origin main
```

(`<REPOSITORY_URL>` is the address in `src/reversehire/__init__.py`.) If `git commit` complains about the name or email, set them with `git config user.name "..."` and `git config user.email "..."`. After the push, check the repository in the browser: that there are no unexpected files and that the two pictures at the top of the README show.

## Note on third-party trademarks

The original logo supplied by the owner (`assetts/Logo.jpg`) contains, on its sides, company names and the word "Claude". The images used in the project (`docs/images/banner.png` and the files in `src/reversehire/resources/`) are crops that exclude them. **The original must not go into the repository**: that is why `assetts/` is in `.gitignore`. If you later want to publish the whole logo, first consider whether you may use those names (they are other companies' trademarks and could suggest an affiliation or endorsement that does not exist); when in doubt, keep the crops.

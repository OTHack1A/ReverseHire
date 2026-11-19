# ReverseHire – plan and progress

Living document: every step is ticked when it is finished **and verified**. Decisions are
recorded here in short form; the reasons and the technical detail are in
[`DECISIONS.md`](DECISIONS.md). Update both files at every change.

Legend: `[x]` done and verified · `[~]` in progress · `[ ]` to do.

## Phase 1 – Discovery, company card, portability (17 November 2026)

- [x] Diagnose the failing company search (public Overpass servers overloaded).
- [x] Multi-source discovery: local OpenStreetMap extract, Overpass as fallback, Wikidata,
      imported register lists, ATECO 2025 categories, radius / province / region.
- [x] Polite network gateway (at least 2 s per host, stop on HTTP 429, cached robots and 404).
- [x] Portable data folder, no administrator rights, launchers for Windows, Linux, macOS.
- [x] Company card (identity status, provenance per field, VIES and GLEIF).
- [x] "My CV" tab, language chips, English display layer, photo from the CV.
- [x] Functional checklist (25 items) and real end-to-end run with real CVs.
- [x] Markdown documentation, LaTeX manual, installation ISO.

## Phase 2 – Robustness, complete card, applying (18 November 2026)

- [x] Window freeze: per-thread database connections, GUI waits at most about 2 s,
      exception safety net, watchdog that logs the GUI stack after 5 s.
      *Not reproduced; fixed from the evidence in the log.*
- [x] Log format `[date][time][computer]: …`, every user action, rolling, 100 MB cap.
- [x] Profile confirmed automatically; the message names what is missing.
- [x] Workplace column, "Open LinkedIn", theme toggle, menus mirroring every button.
- [x] Company card filled from the company website (legal pages, footer, structured data),
      cross-checked with VIES, GLEIF, Wikidata; presentation, history, contacts (HR first).
- [x] Registro Imprese by hand (open the page, paste form); optional paid provider with the
      user's own key. *Paid provider never tested against the live service.*
- [x] Financials section and chart (free figures only for listed companies).
- [x] E-mail drafts: classic Outlook and new Outlook verified with the PDF attached.
      *Gmail in the browser not verified.*
- [x] Assistant strip (Claude Code or Codex), restricted to the app's own tools; both verified.
- [x] Export with preview: print, PDF, Excel, Word. *Real printer not verified.*
- [x] Settings in six pages, hover and focus effect, developer name 0THack1A.
- [x] Documentation, 22 screenshots, LaTeX manual (18 pages), installation ISO.

## Phase 3 – OCR, recruiter simulators, job search (started 18 November 2026)

Research first, then implementation, then end-to-end verification.

- [x] Research: strongest OCR that really runs on this PC (GPU available), with measurements.
- [x] Research: what recruiters' tools do with a CV (Allibo and the other ATS and parsers),
      legitimate sources of job postings, integrated map.
- [x] English at every start, with a "Start-up language" setting.
- [x] Progress bars: CV loading (per step and per OCR page); companies as
      "analysed / estimated" with time left; batch card building.
- [x] About: disks, session user, system details; data folder and model rows removed.
- [x] Version: the third number increases at every build (`tools/build.py`).
- [x] Recruiter view: bar "probability that the text was generated with AI", as an estimate.
- [x] Advanced OCR used automatically when the normal reading is not good enough.
- [x] New tab "Recruiter analysis": simulators of the screening tools, per system.
- [x] Match score between a job posting and the CV in use (skills in common).
- [x] New menu "Job search": search like on a job engine; one click builds the company card.
- [x] Paste-and-review window for sites that forbid automated reading.
- [x] Vague postings: ranked list of possible employers in the area, shown as an estimate.
- [x] Integrated map for companies, postings and the search radius.
- [x] End-to-end verification, documentation, manual, ISO.

## Phase 4 - Fixes after the owner's test, affinity verdict, registers (started 19 November 2026)

- [x] "Start search" button not reacting: the click never reached the button (nothing in the log).
- [x] Filters on every column of every table (replaced by sorting on the header and one search box, see R1 in [`DECISIONS.md`](DECISIONS.md)).
- [x] Simulated score and match always shown as 0-100 %.
- [x] No "Confirm profile" step: the profile is valid once the CV is read; every edit is saved at once.
- [x] Company card: social links next to the name.
- [x] Financials: chart frame always shown, "paste figures" review window, employees per year,
      revenue trend badge and column (a growing revenue marks a more promising company).
- [x] Research: free provincial and regional company registers; sources of past revenue and employees.
- [x] Affinity engine: CV against what the company seeks, 0-100 % with a verdict
      (apply / spontaneous application / watch / not a fit / not enough data), evidence and confidence.
- [x] Affinity in the interface: column, card box, feedback to tune the weights.
- [x] Company lists downloaded from the registers found, editable by the user.
- [x] Verification on real data (done by the end-to-end checks, not by the documentation round), ISO.

## Phase 5 - Applications tracker, search by meaning, publication (started 19 November 2026)

- [x] Choosing the CV file loads it at once; the "Load my CV" button is removed.
- [x] Map: clear message and "Show the online map" button when backgrounds are switched off or fail.
- [x] Tables: sort by clicking the header (alphabetical, numeric, dates); no filter row.
- [x] Search by meaning in every list; Country, Region, Province selectors.
- [x] Assistant suggestions (first 10, then Expand), each verified before it is listed.
- [x] Deep analysis while the computer is idle, after a fast pre-screen of the whole list.
- [x] Regional register built from free official lists, editable by the user.
- [x] Applications tracker: spontaneous or not, cover letter, archived PDF of the CV sent with preview, appointments.
- [x] Job search by meaning (synonyms, typing mistakes) with a second list: companies for a spontaneous application.
- [x] Previous searches: delete from a right-click menu.
- [x] Compare systems: column "How this system shows you" explained.
- [x] Wire affinity, suggestions, idle analysis, register and tracker into the interface.
- [x] Real verification (targets: two well-known local companies, an ICT integrator and a cybersecurity firm, must appear on their own).
- [x] Ready for GitHub: no sensitive data, nothing that is recreated or downloaded at installation (the checklist and the findings of the last check are in [`PUBLISHING.md`](PUBLISHING.md); not done until the owner decides).
- [x] English manual updated; README with two screenshots (see [`USER_MANUAL.md`](USER_MANUAL.md) and the [README](../README.md)).

## Decisions of this phase (owner's answers, 18 November 2026)

| # | Decision |
|---|---|
| D1 | The interface starts in English at every launch; the language chips apply to the running session; a setting can fix another start-up language. |
| D2 | Sites that forbid automated reading are never read by the program. The user copies the content by hand, the program fills a review window, the user corrects it, then it is saved. |
| D3 | The advanced OCR may be a large download and slow; a progress bar is required. A GPU is available on the owner's PC; a CPU fallback must exist. |
| D4 | The posting-versus-CV score grows with the skills in common with the CV used for the search. |
| D5 | The "Recruiter analysis" tab contains simulators of the recruiters' screening systems, so the candidate knows what to expect. They are approximations built from public documentation and are labelled as such. |
| D6 | The AI-generated-text bar is an estimate from writing signals, never presented as proof. |
| D7 | The program must never hang: every slow operation runs in the background and errors go to the log. |
| D8 | An integrated map may be added if it helps to locate places. |
| D9 | Nothing is committed or uploaded to GitHub until the owner says so. |

How the code implements D1-D9 (and the OCR choices): [`DECISIONS.md`](DECISIONS.md), table "Decisions of 2026-11-18, phase 3" (Q1-Q20). How to use the features: [`USER_MANUAL.md`](USER_MANUAL.md), sections 4 to 9 (the workflow) and 11-12; the decisions of 2026-11-19 (affinity, suggestions, register, tracker) are the table "Decisions of 2026-11-19" in [`DECISIONS.md`](DECISIONS.md) (R1-R16); module contracts: [`INTERFACES.md`](INTERFACES.md).

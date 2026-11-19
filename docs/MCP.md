# ReverseHire MCP server (package `reversehire`)

The MCP server lets Claude, Codex and Gemini use the same functions as the app (profile from the CV, company discovery in a radius, analysis of a company website, search history, company cards) to answer detailed requests. It is a single local server using the **stdio** transport: it opens no network port and is started by the client as a child process. The app's own **assistant strip** (see [The assistant strip](#the-assistant-strip-in-app-chat)) uses the same server, with Claude Code or Codex installed on your computer.

## The switch: MCP on / MCP off

MCP access is **off by default**. The main window shows its state at the top right of the menu bar: a **red dot with "MCP off"** or a **green dot with "MCP on"** (the text and the tooltip say the same thing, so the colour is never the only signal). Click it, or choose **Tools > MCP server…**, or **Connection details…** on the **Assistant and MCP** page of the settings, to open the **MCP connection** dialog (the switch is also on that settings page):

- the switch "Allow AI clients (Claude, Codex, Gemini) to use ReverseHire through MCP" is saved at once in `config.json` (`"mcp_enabled": true` or `false`); no restart of the app or of the client is needed, because the server re-reads the setting before every tool call;
- the time of the last tool call served and the total number of calls (kept in `mcp_usage.json` in the data folder: only a time and a counter, never the tool name, the arguments or any data);
- a short explanation of what MCP means for privacy (see [Privacy](#privacy));
- three tabs, **Claude**, **Codex** and **Gemini**, each with the configuration ready to paste, built from the Python that runs the app (the `reversehire-mcp` script next to it when installed, otherwise `python -m reversehire.mcp_server`), the name and location of the file it goes into, and a **Copy** button. The app never writes other programs' configuration files: pasting is your action.

When the switch is off the server still starts and lists its tools, so the client shows a clear message instead of a broken connection: every tool answers `{"error": "MCP access to ReverseHire is switched off. Turn it on in the ReverseHire app ... or set \"mcp_enabled\": true in <data folder>\config.json ..."}`. You can also change `mcp_enabled` by hand in `config.json` while the app is closed. The **?** menu, **About**, shows the same state.

## Installation

From the project folder, with the virtual environment already created (see `README.md`):

```
.venv\Scripts\python.exe -m pip install mcp
.venv\Scripts\python.exe -m pip install -e .
```

The `mcp` package is optional for the app but required for the server and for the assistant strip. The code works with the official Python SDK both in version 2.x (class `MCPServer`, the one tested here: 2.3.0) and in 1.x (class `FastMCP`). Without `mcp` the module still imports, and startup prints on stderr how to install it.

Manual test (the server waits for messages on stdin; quit with Ctrl+C):

```
.venv\Scripts\python.exe -m reversehire.mcp_server
```

The server reads the same settings and uses the same SQLite store as the app (`store.sqlite` in the data folder). The data folder is chosen by the same rules as in the app: the folder in `REVERSEHIRE_HOME` if set, else `userdata` inside the program folder if that is writable, else the per-user folder of the operating system (see [INSTALLATION.md](INSTALLATION.md), section 2). Searches and company cards made with the interface are therefore readable from the MCP clients, the map data downloaded by the app is used by `find_companies`, and the geocoding and page caches are shared. A client started from inside a packaged app (for example the Microsoft Store version of an assistant) may see a redirected AppData folder, which is why the default data folder is not under AppData.

## Configuration

The quickest way is the **MCP connection** dialog in the app (click the dot at the top right): it shows each snippet with the real path already filled in. The example files below use placeholders.

In the examples `<PROJECT_PATH>` is a placeholder: replace it with the folder that holds the project, so that it points to the virtual environment's Python.

- Windows: `C:\Users\<you>\projects\ReverseHire\.venv\Scripts\python.exe` (in JSON the backslashes must be doubled: `C:\\Users\\<you>\\...`).
- Linux and macOS: `/home/<you>/projects/ReverseHire/.venv/bin/python`.

If you move the folder, update the path. Do not edit the real configuration files from a script: copy the sections by hand.

### Claude Code

Copy `.mcp.json.example` as `.mcp.json` in the folder where you work with Claude Code (or register the server with `claude mcp add`, see the Claude Code documentation). Contents:

```json
{
  "mcpServers": {
    "reversehire": {
      "command": "<PROJECT_PATH>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "reversehire.mcp_server"],
      "timeout": 600000
    }
  }
}
```

- On first use Claude Code asks you to approve the servers defined in a project `.mcp.json`; `/mcp` shows the status.
- `timeout` (milliseconds) is the maximum time per tool call: analysing a site and the first download of the model can exceed the default value.

### Claude Desktop

Open Settings, the Developer tab, "Edit config" (the file is `%APPDATA%\Claude\claude_desktop_config.json`) and add the server under the `mcpServers` key:

```json
{
  "mcpServers": {
    "reversehire": {
      "command": "<PROJECT_PATH>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "reversehire.mcp_server"]
    }
  }
}
```

In JSON the backslashes must be doubled. After saving, quit Claude Desktop completely and reopen it.

### Codex CLI

Add the section from `docs/codex_config.example.toml` to `~/.codex/config.toml` (on Windows `%USERPROFILE%\.codex\config.toml`):

```toml
[mcp_servers.reversehire]
command = '<PROJECT_PATH>\.venv\Scripts\python.exe'
args = ["-m", "reversehire.mcp_server"]
startup_timeout_sec = 30
tool_timeout_sec = 600
```

Codex's default values (10 s for startup, 60 s per tool) are too short for this server, so the example raises them. Alternatively: `codex mcp add reversehire -- <python> -m reversehire.mcp_server`. Use `codex mcp list` or `/mcp` in the interface to check that the server is active.

### Gemini CLI

Add the server to `~/.gemini/settings.json` (all projects; on Windows `%USERPROFILE%\.gemini\settings.json`) or to `.gemini/settings.json` in a project folder, under the `mcpServers` key. The example is in `docs/gemini_settings.example.json`:

```json
{
  "mcpServers": {
    "reversehire": {
      "command": "<PROJECT_PATH>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "reversehire.mcp_server"],
      "timeout": 600000
    }
  }
}
```

- If `settings.json` already exists, add only the `reversehire` entry inside its `mcpServers` object (create the object if it is missing); keep the rest of the file.
- `timeout` is in milliseconds (Gemini's default is already 600000 = 10 minutes; the example states it explicitly). Do not set `trust: true`: it would skip the confirmation of every tool call.
- Alternatively: `gemini mcp add reversehire <python> -m reversehire.mcp_server` (the default scope is the project; `-s user` for all projects). Use `/mcp` inside Gemini CLI to check that the server is connected.

### Syntax verification status

Checked against the official documentation on 2026-11-11 (Claude) and 2026-11-12 (Codex, Gemini):

- Claude Code (`code.claude.com/docs/en/mcp`): `mcpServers` key, fields `command`, `args`, `env`, and `timeout` in milliseconds per server; approval of project servers.
- Claude Desktop (`modelcontextprotocol.io`, "Connect to local MCP servers"): file path on Windows, `mcpServers` key, doubled backslashes in paths, full restart.
- Codex (OpenAI documentation on MCP): `~/.codex/config.toml` (or `.codex/config.toml` in a trusted project), `[mcp_servers.<name>]` table with `command`, `args`, `env`, `cwd`, `startup_timeout_sec` (default 10), `tool_timeout_sec` (default 60), `enabled`; command `codex mcp add <name> -- <command>`.
- Gemini CLI (`google-gemini/gemini-cli`, `docs/tools/mcp-server.md`): `~/.gemini/settings.json` and `.gemini/settings.json`, `mcpServers` key with `command`, `args`, `env`, `cwd`, `timeout` (milliseconds, default 600000), `trust` (default false); command `gemini mcp add [-s user|project] <name> <command> [args...]`; `/mcp` shows the status.

Verified in practice (November 2026, one Windows PC): **Claude Code** and **Codex** answering questions through this server's tools, started by the assistant strip of the app (below), not from the hand-written configuration above. **Not verified in practice:** Claude Desktop and Gemini CLI, and the hand-written configuration for any client (tests were made with the `mcp` SDK client, not with those programs). If an application changes its syntax, its current documentation prevails.

## Tools

All tools return a JSON dictionary. An expected error (missing file, place not found, parameter out of range, service unreachable) becomes `{"error": "readable message"}`, never a traceback.

| Tool | What it does |
|---|---|
| `analyze_cv(path, include_personal_data=False)` | Summary profile from the CV (.pdf, .docx or an image read by OCR: .jpg, .jpeg, .png, .bmp, .tif, .tiff, .webp): roles, skills, spoken languages, degrees, years of experience, seniority, work experiences (title, employer, dates, months), employment gaps, mobility (business travel state and days a year out of 220 working days, relocation, driving licence, own car), inferred sectors. |
| `geocode(place)` | Address, city or coordinates to `lat`, `lon`, `name`. |
| `list_sectors()` | Keys and Italian/English names of the available sectors. |
| `find_companies(place, radius_km, sectors=None, max_companies=30)` | Companies in the radius from the same discovery sources as the app (the local map data if it has been downloaded, the regional register with its official lists and the lists you imported, your own saved companies, Overpass only as fallback, and Wikidata), ranked before any site is read, with website, preliminary relevance (0-1), the identifiers found (VAT number, REA, ATECO, LEI, Wikidata id) and a short `why_selected` text. The answer also lists the state of each source. It always searches by radius; province and region areas are available in the app only. |
| `analyze_company(url, cv_path=None, name=None, include_personal_data=False)` | Hiring status, careers page and up to 20 postings, each with contract, work mode (remote / hybrid / onsite), the salary as published, business travel and the first 600 characters of the description (marked `"untrusted": true`: text from a website). With the CV also the scores `match`, `relevance`, `fit`, `desirability`, `spontaneous` (only without postings), `confidence`, `blocked` and `blocked_reason`, the requirement checks of the best posting (priority must / nice / context, kind, level 0-4 or null = uncertain, status, blocking), reasons and gaps. |
| `saved_searches()` | List of searches made with the app, with ids. |
| `search_results(search_id, hiring_only=False, include_personal_data=False)` | Results of a saved search: hiring companies on top, then by `match`; each with postings and the same scores as `analyze_company`. |
| `company_card(name_or_url, build=False)` | The company card saved in the app: header (REA, ATECO, tax code, VAT number), registry data with the company's own website, profiles sought, products and services, selection system and the CV-screening signal, with the identity status (`CONFIRMED`, `PROBABLE`, `UNVERIFIED`, `CONFLICT`) and, for every value, its source and URL (`provenance`). Also: the `presentation` and `history` sentences taken from the company's own pages, the company's published `contacts` (role mailboxes such as an HR address; **named people are left out**), the `financials` (revenue, net profit and employees by year, with source, currency and scope), the `field_conflicts` between sources and the `checked` dates of each section. Fields that were not found are blank and listed under `identity.missing_from_free_sources`. Your own notes, contacts, tags and status are never returned. With `build=True` the card is built or refreshed first: this reads a few pages of the company site slowly and asks VIES, GLEIF, Wikidata and the ESEF filings; it never calls the paid provider. |
| `list_company_cards()` | The saved company cards, most recently updated first: name, website, identity status, VAT number, REA, ATECO, hiring status, favourite flag and the date the full card was built. No notes, contacts or tags. |

Limits per single call (the server enforces them; the app itself has no fixed limits): `radius_km` from 0.1 to 50; `max_companies` from 1 to 100 (default 30). For larger areas make several requests or use the app. `path` and `cv_path` accept only existing `.pdf`, `.docx` or image files (`.jpg`, `.jpeg`, `.png`, `.bmp`, `.tif`, `.tiff`, `.webp`; images and scanned PDFs are read by the local OCR, at most 10 pages); `url` only `http://` or `https://` to public sites (private and local addresses are refused). The server respects the app's constraints: 1 request per second to Nominatim, every discovery and register request through the polite gateway (at least 2 seconds between requests to the same host, few hosts at once, stop on HTTP 429), `robots.txt` and delays between requests to the same site. For this reason `analyze_company` can take tens of seconds; the first evaluation with a CV also downloads the local embedding model (about 470 MB). With a CV, `analyze_company` also makes one request to the ESCO API (the occupation identifier of the CV's main role, cached) to read the essential skills of that occupation; reading the CV itself sends the role titles to ESCO, as in the app.

The nine tools did not change with the later features of the app: the job search, the recruiter analysis, the maps, the affinity verdicts, the "Who could be interested in my profile" suggestions, the idle analysis, the regional register manager and the **Applications sent** tracker are not exposed through MCP (no tool reads the applications database or the affinity results). A company card read with `company_card` can contain the financial figures you pasted yourself, each marked with its source and with the source you stated. Images and scanned PDFs read by `analyze_cv` use the **normal** text recognition (Latin and Cyrillic, bundled with the program); the optional advanced reader is used only by the app's own CV analysis.

### Example requests

- "Read `<path>/cv.pdf` and tell me which roles and sectors emerge."
- "Find IT and software companies within 15 km of Piazza del Duomo, Milano, and give me the first 20 with their website."
- "Show the company card of <company name>: is the identity confirmed, and what does it say about CV screening?"
- "Analyse https://www.example.com with my CV in `<path>/cv.pdf`: are they hiring? Which postings suit me and what am I missing?"
- "Show my saved searches and, for the latest one, only the companies that are hiring."
- "Compare the two postings found at these two companies against my profile."
- "Read the scanned CV in `<path>/cv.png`: which work experiences and gaps do you see?"

Typical flow: `analyze_cv`, then `find_companies` with a place and radius, then `analyze_company` on the companies of interest. The list comes from OpenStreetMap, Wikidata and the other sources and is incomplete; scores are indicative and must be read together with their reasons.

## Privacy

With MCP, the data returned by the tools reaches the cloud model you chose (Claude, Codex or Gemini) and stays in the conversation; the same holds for the assistant strip. For this reason:

- MCP access is off until you switch it on (the dot at the top right of the app window); you can switch it off again at any time and it applies from the next tool call.

- By default `analyze_cv` returns only the extracted values (roles, skills, languages, degrees, years, seniority, work experiences, gaps, mobility, sectors). Note that the work experiences include the **employer names and dates** as written in the CV. It does **not** return the CV text, the evidence lines, the home town, the file path, or the name and contact details. Only with `include_personal_data=True` does it add evidence, home town, path and full text: use it only if you accept that this content leaves the computer.
- The reasons, gaps and requirement checks returned by `analyze_company` and `search_results` are sentences generated by the app; by default the CV lines they may quote are removed. With `include_personal_data=True` they keep the quoted CV lines (and each requirement check its `evidence`).
- `saved_searches` does not include the CV path.
- Even with the default value, roles, skills and degrees describe the person: if you do not want to share them, do not use MCP.
- The CV is never sent to company websites. With the Firecrawl engine enabled in the app settings, only company URLs are sent to Firecrawl.
- `company_card` and `list_company_cards` return company data only (no notes, contacts or tags of yours); the website texts quoted in a card, such as statements about automated processing or the presentation and history, come from the company's pages and are data, not instructions (the card is marked `untrusted`). The contacts of a card are role mailboxes only; e-mail addresses that look like a person's name and the 16-character tax code of a sole trader are left out.
- The server writes nothing outside the app's data folder (the store and `mcp_usage.json`, which holds only the time of the last call and a counter) and does not modify the configuration files of Claude, Codex or Gemini. Log and error messages go to stderr; only the protocol goes through stdout.

## The assistant strip (in-app chat)

The row above the status bar of the app lets you ask questions in plain language; they are answered by **your own Claude Code or Codex** program, which reads your ReverseHire data through this server (user manual, section 16). How it is wired, so that you know what it can and cannot do:

- ReverseHire starts one process per question (`claude -p` or `codex exec`) and gives it a connection file **generated by the app**: only the `reversehire` server, started with the Python of the app. Your own `.mcp.json` or Codex configuration is not used. Follow-up questions resume the program's own conversation.
- **Claude Code** is started with `--restricted` (so your user, project and local settings, hooks and plugins are ignored and permission shortcuts are refused), with **no built-in tool** (`--tools ""`), with `--strict-mcp-config` and `--allowedTools mcp__reversehire` in `dontAsk` mode (everything that is not a ReverseHire tool is denied without asking), without slash commands and browser integration, and with a limit on the number of turns. **Codex** is started with `--ignore-user-config` and `--ignore-rules`, a read-only sandbox, approval policy `never`, the shell, web, browser, plugin and hook features off, project documents off, and only the `reversehire` server.
- The question goes through standard input, never on the command line; no secret is put on the command line; the process runs in an empty folder owned by the app (`assistant/workspace` in the data folder).
- If the program starts with permissions or tools that are not allowed, the app stops it and says so. A question that lasts more than 15 minutes is stopped.
- The assistant is told to answer only from the tools, not to invent facts, to say plainly when MCP is off or a tool returns nothing, and to reply in the language of the interface.
- **MCP must be on** (the dot at the top right). When it is off the assistant says so; the line under the strip says the same before you ask.
- **Questions count against your own plan** with Claude or OpenAI; the data returned by the tools reaches that provider (see Privacy below).

## Troubleshooting

- Every tool answers "MCP access to ReverseHire is switched off": click the red "MCP off" dot in the app and tick the switch.
- The client does not see the server: check the Python path and try `python -m reversehire.mcp_server` by hand; if `mcp` is missing, you will see a message saying the `mcp` package is not installed.
- Timeout during `analyze_company`: raise the timeout (`timeout` in `.mcp.json` and in Gemini's `settings.json`, `tool_timeout_sec` in Codex).
- "Place not found": write a more precise address or give the coordinates as "lat, lon".
- "No discovery source answered" or an Overpass error: without downloaded map data the server falls back on the public Overpass service, which is sometimes busy. Download the map data of the region in the app (location tab) and try again; results already obtained are cached.

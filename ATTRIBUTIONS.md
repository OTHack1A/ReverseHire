# Attributions and third-party licences

ReverseHire is distributed under the MIT licence (see `LICENSE`). This file lists the third-party data, services, models, font, libraries and source code it uses, with the conditions to respect. Where a licence has not been verified against package metadata or the official source, it says "to be verified".

## Data and services

| What | Licence / terms | Obligations |
|---|---|---|
| **OpenStreetMap** (companies, addresses, coordinates) | Open Database Licence (ODbL) 1.0 | Attribution "© OpenStreetMap contributors" with a link to <https://www.openstreetmap.org/copyright>; anyone who redistributes a derived database must release it under ODbL. Data stored in the local cache comes from OSM. Test fixtures under `tests/data/geo/` are being replaced with invented data. |
| Public **Nominatim** (geocoding) | Usage policy: <https://operations.osmfoundation.org/policies/nominatim/> | At most 1 request per second, a User-Agent header that identifies the application, result caching, no bulk searches. The app applies these limits. Whoever redistributes it should set their own contact in the User-Agent (the `user_agent` setting). |
| Public **Overpass API** | Fair use: <https://wiki.openstreetmap.org/wiki/Overpass_API> | Moderate use, caching, no excessive load. The servers may refuse requests when overloaded. Used only as the fallback of the local map data; every request carries the User-Agent `ReverseHire/<version> (+https://github.com/OTHack1A/ReverseHire)` and goes through the polite gateway (`discovery/gateway.py`). |
| **OpenStreetMap regional extract** (`.osm.pbf`, one file per Italian region, e.g. `emilia_romagna-latest.osm.pbf`, from `download.openstreetmap.fr/extracts/`) | ODbL 1.0 (OpenStreetMap data); the extracts are published by the OpenStreetMap-France association: <https://download.openstreetmap.fr/> | Downloaded only on request ("Download / refresh map data") into `<data folder>/osm/`, through the polite gateway (throttled, resumable, reused for 30 days), then indexed locally (`discovery/osm_extract.py`, with the built-in reader `discovery/pbf.py`). The local index is a derived database: keep the "© OpenStreetMap contributors" attribution and, if redistributed, release it under ODbL. Not in the repository. A custom extract URL can be set for another area. |
| **Wikidata** (company websites, VAT numbers, LEI, industry, headquarters; for a company card also the founding year, employees, the chief executive and, as a low-authority source, revenue P2139, net profit P2295 and employees P1128) | CC0 1.0 (<https://www.wikidata.org/wiki/Wikidata:Licensing>); query service User-Agent policy <https://meta.wikimedia.org/wiki/User-Agent_policy> | No attribution required (credited anyway as "Wikidata"). One SPARQL query per region, cached for 30 days, with an identifying User-Agent and contact URL. On a card an item is found only by an identifier (the QID the company's site links, its LEI or its EU VAT number), a few requests through the polite gateway, only when the user builds the card. |
| **ISTAT ATECO 2025** classification (`src/reversehire/discovery/data/ateco_2025.csv`, codes and Italian/English titles) | CC BY 4.0 (<https://creativecommons.org/licenses/by/4.0/>), source ISTAT, `StrutturaATECO-2025-IT-EN-DE.xlsx` | Attribution "Fonte: ISTAT" (shown in the code header and here). The German titles and the ordering column were removed; nothing else changed. |
| **Overture Maps places** (optional source, off by default) | CDLA-Permissive-2.0 / Apache-2.0 depending on the upstream source (<https://docs.overturemaps.org/attribution/>) | Read only when the user enables the source and installs `duckdb`; the attribution of the release is kept with the cached data. |
| **VIES** (EU VAT number validation, European Commission) | Public service: <https://ec.europa.eu/taxation_customs/vies/> (disclaimer and usage terms on the site) | Used only to confirm a VAT number found for a company card, on demand, at most one request every 2 s, cached for 30 days. A "not registered" answer only means the company is not an intra-EU trader. |
| **GLEIF** LEI records (Global Legal Entity Identifier Foundation API) | CC0 1.0 (<https://www.gleif.org/en/meta/lei-data-terms-of-use>) | Used on demand for a company card, at most one request every 2 s, cached for 30 days. |
| **Regional register: RNA – Registro Nazionale Aiuti di Stato** (open data of state aid, de minimis aid and hiring incentives; <https://www.incentivitalia.gov.it/open-data/aiuti>) | CC BY 4.0 (<https://creativecommons.org/licenses/by/4.0/>), terms on <https://www.incentivitalia.gov.it/open-data/aiuti> | Attribution "Registro Nazionale Aiuti di Stato – Incentivi Italia, open data (CC BY 4.0)". Downloaded only when the user asks in the Register manager (Search menu), through the polite gateway (resumable, verified), into `<data folder>/registers/raw/rna/`; only companies (11-digit tax codes, never natural persons) of the regions chosen in the settings are kept in the local register `<data folder>/registers/register.sqlite`. The aid sector is shown as a hint, never as the company's ATECO code. |
| **Regional register: GLEIF Golden Copy** (the whole LEI register; <https://www.gleif.org/en/lei-data/gleif-golden-copy>) | CC0 1.0 (<https://www.gleif.org/en/meta/lei-data-terms-of-use>) | No attribution required (credited as "GLEIF Golden Copy, Global Legal Entity Identifier Foundation (CC0)"). Downloaded on request into `<data folder>/registers/raw/gleif/`, Italian companies of the chosen regions kept in the local register. |
| **Regional register: OpenCoesione – soggetti** (recipients of cohesion funds; <https://opencoesione.gov.it/it/opendata/>) | CC BY 4.0 (<https://creativecommons.org/licenses/by/4.0/>), terms on <https://opencoesione.gov.it/it/opendata/> | Attribution "OpenCoesione – Dipartimento per le politiche di coesione (CC BY 4.0)". Downloaded on request into `<data folder>/registers/raw/opencoesione/`; companies of the chosen regions kept in the local register. |
| **Regional register: AGCM – rating di legalità** (companies with a legality rating; <https://dati.agcm.it/>) | CC BY 4.0 (<https://creativecommons.org/licenses/by/4.0/>), terms on <https://dati.agcm.it/> | Attribution "Autorità Garante della Concorrenza e del Mercato, rating di legalità (CC BY 4.0)". Downloaded on request (respecting the site's crawl delay) into `<data folder>/registers/raw/agcm/`. A rating is a statistical signal only: later revocations must be checked on later lists. |
| **Registro Imprese lists bought by the user** ("Elenco" CSV/XLSX) and other company lists the user imports | Bought by the user from the Chambers of Commerce under their own terms: personal use only | Imported by the user into the same local register (Register manager › Import), never redistributed and never sent anywhere; values keep their source and date. |
| **filings.xbrl.org** (ESEF annual financial reports of listed companies; revenue, net profit and, rarely, average employees) | Public API and files published by the XBRL International filings repository: <https://filings.xbrl.org/>. The site states that there are no restrictions on the ways its data can be used and has no `robots.txt` (read November 2026; **to be verified** against its current terms before redistributing the figures) | Used only when the user builds a company card, for a company with a LEI: one listing request, then the figure files of the latest filings (a few MB each), through the polite gateway, cached; only the figures are kept (source link and date stored with them), the files are not. Covers listed companies only. |
| **Registro Imprese** (<https://www.registroimprese.it/>, the Italian business register, free search) | Terms of the site and of the Chambers of Commerce; paid documents ("visura", balance sheets) are bought by the user from the site under its own terms | **Manual lookup only:** the app opens the free search page in the user's browser and copies a name or number to the clipboard; it never reads, scrapes or queries the site. Values the user copies into the form are validated and kept with the source "Registro Imprese (entered by hand)" and the date. Balance-sheet figures the user buys can be typed or imported from a CSV file. |
| **Openapi** (company.openapi.com, "IT-full" product; optional, paid, off by default) | Commercial service with its own terms and prices (<https://openapi.com/products/italian-full-company>). Prices in the code (0.30 EUR for a single call, from about 0.08 EUR on plans, excluding VAT) were read from its price page on 2026-11-17 and can change | The user's own key, typed in the settings; one call per company, only after a confirmation that shows the cost, never automatically. The VAT number or tax code and the key (in the `Authorization` header only) are sent to Openapi; the key is never logged, cached or shown. **Never tested against the live service.** |
| **ESCO** (European classification of occupations, skills, qualifications) | © European Union; ESCO can be reused with attribution. Full terms: <https://esco.ec.europa.eu> | Cite ESCO and the European Commission as the source. **To be verified**: the exact wording of the reuse notice before redistributing ESCO data. The app does not include ESCO data: it queries the public API and sends only role titles and, to read the essential skills of an occupation, the ESCO occupation identifier. |
| **Claude Code** (Anthropic) and **Codex** (OpenAI), the user's own installed programs, used by the assistant strip (optional) | Terms of use of the respective services and of the user's own plan | Not distributed with ReverseHire and not installed by it. They are started only when the user asks a question, restricted to ReverseHire's MCP tools; ReverseHire never reads or stores their credentials, and the questions count against the user's own plan. What the tools return reaches the provider chosen by the user. |
| **Firecrawl** (optional) | Commercial service with its own terms and plans | The user's own API key; only company URLs are sent. Free plan limits **to be verified** on the official site. |
| Company websites and hiring systems (Greenhouse, Lever, Ashby, Workable, Recruitee, SmartRecruiters, Personio, Workday, BambooHR) | Terms of use of the respective sites | `robots.txt` respected, delays between requests, several different sites may be visited in parallel, but the per-site delays still apply. Glassdoor is not read. Test fixtures that came from real responses are being replaced with invented data (see `docs/PUBLISHING.md`). |
| **OpenStreetMap tiles** (the integrated map: Results, Job search, Location and start) | Tile usage policy: <https://operations.osmfoundation.org/policies/tiles/>; map data ODbL 1.0 | The attribution "© OpenStreetMap contributors" (linked to <https://www.openstreetmap.org/copyright>) is always visible on the map. Tiles are requested only when the user opens a view that shows the map, only for the visible area (no prefetching, no bulk or offline download), with the application's own User-Agent and the normal HTTP cache (`<data folder>/cache/map/http`). Settings: "no map tiles (offline)" and a custom tile URL (https, or a local http/file server) for heavier use. |
| **Job search: staffing agencies' own sites** (Randstad, Manpower, Synergie: job ads published as JobPosting data in their sitemaps) | Terms of use of each site, read on 2026-11-18 (links in the settings page "Job search"): Randstad and Synergie do not address automated reading; Manpower forbids only automation that harms its services | Read slowly through the polite gateway (pause per host, identifying User-Agent, `robots.txt` respected, cache), only when the user runs a job search, a limited number of pages per agency (setting). Results are shown as links to the original ad. **Adecco, Gi Group and Umana** forbid automated extraction in their terms: they are listed in the settings, greyed, and **can never be switched on**. |
| **Adzuna**, **Jooble**, **Careerjet** APIs (optional, off by default) | The terms of each API (<https://developer.adzuna.com/>, <https://jooble.org/api/about>, <https://www.careerjet.it/partners/api>) | Used only with the user's own keys, typed in Settings → Job search (kept in the settings file, never written to the log). Only the search words and the place are sent. Adzuna results carry the attribution "Jobs by Adzuna" as its terms ask. **Never tested against the live services.** |
| **Pasted pages** (LinkedIn, Indeed or any other page the app must not read) | Terms of the respective sites | The app never opens or reads these sites: the user copies a page in the browser and pastes it into "Paste posting / company page…"; lines naming people are dropped before anything is stored, and nothing is saved before the user confirms the reviewed fields. The LinkedIn and Indeed buttons of the Job search only open the same search in the user's browser. |
| **Indeed** and **LinkedIn** job boards (optional, off by default) | Terms of use of the respective sites | Read only when the user installs the `jobboards` extra and ticks the switch in the settings, through the JobSpy library. **Automated access may violate these sites' terms of use** and can be blocked; the feature is experimental. Only the role titles and the town are sent, never the CV. Separately, the **Open LinkedIn** buttons and the "people" links of a company card only open the user's browser on a LinkedIn search page: nothing is read there. |

## Embedding model

- `intfloat/multilingual-e5-small` (Wang et al., "Multilingual E5 Text Embeddings: A Technical Report", arXiv:2402.05672; <https://huggingface.co/intfloat/multilingual-e5-small>), downloaded on first use from Hugging Face and run locally with fastembed/ONNX (the ONNX file published by intfloat in the same repository). Model licence: **MIT** (model card and repository licence tag).
- Fallback model `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`: Apache-2.0 according to fastembed's model list.
- The model is not included in the repository.

## OCR models

- The OCR engine RapidOCR ships the **PP-OCR** text detection and recognition models (from the PaddleOCR project) inside its wheel, as ONNX files. Licence: **Apache-2.0** (RapidOCR and PaddleOCR). They run locally; nothing is downloaded at run time and no image leaves the computer.
- **PP-OCRv5 Cyrillic mobile recogniser** (PaddlePaddle / PaddleOCR), bundled with the package as `src/reversehire/cv/data/ocr_cyrillic_rec.onnx` (8,048,799 bytes, SHA-256 `5371ee1d...`) with its character list `ocr_cyrillic_keys.txt` (taken from the model's `inference.yml`). Source: Hugging Face `PaddlePaddle/cyrillic_PP-OCRv5_mobile_rec_onnx`, revision `2cef88145434beb8afa9dd82d77d799eb1ad7b29`. Licence: **Apache-2.0**; the licence text is in `src/reversehire/cv/data/ocr_models_LICENSE.txt`. Unmodified. It lets the normal OCR read Ukrainian (Cyrillic) scans.
- **Advanced reader** (optional, downloaded only when the user agrees, into `models/ocr/advanced-v1/`, with a `manifest.json` and a copy of the Apache-2.0 licence; every file pinned by revision and SHA-256 in `cv/ocr_models.py`), all **Apache-2.0**, unmodified, from Hugging Face:
  - `PaddlePaddle/PP-OCRv6_medium_det_onnx` (revision `61323801669c338b7891481ec7bac61ce31b576a`): PP-OCRv6 medium text detector, 62.0 MB;
  - `PaddlePaddle/PP-OCRv6_medium_rec_onnx` (revision `50c7eacafc52fa7bcf4194e8cd08e46f8558504b`): PP-OCRv6 medium Latin recogniser, 76.6 MB, and its `inference.yml` (character list);
  - the PP-OCRv5 Cyrillic recogniser above (copied from the bundled file, or downloaded from the same repository and revision).
- Optional on Windows: **onnxruntime-directml** 1.24.4 (Microsoft, **MIT**), the DirectML execution provider used by the advanced reader when installed.
- Evaluated and not adopted: Indra OCR (model weights under a modified OpenRAIL-M licence, not compatible with this MIT app) and VLM-based OCR (too slow on a CPU, no working GPU path on the target hardware).

## Translation models (local CV translation into English)

- **OPUS-MT** `Helsinki-NLP/opus-mt-it-en` (<https://huggingface.co/Helsinki-NLP/opus-mt-it-en>) and `Helsinki-NLP/opus-mt-uk-en` (<https://huggingface.co/Helsinki-NLP/opus-mt-uk-en>), by the OPUS-MT project, Language Technology Research Group, University of Helsinki (<https://github.com/Helsinki-NLP/Opus-MT>), trained on OPUS data. Attribution: "Helsinki-NLP, OPUS-MT, Tiedemann & Thottingal 2020". Licence: treated as **CC-BY-4.0** (<https://creativecommons.org/licenses/by/4.0/>), the licence under which the OPUS-MT project distributes its pre-trained models; the Hugging Face model cards also state Apache-2.0, and CC-BY-4.0 is the stricter of the two.
- Used in the ONNX, int8-quantised conversions published by Xenova: `Xenova/opus-mt-it-en` (<https://huggingface.co/Xenova/opus-mt-it-en>, revision `fd0b89b9c052adc1f2f64152f555aa17353728be`) and `Xenova/opus-mt-uk-en` (<https://huggingface.co/Xenova/opus-mt-uk-en>, revision `b2ad6afa424aaeff8913ff350860b7a7b7c0a607`), files pinned by SHA-256 in `cv/translate.py`. Changes made to the original models (by Xenova): ONNX export and int8 quantisation; ReverseHire uses the converted files unchanged. The conversion repositories declare no separate licence, so the original model licence applies.
- They run locally with onnxruntime and the `tokenizers` library; the CV text never leaves the computer. They are not in the repository: they are looked for in `models/translation` of the data folder (or of the other model folders, or the folder named by `REVERSEHIRE_TRANSLATION_MODELS`) and are downloaded on request (`python -m reversehire.cv.translate download it uk`) or copied there with a `models` folder.
- CTranslate2 (MIT) was evaluated and not adopted: its compiled extension is blocked by Windows Smart App Control.

## Face detection model (CV photo)

- **YuNet** face detector `face_detection_yunet_2023mar.onnx` from the OpenCV Zoo (<https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet>), bundled in `src/reversehire/cv/data/` (232,589 bytes, SHA-256 `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`). Licence: **MIT**, "Copyright (c) 2020 Shiqi Yu"; the full licence text ships next to the model as `face_detection_yunet_LICENSE.txt`. Run locally with `cv2.FaceDetectorYN` only to locate the portrait in a CV (detection, never recognition). OpenCV 5 wheels ship no Haar cascade and no `CascadeClassifier`, so the Haar cascade (Intel License Agreement, BSD-style) was not bundled; it is still used when an installed OpenCV 4.x provides it.

## Font

| Font | Licence | Obligations |
|---|---|---|
| **JetBrains Mono**, © JetBrains s.r.o. and The JetBrains Mono Project Authors (<https://github.com/JetBrains/JetBrainsMono>) | SIL Open Font License 1.1 (<https://openfontlicense.org>) | Bundled with the app as the interface font. The font may be used, bundled and redistributed with software, but not sold on its own; keep the copyright notice and the licence text with every copy of the font. The OFL text ships next to the font files (`src/reversehire/resources/fonts/OFL.txt`, copyright line "Copyright 2020 The JetBrains Mono Project Authors"); the font declares no Reserved Font Name. |

## Map libraries (bundled)

| Library | Version | Licence | Where |
|---|---|---|---|
| **Leaflet** (<https://leafletjs.com>) | 1.9.4 | BSD-2-Clause, "Copyright (c) 2010-2023, Volodymyr Agafonkin; Copyright (c) 2010-2011, CloudMade" | `src/reversehire/mapview/web/vendor/leaflet/` (licence text in `LICENSE` next to the files) |
| **Leaflet.markercluster** (<https://github.com/Leaflet/Leaflet.markercluster>) | 1.5.3 | MIT, "Copyright 2012 David Leaver" | `src/reversehire/mapview/web/vendor/markercluster/` (licence text in `MIT-LICENCE.txt`) |

Both run inside Qt WebEngine from the bundled files (no CDN); when the web engine cannot start, a plain painter map is shown instead.

## Libraries

Licences read from the metadata of the packages installed in the project's virtual environment (`pip`/`importlib.metadata`).

| Package | Version tested | Licence | Use |
|---|---|---|---|
| PySide6 (Qt for Python) | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only (Qt itself: LGPL-3.0 / GPL / commercial) | graphical interface |
| shiboken6 (dependency of PySide6) | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | Qt bindings |
| pypdf | 7.0.1 | BSD-3-Clause | PDF reading, merging the PDFs of an exported application |
| python-docx | 1.2.0 | MIT | DOCX reading, Word export |
| pywin32 | 312 | PSF (Python Software Foundation licence, from the package metadata) | Windows only: classic Outlook through COM, to open an application draft with the PDF attached (`mail/methods.py`) |
| httpx | 0.28.1 | BSD-3-Clause | HTTP requests |
| trafilatura | 2.3.1 | Apache-2.0 | text extraction from pages |
| lxml | 6.1.3 | BSD-3-Clause | HTML/XML parsing |
| rapidfuzz | 3.14.6 | MIT | string matching |
| numpy | 2.5.4 | BSD-3-Clause (plus licences of bundled components: 0BSD, MIT, Zlib, CC0-1.0) | numerics |
| fastembed | 0.9.1 | Apache-2.0 | embeddings |
| onnxruntime (dependency of fastembed) | 1.31.1 | MIT | model execution |
| tokenizers (also a dependency of fastembed) | 0.23.3 | Apache-2.0 (Hugging Face; the wheel metadata leaves the field empty, licence from the project repository) | tokenisation for the local translation models |
| truststore | 0.10.5 | MIT | system certificate store for HTTPS |
| lingua-language-detector | 2.2.0 | Apache-2.0 (from the metadata classifier) | language detection |
| rapidocr | 3.10.0 | Apache-2.0 | local OCR of image CVs and scanned PDFs (PP-OCR models bundled, Apache-2.0) |
| opencv-python-headless (dependency of rapidocr) | 5.0.0.93 | Apache-2.0 (OpenCV; the wheel bundles third-party libraries with their own licences) | image processing for OCR |
| shapely (dependency of rapidocr) | 2.2.0 | BSD-3-Clause | geometry of the text boxes |
| pyclipper (dependency of rapidocr) | 1.4.0 | MIT (bundles Clipper, Boost Software License 1.0) | polygon offsetting |
| omegaconf (dependency of rapidocr) | 2.4.0 | BSD-3-Clause (classifier "BSD License") | configuration |
| colorlog (dependency of rapidocr) | 6.12.0 | MIT | logging |
| six (dependency of rapidocr) | 1.17.0 | MIT | compatibility |
| PyYAML, requests, tqdm, Pillow (dependencies of rapidocr) | 6.0.3, 2.34.2, 4.70.1, 13.0.0 | MIT; Apache-2.0; MPL-2.0 AND MIT; MIT-CMU | configuration, downloads (not used at run time by the app), progress, images |
| mcp (optional) | 2.3.0 | MIT | MCP server (also used by the assistant strip) |
| duckdb (optional extra `overture`) | not installed by default | MIT | querying Overture Maps GeoParquet files |
| python-jobspy (optional extra `jobboards`) | 1.4.1 | MIT | job boards (Indeed, LinkedIn); used as a library, no code copied. It pulls pandas (BSD-3-Clause), beautifulsoup4 (MIT), markdownify (MIT), curl_cffi, pydantic and requests, each with its own licence |
| pytest (development) | 9.2.0 | MIT | tests |
| fpdf2 (development) | 2.9.0 | LGPL-3.0 | only to generate the sample CV PDFs; not a dependency of the app |
| playwright (optional) | not installed | **to be verified** | crawl engine for dynamic sites; it also downloads the Chromium browser, with its own licences |

Note on Qt and the LGPL-3.0: the app uses PySide6 as a dynamically linked library installed from PyPI and does not modify it. Anyone who redistributes a packaged build must comply with the LGPL-3.0 terms for Qt: provide the licence texts, a notice that Qt is used under the LGPL-3.0, access to the corresponding source and the means to replace the library. The ready-made builds do this (one-folder builds, with Qt as separate replaceable shared libraries; see "Third-party licences in the ready-made builds" below); installing from source, as described in the README, is not affected.

Indirect dependencies also have their own licences; the full list can be obtained with `pip list` and `pip show <package>` in the virtual environment.

The compiled `osmium` package is **not** used any more: regional extracts are read with the built-in reader `discovery/pbf.py`. The Excel files of the export are written with the standard library only (no spreadsheet library).

## Third-party licences in the ready-made builds

The ready-made builds (Windows, Linux, macOS; PyInstaller one-folder builds) contain third-party open-source components besides ReverseHire's own MIT code: the Python 3.14 interpreter and standard library (PSF licence), Qt 6.12 and PySide6 / shiboken6 under the **LGPL-3.0** (shipped unmodified as separate, replaceable shared libraries, with Chromium inside Qt WebEngine and PDFium inside Qt PDF), onnxruntime (MIT), OpenCV (Apache-2.0), Shapely with GEOS (LGPL-2.1), NumPy, lxml, cryptography, tokenizers, Pillow and the other Python packages of the table above, the bundled Leaflet libraries, the JetBrains Mono font and the two bundled ONNX models. Each keeps its own licence.

Every package contains `THIRD-PARTY-NOTICES.txt` (one entry per component: version, licence, copyright holder, upstream URL, the LGPL notice for Qt and the written offer of the corresponding source) and a `licenses` folder with the full licence texts (LGPL-3.0, GPL-3.0, LGPL-2.1, Apache-2.0, MPL-2.0, BSD-3-Clause, PSF-2.0, OFL-1.1 and ReverseHire's MIT licence). Source of these files in the repository: `tools/packaging/THIRD-PARTY-NOTICES.txt` and `tools/packaging/licenses/`. The models downloaded at first use (embedding, translation, advanced OCR) are not part of the packages and are listed in the sections above.

## Source code adapted from other projects

By a project decision, code from MIT, Apache-2.0 or BSD repositories may be adapted with attribution. The MIT licence requires the copyright notice and the permission notice to be kept with substantial portions of the code; the full MIT text is the same as in this project's `LICENSE`, with the copyright line given below.

| Project | Licence and copyright | What was adapted | Where |
|---|---|---|---|
| **erga-mcp** | MIT, "Copyright (c) 2026 Erga MCP contributors" | From `src/erga_mcp/applications/role_profile.py`: the required / preferred / context weights, the cue regexes for required and preferred items (extended with Italian), the stop words and the light `_stem` token overlap. Its list of ATS host suffixes was merged into the list of recognised but unsupported ATS hosts. | `src/reversehire/scoring/requirements.py` (notice in the module docstring); `UNSUPPORTED_ATS_HOSTS` in `src/reversehire/web/ats.py` |
| **ats-jobs-scraper-cli** | MIT, "Copyright (c) 2026 Grey Ridge Signals Group LLC" | Workday (CXS) and BambooHR endpoints, request payload fields, host guards and field mapping from `src/adapters.js`, re-written in Python | `src/reversehire/web/ats.py` (notice in the module docstring) |
| **mr-jobs** | MIT, "Copyright (c) 2025 humancto" | The `ATS_DOMAINS` host list, merged into the list of recognised but unsupported ATS hosts | `UNSUPPORTED_ATS_HOSTS` in `src/reversehire/web/ats.py` |
| **JobSpy** (`python-jobspy`) | MIT, "Copyright (c) 2023 Cullen Watson" | Nothing copied: used as an optional library through its scraper classes | `src/reversehire/web/jobboards.py` |

Ideas only, no text or code copied:

- **resume-job-matcher** (no licence found): the anchored 0-4 scale for requirement evidence and the rule that an uncertain requirement passes the gate (`scoring/requirements.py`).
- **Resume-Matcher** (Apache-2.0, <https://github.com/srbhr/Resume-Matcher>): the layout checks of a CV file before parsing (columns, tables, text boxes, header/footer text, embedded fonts and glyphs) were re-implemented from its ideas in `src/reversehire/recruiter_sim/layout.py`; no code was copied.
- **fairlearn** (MIT, Microsoft Corporation and Fairlearn contributors): the principle of keeping protected or proxy attributes (name, photo, date of birth, gender, nationality, marital status) out of the scoring text. The library is not a dependency.

**To be verified** before publishing: that the copyright lines above still match the LICENSE files of the upstream repositories.

## Sample data

The CVs in `data/sample_cvs/` are invented (fictional people, companies and contact details, `.invalid` email addresses), generated by `tests/data/cv/generate_cv.py`.

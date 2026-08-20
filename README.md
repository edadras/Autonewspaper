# AI Newspaper Studio

Autonomous newspaper and magazine production for Windows. You supply the copy
and the pictures; the application acts as editor, art director and InDesign
operator, and gives you back a print-ready PDF, the InDesign document and its
IDML companion.

```
content ──▶ editorial analysis ──▶ layout engine ──▶ InDesign ──▶ Vision QA ──▶ PDF
                    │                     │              │            │
              news value,          constraint-based   scripting    geometry +
              headlines,           candidates and     API, not     pixels +
              page plan            scoring            the mouse    the model
```

## What it does

Press **Generate Newspaper** once and the application:

1. analyses the imported copy and scores its news value;
2. writes headlines, decks, leads and summaries — never the body text;
3. assigns every story to a page and an editorial area;
4. measures every picture and picks the best one for each story;
5. generates the missing pictures with the configured image provider;
6. builds several candidate layouts per page, scores them and keeps the best;
7. drives InDesign through its scripting API to build the real document;
8. renders each page, reviews it, corrects it and re-renders until it passes;
9. exports the PDFs, the `.indd`, the IDML and page previews;
10. snapshots the project so the run can be reproduced or rolled back.

## Requirements

| | |
|---|---|
| Operating system | Windows 10 or 11 (64-bit) for the Adobe automation |
| Python | 3.11 or newer (only for running from source) |
| Adobe InDesign | 2020 or newer — optional, see below |
| Adobe Photoshop | 2020 or newer — optional |
| AI provider | optional; the application ships with an offline analyser |

**Without InDesign** the application still runs end to end: it plans the
layout, renders the pages with its built-in renderer and produces a PDF. The
run result and the log always say which engine produced the output, so a PDF
from the built-in renderer is never mistaken for an InDesign export.

**Without an API key** the offline rule-based analyser scores the news, writes
the display type and plans the pages. It is deterministic and needs no
network.

## Install

### From the installer

Run `AI-Newspaper-Studio-Setup.exe`. It installs the application, a desktop
shortcut, a Start-menu entry and an uninstaller, and creates the data folder
at `%LOCALAPPDATA%\AINewspaperStudio`.

### From source

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python scripts/build_builtin_templates.py
python -m app.main
```

## First run

```powershell
python scripts/seed_demo_project.py --generate
```

That creates a demonstration edition with real Persian copy and test
pictures, runs the whole pipeline and prints the PDFs it produced.

In the application itself: **New Project** → **Content** (drop files or paste)
→ **Assets** (drop pictures) → **Dashboard** → **Generate Newspaper**.

## Command line

```powershell
python -m app.main                              # the desktop application
python -m app.main --project demo_edition       # open a project on start
python -m app.main --generate --project demo    # head-less run
python -m app.main --diagnostics                # check the installation
```

## How Adobe is driven

Automation degrades through a fixed priority chain; the mouse is the last
resort, never the default:

| Priority | Mechanism | Used for |
|---|---|---|
| 1 | COM scripting API (`DoScript`) | everything |
| 2 | COM with the script passed as a file | long scripts, encoding-sensitive payloads |
| 3 | A resident queue runner in the Scripts Panel | when COM is unavailable or blocked |
| 4 | Windows UI Automation | operations with no scripting path |
| 5 | Mouse and keyboard | disabled unless you enable it |

Every operation is expressed once, as an ExtendScript program generated from
the layout plan, and returns a structured JSON result. The strategy that
actually ran is recorded in the run result.

## Layout

The layout engine is constraint-based. For each page it builds candidates
with several strategies (hierarchical, modular, horizontal, vertical,
feature), checks each against the hard constraints — no overlap, inside the
page, margins, minimum type size, image resolution, no overflow — and scores
the survivors on visual balance, hierarchy, readability, space efficiency,
image quality and typography, minus penalties. The best candidate wins.

## Persian and right-to-left

Persian, Arabic, Turkish and English are first-class. Text is normalised,
digits are converted, paragraph direction and the World-Ready composer are
applied in InDesign, and the built-in renderer shapes Arabic script correctly
(through Raqm when Pillow provides it, `arabic-reshaper` + `python-bidi`
otherwise).

## Documentation

| Document | What it covers |
|---|---|
| [docs/user-guide.md](docs/user-guide.md) | Working with the application |
| [docs/configuration.md](docs/configuration.md) | Every setting and where it lives |
| [docs/adobe-integration.md](docs/adobe-integration.md) | How InDesign and Photoshop are driven |
| [docs/architecture.md](docs/architecture.md) | How the code is organised |
| [docs/developer-guide.md](docs/developer-guide.md) | Building, testing and extending |

## Tests

```powershell
python scripts/dev.py test          # everything (Qt runs offscreen)
pytest -m adobe                     # the live Adobe test project
pytest tests/layout                 # the layout guarantees
```

The Adobe tests are skipped automatically when no installation is detected.

## Licence

See [LICENSE.txt](LICENSE.txt). Adobe, InDesign and Photoshop are trademarks
of Adobe Inc.; this application is not affiliated with Adobe and drives the
locally installed applications through their published scripting interfaces.

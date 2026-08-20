# Developer guide

## Setting up

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python scripts/build_builtin_templates.py
```

On Linux or macOS the application runs in planning-only mode (no Adobe), which
is enough for everything except the Adobe integration itself.

## Everyday commands

```powershell
python scripts/dev.py test            # the whole suite, Qt offscreen
python scripts/dev.py test tests/layout -q
python scripts/dev.py check           # ruff and mypy when installed
python scripts/dev.py run             # start the application
python scripts/dev.py diagnose        # print the diagnostics report
python scripts/seed_demo_project.py --generate
```

## Test layout

| Directory | Covers |
|---|---|
| `tests/unit` | Text and RTL handling, errors, events, jobs, undo, versions |
| `tests/layout` | The layout guarantees and the engine internals |
| `tests/ai` | Provider abstraction, the offline analyser, prompts |
| `tests/automation` | Detection, generated JSX, the strategy chain, live Adobe |
| `tests/integration` | Services, the pipeline, the agents, the interface |

Markers: `adobe` (needs a real installation), `gui` (needs Qt), `network`.
The Adobe tests skip themselves when nothing is installed.

The layout tests are the contract: no overlap, nothing outside the page,
margins respected, no overflow, type never below the template minimum,
right-to-left correctness, image resolution, grid alignment and a visible
hierarchy. A change that breaks one of those breaks the product.

## Adding an AI provider

1. Subclass `AIProvider` in `app/ai/`, implementing `generate_text`,
   `analyze_image` and `health_check`.
2. Register it in `TEXT_PROVIDERS` in `app/ai/registry.py`.
3. Add its name to the `ProviderName` literal in `app/config/settings.py` and
   to the picker in `app/ui/pages/settings_pages.py`.

The provider only has to speak its own dialect; retries, timeouts, JSON
extraction and the fallback to the offline analyser are handled above it.

## Adding an image provider

Subclass `ImageGenerationProvider` in `app/ai/images.py` and add it to
`IMAGE_PROVIDERS`. Call `self._finalize(target, generated)` so the real pixel
size is recorded and the provenance sidecar is written.

## Master page tokens

Master-page element text is expanded before the page is composed. The tokens
a template may use:

| Token | Expands to |
| --- | --- |
| `{publication_name}` | The publication's name as entered on the project |
| `{edition_date}` | The edition date written the way that language prints it - a Persian edition gets the Jalali date in Persian digits (`سه‌شنبه ۳۰ مرداد ۱۴۰۳`), other languages get the ISO date |
| `{edition_date_short}` | The same date as numbers only (`۱۴۰۳/۰۵/۳۰`) |
| `{gregorian_date}` | The stored Gregorian date, for a masthead that prints both |
| `{page_number}` | This page's number |
| `{page_count}` | Pages in the edition |
| `{section}` | The section this page belongs to |

An edition date the operator typed as free text rather than a date (a special
issue, say) is passed through untouched.

## Adding a layout strategy

Write a function in `app/layout/strategies.py` with the signature
`(grid, area, blocks, variant) -> list[Region]`, returning one rectangle per
story, and add it to `STRATEGIES`. Finish with `_snap_regions`, which snaps to
the column grid and guarantees the regions cannot overlap. List the strategy
in a template's `layout_rules.allowed_strategies` to make it available.

## Adding an InDesign operation

1. Add the function to `app/adobe/scripts/indesign_lib.jsx` on the `AINS.ID`
   namespace, returning a plain object.
2. Add a method to `InDesignController` that builds a script with
   `ScriptBuilder`, calls it and emits the result.
3. If an agent should be able to use it, register a `Tool` in
   `app/agents/toolset.py` with a schema, a capability and a verifier.

Never talk to the DOM from Python — the controller generates a program and
reads a result, which is what makes every strategy in the chain work.

## Adding a tool for the agent

```python
registry.register(
    Tool(
        name="align_element",
        description="Align a frame to its page.",
        parameters=[
            Parameter("element_id", "string", "Frame id"),
            Parameter("mode", "string", "Alignment",
                      choices=["left", "right", "center", "top", "bottom"]),
        ],
        capability="layout.write",
        handler=lambda element_id, mode: _align(context, element_id, mode),
        verifier=verify_geometry,
    )
)
```

Arguments are validated against the schema, the capability is checked against
the policy, the handler runs, and the verifier confirms the result against the
real state. A tool that cannot be verified is reported as failed.

## Adding a UI page

Subclass `Page` in `app/ui/pages/`, set `title`, `subtitle` and `icon`, build
the widgets in `build()` and reload data in `refresh()`. Add the class to the
list in `MainWindow.__init__`. Wrap actions in `run_guarded` so a failure is
reported rather than raised into the event loop.

## Coding standards

- Type hints on every public function; docstrings on every public symbol.
- No module-level mutable state; dependencies arrive through the container.
- No `except: pass` — catch, log with context and degrade deliberately.
- Secrets never touch `settings.json`, the log or a project archive.
- New behaviour comes with a test; a bug fix comes with the test that catches
  it.

## Building the installer

```powershell
python scripts/build_installer.py            # bundle and installer
python scripts/build_installer.py --bundle   # PyInstaller only
```

The script pre-flights the inputs, runs PyInstaller with
`installer/ai_newspaper_studio.spec` and then Inno Setup with
`installer/setup.iss`, producing
`installer/output/AI-Newspaper-Studio-Setup.exe`. If Inno Setup is not
installed the PyInstaller bundle in `dist/` is still a working application.

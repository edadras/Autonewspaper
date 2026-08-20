# Configuration

## Where settings live

| What | Where | Notes |
|---|---|---|
| Application settings | `%LOCALAPPDATA%\AINewspaperStudio\settings.json` | Written by the Settings pages |
| API keys | Windows Credential Manager | Never written to a file |
| Project settings | `projects/<slug>/project.json` and its database | Travels with the project |
| Templates | `templates/*.template.json` | Shipped and user copies |
| Prompts | `prompts/<group>/<name>.prompt.md` | Versioned, editable |
| Logs | `%LOCALAPPDATA%\AINewspaperStudio\logs` | Rotating, 8 MB × 10 |

Override the data directory with `--data-dir` or the `AINS_DATA_DIR`
environment variable. Every setting can also be set through the environment
with the `AINS_` prefix and `__` between levels, for example
`AINS_AI__PROVIDER=openai`.

If `settings.json` is unreadable it is renamed to `settings.invalid.json` and
the defaults are restored, so a bad edit never stops the application starting.

## settings.json

```json
{
  "ai": {
    "provider": "heuristic",
    "model": "gpt-4o-mini",
    "vision_provider": "heuristic",
    "vision_model": "gpt-4o",
    "temperature": 0.4,
    "max_tokens": 4096,
    "timeout_seconds": 120.0,
    "max_retries": 3,
    "base_url": null
  },
  "image_ai": {
    "provider": "none",
    "model": "gpt-image-1",
    "default_style": "Realistic Editorial Photography",
    "negative_prompt": "no text, no watermark, no logo, no signature, no caption",
    "base_url": null,
    "timeout_seconds": 240.0
  },
  "adobe": {
    "indesign_path": null,
    "photoshop_path": null,
    "prefer_com": true,
    "allow_ui_automation": true,
    "allow_input_automation": false,
    "script_timeout_seconds": 600.0,
    "launch_timeout_seconds": 180.0,
    "close_documents_on_finish": true
  },
  "layout": {
    "candidates_per_page": 6,
    "max_iterations": 5,
    "qa_threshold": 90.0,
    "min_body_font_pt": 7.5,
    "min_image_dpi": 200.0,
    "gutter_mm": 4.0,
    "random_seed": 20240101
  },
  "export": {
    "default_preset": "print",
    "preview_dpi": 110,
    "export_idml": true,
    "export_indd": true,
    "output_dir": null
  },
  "ui": {
    "language": "fa",
    "theme": "dark",
    "undo_steps": 50,
    "autosave_seconds": 120
  },
  "pipeline": {
    "mode": "semi_auto",
    "parallel_workers": 4,
    "agent_max_iterations": 12,
    "agent_timeout_seconds": 900.0,
    "agent_max_retries": 3,
    "stop_on_first_error": false,
    "generate_missing_images": true
  },
  "default_template_id": "broadsheet_fa_standard",
  "log_level": "INFO"
}
```

### The settings that matter most

**`layout.qa_threshold`** — the score a page must reach. Above about 92 most
pages will use all their correction iterations; 88–90 is a practical target.

**`layout.max_iterations`** — how many times a page may be corrected and
re-rendered. Each iteration costs a render, so 3–5 is the useful range. When
the threshold is never reached the best result is kept and reported.

**`layout.candidates_per_page`** — how many compositions are built and scored
per page. More candidates means better pages and a longer run; 6 is a good
balance.

**`adobe.allow_input_automation`** — off by default. Turning it on lets the
application move the mouse and type when no scripting or accessibility path
exists. It affects whatever window is focused, so leave it off unless a
specific operation needs it.

**`pipeline.parallel_workers`** — how much independent work runs at once
(image analysis, generation, processing). Anything touching the shared
InDesign document is serialised regardless of this number.

**`ai.provider`** — `heuristic` needs no key and no network. The cloud
providers need a key in the credential store. If a cloud provider fails
mid-run the offline analyser takes over and the run continues.

## API keys

Keys are stored through `keyring`, which uses the Windows Credential Manager.
Where no credential store exists (a developer machine, a container) the
application falls back to an encrypted local vault bound to the machine —
DPAPI on Windows, an HMAC-derived key elsewhere. Keys are never written to
`settings.json`, never logged and never included in a project archive.

`.env` files and environment variables are read as a last resort so a
developer can export `OPENAI_API_KEY`; they are not the supported production
mechanism.

## Templates

A template is the design system of a publication. See
`templates/broadsheet_fa_standard.template.json` for a complete example.

```json
{
  "id": "broadsheet_fa_standard",
  "page_width_mm": 297.0,
  "page_height_mm": 420.0,
  "margins": {"top": 14.0, "bottom": 16.0, "inside": 14.0, "outside": 12.0},
  "bleed_mm": 3.0,
  "grid": {"columns": 6, "gutter_mm": 4.0, "baseline_mm": 4.4, "rows": 14},
  "paragraph_styles": [
    {"id": "headline", "font_family": "IRANSans", "font_style": "Black",
     "size_pt": 30, "leading_pt": 33, "alignment": "right", "direction": "rtl",
     "min_size_pt": 15, "max_size_pt": 64, "auto_fit": true}
  ],
  "layout_rules": {
    "min_body_size_pt": 7.5,
    "min_image_dpi": 200.0,
    "max_articles_per_page": 6,
    "whitespace_target": 0.13,
    "allowed_strategies": ["hierarchical", "modular", "horizontal", "vertical", "feature"]
  },
  "pdf_presets": [
    {"id": "print", "indesign_preset": "[Press Quality]", "color_space": "CMYK",
     "include_bleed": true, "include_marks": true, "downsample_dpi": 300}
  ]
}
```

Regenerate the shipped templates with:

```powershell
python scripts/build_builtin_templates.py
```

Built-in templates cannot be overwritten. Duplicate one, edit the copy, and
the Templates page will validate it and tell you what is missing.

## Prompts

Prompts live in `prompts/` as Markdown with a front-matter block:

```markdown
---
version: "1.0"
task: editorial_analysis
---

## system
…instructions…

## user
…the request, with {placeholders}…
```

`{name}` is substituted; `{{` and `}}` are literal braces, which is how the
JSON skeletons in the prompts are written. The version of every prompt used
is recorded with the run.

Editing a prompt changes the behaviour of the cloud providers immediately.
The offline analyser is driven by the `task` field rather than the text.

## PDF presets

Each template carries its own presets. `indesign_preset` names an InDesign PDF
export preset by its exact name (`[Press Quality]`, `[High Quality Print]`,
`[Smallest File Size]`, or one of your own). If the preset does not exist in
the installed InDesign, the explicit settings — colour space, bleed, marks,
downsampling — are applied instead, and that is written to the log.

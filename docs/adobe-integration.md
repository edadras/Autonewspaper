# Adobe integration

## The principle

The application is not a mouse macro. Every operation is expressed once, as an
ExtendScript program generated from the layout plan, and sent to InDesign or
Photoshop through their scripting interface. Synthetic input exists only as
the last rung of a ladder and is disabled by default.

```
Python                     ExtendScript                      Adobe
──────                     ───────────                       ─────
layout plan  ──serialise──▶ __plan = {...}      ──DoScript──▶ InDesign DOM
                            AINS.ID.buildDocument(__plan)     builds the pages
result  ◀──parse JSON────── AINS.emit(true, {...})  ◀──────── returns a result
```

## The priority chain

| Priority | Strategy | How it works | When it is used |
|---|---|---|---|
| 1 | `com` | `win32com` dispatches `InDesign.Application` and calls `DoScript` with the program text | Normally |
| 2 | `script-file` | The program is written to disk and evaluated with `$.evalFile` through the same COM call | Long programs, encoding-sensitive payloads |
| 3 | `queue` | A resident script in the Scripts Panel watches a folder for job files and writes result files | COM unavailable or blocked by policy |
| 4 | `ui-automation` | `pywinauto` drives menus and dialogs through the accessibility tree | Operations with no scripting path |
| 5 | `input-automation` | Mouse and keyboard | Only if you enable it |

The bridge tries the strategies in order, records every attempt and reports
which one succeeded. A script that *ran* but reported an error is not retried
through a lower strategy — the same error would come back — so the failure is
returned immediately with its log.

## The result protocol

Every generated program is wrapped in a guard and ends by emitting a JSON
object:

```javascript
{
  "ok": true,
  "data": { "pages": [...], "overflow": [] },
  "error": null,
  "log": ["[0.412s] Created document 297x420 mm, 8 page(s)"],
  "duration": 1.83
}
```

The same JSON is written to a result file, so the queue strategy and a COM
call that returns nothing both work. Python parses it defensively: fenced
code, prose around the object and trailing commas are all handled.

Because ExtendScript has no `JSON` object, a minimal implementation is
prepended to every program (`app/adobe/scripts/json2.jsx`).

## Encoding

Persian text is serialised with `ensure_ascii=True`, so every generated
program is pure ASCII with `\uXXXX` escapes. That removes the whole class of
code-page problems that otherwise appear when a `.jsx` file with Persian text
is read by the ExtendScript engine.

## What the InDesign library does

`app/adobe/scripts/indesign_lib.jsx` is the whole InDesign surface:

- documents: create from the template geometry, open an `.indt`, ensure the
  page count, set per-page margins and columns, apply master pages;
- design system: create colours, paragraph styles, object styles, resolve
  fonts through the template's fallback chain;
- frames: text and picture frames from millimetre rectangles, columns and
  gutters, insets, text wrap, threading a story across frames;
- content: set text, apply paragraph and character styles, place and fit
  images;
- geometry: move, resize, set exact bounds, align to the page, delete,
  bring to front;
- inspection: overflow detection, a per-page report of every item with its
  geometry, effective resolution and link status;
- output: PDF with a preset or explicit settings, PNG/JPEG page previews,
  IDML, save, package for print.

Middle-Eastern features — paragraph direction, Arabic digits, the World-Ready
composer — are applied defensively, so the same program runs on a Western
InDesign build and simply logs that the feature was unavailable.

## What the Photoshop library does

`app/adobe/scripts/photoshop_lib.jsx` covers open, create, resize, crop to an
aspect ratio, tonal adjustments, background removal (Photoshop 2020+),
smart objects, colour-mode conversion and export to JPEG, PNG, TIFF and PSD,
plus a `processImage` recipe that runs the whole "prepare this photo for
print" pass in one call.

When Photoshop is missing, or a feature is not in the installed version, the
controller performs the same work locally with Pillow and reports which engine
ran. Background removal and CMYK conversion genuinely require Photoshop; the
local path says so instead of pretending.

## Detection

Installation paths are never hard-coded. Detection walks: an explicit setting,
the `AINS_INDESIGN_PATH` / `AINS_PHOTOSHOP_PATH` environment variables, the
Adobe registry keys, the Windows uninstall entries, the registered COM
ProgIDs, the conventional `Program Files` locations, and finally `PATH`. The
method that found the application is reported on the Adobe Settings page.

## The pre-flight check

Before a run the service checks that InDesign and Photoshop are present, that
a version was detected, that a script engine is available, that the output
directory is writable and that there is disk space. When InDesign is missing
the check does not fail the run — it records that the built-in renderer will
produce the previews and the PDF.

## Running without Adobe

The application is fully usable without Adobe: the editorial analysis, the
layout engine, the QA loop and the export all work, with the built-in renderer
producing the page images and a multi-page PDF. What you do not get is
InDesign's own composition, colour management, preflight, the `.indd` and the
IDML. Every result that came from the built-in renderer says so.

## Testing against a real installation

```powershell
pytest -m adobe
```

`tests/automation/test_adobe_live.py` is the test project of the
specification: connection, template, text, image, multi-page and PDF export.
It is skipped automatically when no Adobe installation is detected.

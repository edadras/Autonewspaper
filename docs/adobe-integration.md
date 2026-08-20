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

## Which document the edition is built into

When a run reaches InDesign it does not always start from a blank document.
The choice is made *before* the pages are planned, because it can change the
page the layout has to fit, and it is set in **Adobe Settings → Where the
edition is built**:

| Setting | What happens |
| --- | --- |
| `auto` (default) | The document already open in InDesign, if there is one; otherwise the InDesign file the template names; otherwise a new document |
| `open_document` | Only ever the document already open — the run says so plainly when there is none rather than quietly making one |
| `template_file` | Always the `.indt`/`.indd` the template names |
| `new_document` | Always a new, blank document |

### Using the document that is already open

InDesign is asked to describe what it has open — page size, margins, columns,
gutter, bleed, facing pages — without modifying it. A document the operator
set up by hand rarely matches the template to the millimetre, so with
**"Lay the edition out to fit the open document's own page setup"** on (the
default) that geometry replaces the template's for the run: the pages are
planned to the real sheet, and the template's master furniture is scaled to
it, so the masthead still spans the page and the folio still sits at its
foot. Everything that is not geometry — styles, colours, fonts, layout rules,
PDF presets — stays the template's own. The run reports the substitution as a
warning so it is never silent.

With that option off, a document whose page differs from the template is not
used at all and a new one is created, because a plan laid out for one sheet
does not belong on another.

**A document the operator opened is never closed**, whatever
`close_documents_on_finish` says, and never saved on their behalf: the
edition is left in it for them to look at. The same guard sits in the
scripting library itself — both the InDesign and Photoshop libraries record
whether they opened a document or merely found it, and refuse to close one
they found without saving.

### Linking a template to an InDesign file

A template can name an `.indt`/`.indd` that its editions are built into.
Select the template in the Templates page and press **InDesign file…**. The
templates that ship with the application cannot be edited, so linking one
offers to make an editable copy and to switch the project to it.

## Premiere Pro

Premiere is the third host, and it is reached differently from the other two.

**It registers no automation object.** There is no `Premiere.Application` to
dispatch, so the COM path that InDesign and Photoshop use first does not
exist here and is not offered - trying it would only be a connection attempt
that always fails. Premiere's chain is `queue -> ui-automation ->
input-automation`.

**Scripting goes through an extension panel.** Where InDesign and Photoshop
have a Scripts Panel folder to drop the resident runner into, Premiere's
equivalent is a CEP extension. The application ships one; it watches the same
job queue, hands each job to ExtendScript and writes the result back, so the
protocol above is unchanged and only the delivery differs.

```
python scripts/install_premiere_extension.py          # install
python scripts/install_premiere_extension.py --check  # report only
python scripts/install_premiere_extension.py --remove # uninstall
```

The extension is unsigned, so Premiere loads it only once the per-user debug
flag is set. That script is the one place that changes it, it prints what it
changed, and `--remove` puts it back. Afterwards, start Premiere and open
**Window -> Extensions -> AI Newspaper Studio**; the panel has to stay open
for the application to reach Premiere.

### Two APIs, and which is trusted

The public DOM (`app.project`, `app.encoder`) does most of the work:
projects, sequences, importing, placing clips, motion and opacity keyframes,
and export through Media Encoder. It is stable and documented.

A few things exist only on the **QE DOM** - inserting a transition, applying
a named effect, changing a clip's speed. Adobe neither documents nor promises
it. Every QE call is wrapped: it reports what it could not do and leaves the
sequence in a state the operator can finish by hand, rather than half-built.
`session()` reports whether QE is present at all, so an edit that depends on
it can be planned differently before anything is built.

### What is built, and what is checked

`build_edit` sends one script for the whole edit: create the sequence at the
requested frame size and rate, import the footage, lay down the clips,
transitions, effects, transforms and overlays. Each step reports its own
outcome and one failing step does not throw the rest away - a transition
Premiere will not add should not cost the operator the cuts that did work.
`timeline_report` then says what the timeline actually contains, which is
what the quality check measures.

A sequence at an arbitrary frame size needs a preset that matches, and one
may not be installed - so the library writes the preset for the requested
size rather than hoping. When Premiere still produces a different size, the
result says so instead of quietly delivering the wrong frame.

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

# User guide

## The window

The navigation on the left holds thirteen pages. The status bar shows the open
project, running jobs and the progress of a generation run.

| Page | What you do there |
|---|---|
| Dashboard | See the edition at a glance and start a run |
| Projects | Open, duplicate, archive or delete an edition |
| New Project | Create an edition |
| Content | Import copy, set the running order, edit a story |
| Assets | Import pictures, see their measurements, assign and generate |
| Templates | Browse the design systems, validate and duplicate them |
| Layout | Inspect every frame, move it by hand, undo, re-score |
| Preview | Look at the rendered pages and the QA findings |
| Export | Choose the PDF presets and produce the files |
| AI Settings | Choose the provider and store the API keys |
| Adobe Settings | Point at InDesign and Photoshop, set the automation policy |
| Diagnostics | Check everything the application depends on |
| Logs | Follow the live log and the job queue |

## Creating an edition

**New Project** asks for the publication name, the edition date, the language,
the product type, the page size, the page count, the template, the design
style and the AI provider. The project becomes a self-contained folder:

```
projects/morning_post_2026_08_20/
    project.json            the manifest
    database.sqlite         articles, assets, pages, frames, runs
    content/articles/       the source documents you imported
    assets/images/          the pictures you imported
    assets/generated/       AI generated pictures with their provenance
    assets/processed/       derivatives cropped for their frames
    templates/              a project-local copy of the template
    adobe/indesign/         the .indd and .idml
    adobe/scripts/          the generated ExtendScript
    previews/               page images from the QA loop
    output/                 the PDFs, previews and archives
    versions/               snapshots
    logs/                   the project log
```

Copy the folder to another machine and it opens with everything intact.

## Importing content

Drop files onto the Content page, use **Import files…**, or paste from the
clipboard. TXT, Markdown, DOCX, PDF, HTML, JSON and CSV are supported.

A plain-text file can hold several stories separated by a line of dashes, and
each may start with a metadata header:

```
title: زلزله شدید غرب کشور را لرزاند
category: incident
author: سرویس خبر
page: 1
priority: 90

متن خبر …

---

title: نرخ ارز نوسان کرد
category: economy

متن خبر دوم …
```

`title`, `subtitle`, `category`, `author`, `source`, `page` and `priority` are
recognised. Everything after the header is the body.

For DOCX, each Heading 1 starts a new story. For HTML, each `<article>`
element is one story. For JSON and CSV, the columns `title`, `body`,
`category`, `author`, `page` and `priority` are read, and Persian column names
work too.

## Importing pictures

Drop images onto the Assets page. Each picture is measured before it is used:
pixel size, aspect ratio, sharpness, exposure, contrast, colourfulness and
faces, and a perceptual hash that catches re-saved duplicates. The table shows
the resulting quality score and any flags.

Pictures with a measured defect — too few pixels, blurred, badly exposed — are
never chosen for a lead position.

**Assign** links a picture to a story by hand. **Auto-assign** hands the best
remaining picture to the highest-priority story that needs one.

## Generating pictures

If a story needs a picture and has none, the pipeline can generate it. Choose
a provider on the AI Settings page: OpenAI Images, Google Imagen, a local
Stable Diffusion server, or any OpenAI-compatible endpoint.

Every generated file gets a sidecar `*.ai.json` recording that it was AI
generated, with the provider, the model, the prompt and the timestamp.

When generation is **disabled**, the frame is filled with a clearly marked
grey placeholder and QA raises an `image_missing` issue. The application never
pretends a picture was generated.

## Generating the edition

The Dashboard has one button. The mode selector decides how much it asks:

- **auto** — runs the whole pipeline without stopping.
- **semi_auto** — stops for approval before the layout, before InDesign and
  before the export, showing you what it is about to do.
- **manual** — behaves like semi-automatic; use the Layout page between the
  gates to adjust frames by hand.

The stage pills under the progress bar turn blue as each stage starts and
green when it completes. **Stop** cancels; the project is left in a usable
state and the run is recorded as cancelled.

### Working into a document you already have open

If InDesign already has a document open when a run starts, that is where the
edition goes — you do not have to point at a file. Its page size, margins and
columns are read from the document itself and the pages are planned to fit
them, so a sheet you set up by hand is respected rather than overwritten with
the template's measurements. The run says which document it used and what
geometry it took from it.

The document stays open when the run finishes, and is never saved for you:
the edition is placed in it and left for you to look at.

If you would rather it always started from a blank document, or always from
one particular InDesign file, change **Adobe Settings → Where the edition is
built**. To tie a template to a specific `.indt`/`.indd`, select it on the
Templates page and press **InDesign file…**.

Photoshop works differently: it is only used to prepare pictures, one file in
and one file out, so whatever you have open there is left alone.

## Reviewing the result

The Preview page shows each rendered page next to the issues QA found, with
the page's plan score, its QA score and how many correction iterations it
needed. The Layout page shows every frame with its exact geometry and type
size.

To change something by hand: select the frame on the Layout page, edit the
position, size, type size or text, and press **Apply**. The change is checked
before it is applied — a move that would push a frame off the page or over
another one is refused — and it is undoable with Ctrl+Z.

Master-page furniture (the masthead, the folio, the section rules) is locked;
change it in the template instead.

## Exporting

The Export page produces:

| Preset | For |
|---|---|
| print | CMYK with bleed and printer marks |
| high_quality | Archive quality |
| digital | RGB, no bleed |
| web | Small file for e-mail |

Alongside the PDFs it can save the InDesign document, export IDML, write page
previews and zip the whole project. **Package for print** collects the links
and fonts (InDesign only).

## Recovering from a crash

The pipeline records its progress after every stage. If the application is
closed mid-run, the next time you open the project the status bar offers to
resume, and the run picks up from the stage it had reached — the editorial and
layout plans are read back from the project folder.

Before every generation run the project is snapshotted into `versions/`. The
Run menu can also take a snapshot by hand.

## When something goes wrong

Nothing in a run is allowed to stop the application. A failing stage is
recorded, the best result so far is kept and the run continues. The Dashboard
lists the warnings, and the Logs page has the detail.

Run **Diagnostics** first: it checks Python, the packages, SQLite, storage,
permissions, the templates, the fonts, right-to-left shaping, the network, the
Adobe installations, the AI providers and the credential store.

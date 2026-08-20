# Architecture

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│  app/ui            PySide6 pages, theme, event bridge        │
├──────────────────────────────────────────────────────────────┤
│  app/application   the composition root (one object graph)   │
├───────────────┬──────────────┬───────────────┬───────────────┤
│ app/services  │ app/agents   │ app/export    │ app/templates │
│ projects,     │ tool surface,│ PDF presets,  │ design system │
│ content,      │ permissions, │ archives      │               │
│ assets,       │ bounded loop │               │               │
│ diagnostics,  │              │               │               │
│ pipeline      │              │               │               │
├───────────────┼──────────────┼───────────────┴───────────────┤
│ app/layout    │ app/vision   │ app/ai                        │
│ grid,         │ renderer,    │ provider abstraction,         │
│ typography,   │ analyzer,    │ prompts, offline analyser     │
│ constraints,  │ corrector,   │                               │
│ scoring,      │ QA loop      │                               │
│ strategies    │              │                               │
├───────────────┴──────────────┴───────────────────────────────┤
│  app/adobe            app/automation                          │
│  detection, JSX,      priority router, UI automation,         │
│  bridge, controllers  vision matching, input automation       │
├──────────────────────────────────────────────────────────────┤
│  app/core   errors, events, jobs, undo, versioning, DI        │
│  app/models entities and transfer objects                     │
│  app/database sessions and repositories                       │
│  app/config paths, settings, secrets                          │
│  app/utils  text/RTL, units, files, image intelligence        │
└──────────────────────────────────────────────────────────────┘
```

Dependencies point downward. The UI knows about the services; the services
know about the engines; nothing below `app/ui` imports Qt.

## The composition root

`app/application.py` builds every service once and registers it in the
dependency-injection container. The UI, the command line and the tests all
start there, so there is exactly one description of the object graph and no
module-level singletons to work around in tests.

## Threading

- The Qt event loop owns the widgets.
- `JobQueue` runs work on a bounded thread pool with two lanes: `parallel`
  for independent work and `exclusive` for anything touching the shared
  InDesign document, which is serialised regardless of the pool size.
- A generation run happens on a `QThread`; approval requests are marshalled
  back to the GUI thread through a signal and the worker waits for the answer.
- `AIService` owns a private asyncio loop so synchronous callers can await
  provider coroutines without an event loop of their own.
- `EventBridge` re-emits core events as queued Qt signals.

## Data flow of a run

```
articles + assets (SQLite)
        │
        ▼  EditorialPlan          scores, headlines, page assignments
        │
        ▼  ArticleBlock[]         one per story, grouped by page
        │
        ▼  LayoutPlan             pages → elements, millimetre geometry
        │                         persisted to layout_plan.json and the DB
        ├──────────────▶ InDesign: generated JSX builds the document
        │
        ▼  page images            InDesign export, or the built-in renderer
        │
        ▼  QAReport               geometry + pixels + InDesign + vision model
        │
        ▼  CorrectionAction[]     validated, applied, rolled back on regression
        │
        ▼  PDFs, .indd, .idml, previews, archive
```

## Key contracts

`app/models/schemas.py` holds the transfer objects every layer shares:
`Rect`, `TypographySpec`, `ElementSpec`, `PageLayout`, `LayoutPlan`,
`EditorialPlan`, `QAIssue`, `QAReport`, `ImageRequest`, `PipelineResult`.
They are pydantic models with no ORM or Qt dependency, so they serialise to
disk and to a model prompt unchanged.

## Error handling

Every failure becomes an `AppError` subclass carrying its component, its
severity, a recovery action and context. `to_report()` converts any exception
into the same structure. A stage that fails is recorded and the pipeline
continues; a job that fails is captured by the queue; a listener that raises
cannot break the event bus; a widget action that raises is reported in a
dialog instead of killing the window.

## Extending to other products

The layout engine, the template system and the pipeline are not
newspaper-specific. `ProjectSpec.product_type` already accepts magazine,
brochure, catalogue, flyer, poster and digital, and a template declares its
own page size, grid, styles and allowed strategies. Adding a product means
adding a template and, if the composition differs, a strategy in
`app/layout/strategies.py` — the core does not change.

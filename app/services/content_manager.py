"""Content import.

Supports every source format in specification §21 - plain text, Word, PDF,
HTML, JSON, CSV and pasted text - and turns each into
:class:`~app.models.entities.Article` rows. Importers never invent content:
they extract what is in the file and leave the editorial work to the agent.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.errors import ContentImportError
from app.core.events import EventBus, EventType
from app.models import entities as E  # noqa: N812
from app.services.project_manager import ProjectHandle
from app.utils import text as T
from app.utils.files import copy_into, read_json

log = logging.getLogger(__name__)

TEXT_SUFFIXES = {".txt", ".md", ".text"}
DOCX_SUFFIXES = {".docx"}
PDF_SUFFIXES = {".pdf"}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
JSON_SUFFIXES = {".json"}
CSV_SUFFIXES = {".csv", ".tsv"}

SUPPORTED_SUFFIXES = (
    TEXT_SUFFIXES | DOCX_SUFFIXES | PDF_SUFFIXES | HTML_SUFFIXES | JSON_SUFFIXES | CSV_SUFFIXES
)

#: Separator used in plain-text files that contain several stories.
ARTICLE_SEPARATOR = re.compile(r"^\s*(?:-{3,}|={3,}|\*{3,}|#{3,}\s*)\s*$", re.MULTILINE)

#: ``key: value`` metadata lines allowed at the top of a plain-text story.
META_LINE = re.compile(r"^(title|subtitle|category|author|source|page|priority)\s*:\s*(.+)$", re.I)


@dataclass
class ParsedArticle:
    """One story extracted from a source document."""

    title: str = ""
    subtitle: str = ""
    body: str = ""
    category: str = ""
    author: str = ""
    source: str = ""
    page_preference: int | None = None
    priority: int | None = None
    image_required: bool = False
    ai_image_required: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def clean(self) -> ParsedArticle:
        """Drop characters no page can show, whatever the source carried.

        A NUL in a headline terminates the string on the ExtendScript side and
        the rest print as boxes. Every parser's output passes through here, so
        no individual parser has to remember.
        """
        for name in ("title", "subtitle", "body", "category", "author", "source"):
            value = getattr(self, name)
            if value:
                setattr(self, name, T.strip_control(value))
        return self

    def is_empty(self) -> bool:
        """Whether there is nothing worth importing."""
        return not (self.title.strip() or self.body.strip())


@dataclass
class ImportResult:
    """Outcome of importing one or more files."""

    articles: list[int] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Number of imported articles."""
        return len(self.articles)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "articles": self.articles,
            "files": self.files,
            "skipped": [{"file": f, "reason": r} for f, r in self.skipped],
            "warnings": self.warnings,
            "count": self.count,
        }


class ContentManager:
    """Imports source documents into a project."""

    def __init__(self, bus: EventBus | None = None) -> None:
        self.bus = bus

    # ------------------------------------------------------------- parsing
    def parse_file(self, path: Path | str) -> list[ParsedArticle]:
        """Parse a source file into articles (no database involved)."""
        file = Path(path)
        if not file.exists():
            raise ContentImportError(f"File not found: {file}")
        suffix = file.suffix.lower()
        try:
            return [article.clean() for article in self._parse_by_suffix(file, suffix)]
        except ContentImportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ContentImportError(f"Cannot read {file.name}: {exc}", cause=exc) from exc

    def _parse_by_suffix(self, file: Path, suffix: str) -> list[ParsedArticle]:
        """Dispatch to the parser for *suffix*."""
        if suffix in TEXT_SUFFIXES:
            return self.parse_text(file.read_text(encoding="utf-8", errors="replace"), source=file.name)
        if suffix in DOCX_SUFFIXES:
            return self.parse_docx(file)
        if suffix in PDF_SUFFIXES:
            return self.parse_pdf(file)
        if suffix in HTML_SUFFIXES:
            return self.parse_html(file.read_text(encoding="utf-8", errors="replace"), source=file.name)
        if suffix in JSON_SUFFIXES:
            return self.parse_json(read_json(file, []), source=file.name)
        if suffix in CSV_SUFFIXES:
            return self.parse_csv(file)
        raise ContentImportError(
            f"Unsupported file type '{suffix}'",
            context={"supported": sorted(SUPPORTED_SUFFIXES)},
            recovery_action="Convert the file to TXT, DOCX, PDF, HTML, JSON or CSV.",
        )

    def parse_text(self, raw: str, source: str = "") -> list[ParsedArticle]:
        """Parse plain text; ``---`` lines separate several stories."""
        chunks = [c for c in ARTICLE_SEPARATOR.split(raw) if c.strip()]
        parsed = (self._parse_chunk(chunk, source).clean() for chunk in chunks)
        return [a for a in parsed if not a.is_empty()]

    def _parse_chunk(self, chunk: str, source: str) -> ParsedArticle:
        """Parse one story: optional ``key: value`` header, then the body."""
        article = ParsedArticle(source=source)
        lines = chunk.strip().splitlines()
        index = 0
        while index < len(lines):
            match = META_LINE.match(lines[index].strip())
            if not match:
                break
            key, value = match.group(1).lower(), match.group(2).strip()
            if key == "title":
                article.title = value
            elif key == "subtitle":
                article.subtitle = value
            elif key == "category":
                article.category = value
            elif key == "author":
                article.author = value
            elif key == "source":
                article.source = value
            elif key == "page":
                article.page_preference = _int_or_none(value)
            elif key == "priority":
                article.priority = _int_or_none(value)
            index += 1

        remainder = [line for line in lines[index:]]
        while remainder and not remainder[0].strip():
            remainder.pop(0)
        if not article.title and remainder:
            candidate = remainder[0].strip().lstrip("#").strip()
            if len(candidate) <= 140:
                article.title = candidate
                remainder = remainder[1:]
        article.body = "\n".join(remainder).strip()
        return article

    def parse_docx(self, path: Path) -> list[ParsedArticle]:
        """Parse a Word document; each Heading 1/2 starts a new story."""
        try:
            import docx  # type: ignore
        except ImportError as exc:
            raise ContentImportError(
                "python-docx is not installed; cannot read .docx files",
                recovery_action="pip install python-docx, or export the document as text.",
                cause=exc,
            ) from exc
        document = docx.Document(str(path))
        articles: list[ParsedArticle] = []
        current = ParsedArticle(source=path.name)
        body: list[str] = []
        for paragraph in document.paragraphs:
            text = (paragraph.text or "").strip()
            style = (paragraph.style.name if paragraph.style else "") or ""
            if not text:
                body.append("")
                continue
            if style.startswith("Heading 1") or style == "Title":
                if current.title or body:
                    current.body = "\n".join(body).strip()
                    if not current.is_empty():
                        articles.append(current)
                current, body = ParsedArticle(title=text, source=path.name), []
            elif style.startswith("Heading 2") and not current.subtitle:
                current.subtitle = text
            else:
                body.append(text)
        current.body = "\n".join(body).strip()
        if not current.is_empty():
            articles.append(current)
        if not articles:
            raise ContentImportError(f"{path.name} contains no readable text")
        return articles

    def parse_pdf(self, path: Path) -> list[ParsedArticle]:
        """Extract the text of a PDF; one story per document."""
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as exc:
            raise ContentImportError(
                "pypdf is not installed; cannot read PDF files",
                recovery_action="pip install pypdf, or paste the text instead.",
                cause=exc,
            ) from exc
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001
                log.warning("Cannot extract a PDF page from %s: %s", path.name, exc)
        text = "\n\n".join(p.strip() for p in pages if p.strip())
        if not text.strip():
            raise ContentImportError(
                f"{path.name} has no extractable text",
                recovery_action="The PDF is probably a scan; run OCR on it first.",
            )
        articles = self.parse_text(text, source=path.name)
        if articles and not articles[0].title:
            articles[0].title = path.stem
        return articles

    def parse_html(self, raw: str, source: str = "") -> list[ParsedArticle]:
        """Parse an HTML document, preferring ``<article>`` elements."""
        try:
            from bs4 import BeautifulSoup  # type: ignore
        except ImportError as exc:
            raise ContentImportError(
                "beautifulsoup4 is not installed; cannot read HTML",
                recovery_action="pip install beautifulsoup4 lxml.",
                cause=exc,
            ) from exc
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside", "form"]):
            tag.decompose()

        containers = soup.find_all("article") or [soup.body or soup]
        articles: list[ParsedArticle] = []
        for container in containers:
            heading = container.find(["h1", "h2"])
            title = heading.get_text(" ", strip=True) if heading else ""
            deck = container.find(["h2", "h3"]) if heading else None
            subtitle = deck.get_text(" ", strip=True) if deck is not None and deck is not heading else ""
            paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
            body = "\n".join(p for p in paragraphs if p)
            if not (title or body):
                continue
            byline = container.find(attrs={"class": re.compile("author|byline", re.I)})
            articles.append(
                ParsedArticle(
                    title=title or (soup.title.get_text(strip=True) if soup.title else ""),
                    subtitle=subtitle,
                    body=body,
                    author=byline.get_text(" ", strip=True) if byline else "",
                    source=source,
                )
            )
        if not articles:
            raise ContentImportError("No readable article content found in the HTML")
        return articles

    def parse_json(self, payload: Any, source: str = "") -> list[ParsedArticle]:
        """Parse a JSON list (or ``{"articles": [...]}``) of stories."""
        if isinstance(payload, dict):
            payload = payload.get("articles") or payload.get("items") or [payload]
        if not isinstance(payload, list):
            raise ContentImportError("JSON content must be a list of article objects")
        articles: list[ParsedArticle] = []
        for item in payload:
            if isinstance(item, dict):
                articles.append(self._from_mapping(item, source))
            elif isinstance(item, str) and item.strip():
                # A bare list of headlines is a legitimate hand-written file.
                articles.append(ParsedArticle(title=item.strip(), source=source))
        articles = [a for a in articles if not a.is_empty()]
        if not articles:
            raise ContentImportError("The JSON file contains no article objects")
        return articles

    def parse_csv(self, path: Path) -> list[ParsedArticle]:
        """Parse a CSV/TSV file, with or without a header row.

        Spreadsheets exported outside an English locale use semicolons, rows
        are routinely short or long by a field, and a two-column export often
        has no header at all. None of that is a reason to refuse the file.
        """
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if not text.strip():
            raise ContentImportError(f"{path.name} is empty")
        delimiter = self._csv_delimiter(text, path)
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not rows:
            raise ContentImportError(f"{path.name} contains no rows")

        header = [cell.strip() for cell in rows[0]]
        if self._looks_like_header(header):
            body_rows, names = rows[1:], header
        else:
            # Positional: title, body, then the usual columns in order.
            body_rows = rows
            names = ["title", "body", "subtitle", "category", "author", "source"]

        articles: list[ParsedArticle] = []
        for row in body_rows:
            mapping: dict[str, Any] = {}
            for index, cell in enumerate(row):
                # A row longer than the header keeps its extra cells under a
                # generated name rather than a None key the parser cannot use.
                key = names[index] if index < len(names) else f"column_{index + 1}"
                mapping[key or f"column_{index + 1}"] = cell
            articles.append(self._from_mapping(mapping, path.name))
        articles = [a for a in articles if not a.is_empty()]
        if not articles:
            raise ContentImportError(f"{path.name} contains no rows with a title or body")
        return articles

    @staticmethod
    def _csv_delimiter(text: str, path: Path) -> str:
        """Work out which character separates the fields."""
        if path.suffix.lower() == ".tsv":
            return "\t"
        sample = "\n".join(text.splitlines()[:20])
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            # The sniffer gives up on a single column; count instead.
            first = text.splitlines()[0]
            counts = {candidate: first.count(candidate) for candidate in ",;\t|"}
            best = max(counts, key=lambda c: counts[c])
            return best if counts[best] else ","

    @staticmethod
    def _looks_like_header(row: list[str]) -> bool:
        """Whether the first row names the columns rather than carrying data."""
        known = {
            "title",
            "headline",
            "subtitle",
            "deck",
            "body",
            "content",
            "text",
            "category",
            "section",
            "author",
            "byline",
            "source",
            "agency",
            "page",
            "priority",
            "importance",
            "image_required",
            "needs_image",
            "عنوان",
            "تیتر",
            "زیرتیتر",
            "متن",
            "سرویس",
            "نویسنده",
            "منبع",
        }
        cells = [cell.strip().lower() for cell in row if cell.strip()]
        if not cells:
            return False
        return any(cell in known for cell in cells)

    def _from_mapping(self, item: dict[str, Any], source: str) -> ParsedArticle:
        """Build an article from a dictionary with flexible key names."""

        def pick(*names: str, default: str = "") -> str:
            for name in names:
                for key in (name, name.title(), name.upper()):
                    if key in item and item[key] not in (None, ""):
                        return str(item[key]).strip()
            return default

        return ParsedArticle(
            title=pick("title", "headline", "عنوان", "تیتر"),
            subtitle=pick("subtitle", "deck", "زیرتیتر"),
            body=pick("body", "content", "text", "متن"),
            category=pick("category", "section", "سرویس"),
            author=pick("author", "byline", "نویسنده"),
            source=pick("source", "agency", "منبع") or source,
            page_preference=_int_or_none(pick("page", "page_preference")),
            priority=_int_or_none(pick("priority", "importance")),
            image_required=_bool(pick("image_required", "needs_image")),
            ai_image_required=_bool(pick("ai_image", "ai_image_required", "generate_image")),
            meta={k: v for k, v in item.items() if k.lower().startswith("meta_")},
        )

    # ------------------------------------------------------------ importing
    def import_files(
        self,
        handle: ProjectHandle,
        files: Iterable[Path | str],
        *,
        copy_sources: bool = True,
    ) -> ImportResult:
        """Import several files into the project."""
        result = ImportResult()
        parsed: list[ParsedArticle] = []
        for item in files:
            file = Path(item)
            try:
                articles = self.parse_file(file)
            except ContentImportError as exc:
                result.skipped.append((file.name, exc.message))
                log.warning("Skipped %s: %s", file.name, exc.message)
                continue
            if copy_sources:
                try:
                    copy_into(file, handle.content_dir)
                except OSError as exc:  # pragma: no cover
                    result.warnings.append(f"Could not copy {file.name}: {exc}")
            parsed.extend(articles)
            result.files.append(str(file))
        result.articles = self.store(handle, parsed)
        self._emit(EventType.CONTENT_IMPORTED, slug=handle.slug, **result.to_dict())
        return result

    def import_text(self, handle: ProjectHandle, raw: str, *, source: str = "clipboard") -> ImportResult:
        """Import pasted text (specification §21: clipboard support)."""
        result = ImportResult()
        try:
            parsed = self.parse_text(raw, source=source)
        except ContentImportError as exc:
            result.skipped.append((source, exc.message))
            return result
        if not parsed:
            result.skipped.append((source, "no readable content"))
            return result
        result.articles = self.store(handle, parsed)
        result.files.append(source)
        self._emit(EventType.CONTENT_IMPORTED, slug=handle.slug, **result.to_dict())
        return result

    def store(self, handle: ProjectHandle, articles: list[ParsedArticle]) -> list[int]:
        """Persist parsed articles and return their ids."""
        if not articles:
            return []
        ids: list[int] = []
        with handle.uow() as uow:
            order = uow.articles.next_order_index(handle.project_id)
            project = uow.projects.get(handle.project_id)
            default_language = project.language if project else "fa"
            for parsed in articles:
                language = T.detect_language(f"{parsed.title} {parsed.body}") or default_language
                body = T.normalize(parsed.body, language)
                row = E.Article(
                    project_id=handle.project_id,
                    title=T.normalize(parsed.title, language),
                    original_title=parsed.title,
                    subtitle=T.normalize(parsed.subtitle, language),
                    body=body,
                    category=parsed.category or "general",
                    author=parsed.author,
                    source=parsed.source,
                    language=language,
                    word_count=T.word_count(body),
                    order_index=order,
                    page_preference=parsed.page_preference,
                    priority=parsed.priority if parsed.priority is not None else 50,
                    image_required=parsed.image_required,
                    ai_image_required=parsed.ai_image_required,
                    status="imported",
                )
                if parsed.meta:
                    row.set_meta(parsed.meta)
                uow.articles.add(row)
                ids.append(row.id)
                order += 1
        log.info("Imported %d article(s) into '%s'", len(ids), handle.slug)
        return ids

    # ------------------------------------------------------------- editing
    def update_article(self, handle: ProjectHandle, article_id: int, **fields: Any) -> dict[str, Any]:
        """Apply a manual edit to one article (specification §25)."""
        allowed = {
            "title",
            "subtitle",
            "lead",
            "body",
            "summary",
            "category",
            "author",
            "source",
            "importance",
            "urgency",
            "public_interest",
            "visual_importance",
            "priority",
            "page_preference",
            "recommended_page",
            "recommended_area",
            "image_required",
            "ai_image_required",
            "approved",
            "status",
            "order_index",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ContentImportError(f"Unknown article field(s): {', '.join(sorted(unknown))}")
        with handle.uow() as uow:
            row = uow.articles.get(article_id)
            if row is None:
                raise ContentImportError(f"Article {article_id} does not exist")
            for key, value in fields.items():
                setattr(row, key, value)
            if "body" in fields:
                row.word_count = T.word_count(row.body)
            return row.to_dict()

    def snapshot_article(self, handle: ProjectHandle, article_id: int) -> dict[str, Any] | None:
        """Capture a story so a later change to it can be undone."""
        with handle.uow() as uow:
            row = uow.articles.get(article_id)
            return row.to_dict() if row is not None else None

    def restore_article(self, handle: ProjectHandle, payload: dict[str, Any]) -> int:
        """Put a snapshotted story back, re-creating it if it was deleted.

        The original id is kept when it is still free, so anything that refers
        to the story - an assigned picture, a layout frame - still matches.
        """
        columns = {column.name for column in E.Article.__table__.columns}
        data = {
            key: value
            for key, value in payload.items()
            if key in columns and key not in ("created_at", "updated_at")
        }
        data["project_id"] = handle.project_id
        with handle.uow() as uow:
            existing = uow.articles.get(int(data.get("id") or 0))
            if existing is not None:
                for key, value in data.items():
                    if key != "id":
                        setattr(existing, key, value)
                return existing.id
            row = E.Article(**data)
            uow.articles.add(row)
            return row.id

    def snapshot_order(self, handle: ProjectHandle) -> list[int]:
        """Current running order, for undoing a reorder."""
        with handle.uow() as uow:
            return [a.id for a in uow.articles.for_project(handle.project_id)]

    def delete_article(self, handle: ProjectHandle, article_id: int) -> bool:
        """Remove an article from the project."""
        with handle.uow() as uow:
            row = uow.articles.get(article_id)
            if row is None:
                return False
            uow.articles.delete(row)
            return True

    def reorder(self, handle: ProjectHandle, ordered_ids: list[int]) -> int:
        """Apply a new editorial order (drag and drop in the Content page)."""
        with handle.uow() as uow:
            for index, article_id in enumerate(ordered_ids, start=1):
                row = uow.articles.get(article_id)
                if row is not None:
                    row.order_index = index
        return len(ordered_ids)

    def export_json(self, handle: ProjectHandle, target: Path | str) -> Path:
        """Write every article back out as JSON."""
        with handle.uow() as uow:
            rows = uow.articles.for_project(handle.project_id)
            payload = [row.to_dict() for row in rows]
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return destination

    def _emit(self, event: EventType, **payload: Any) -> None:
        if self.bus is not None:
            self.bus.publish(event, **payload)


def _int_or_none(value: Any) -> int | None:
    """Parse an integer, tolerating Persian digits and empty values."""
    if value in (None, ""):
        return None
    try:
        return int(float(T.to_latin_digits(str(value)).strip()))
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    """Parse a truthy string."""
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "بله", "درست"}

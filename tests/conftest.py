"""Shared test fixtures.

Every test runs against a throw-away data directory so nothing touches the
operator's real projects or settings.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.config.paths import AppPaths, set_paths
from app.config.settings import SettingsManager
from app.models.schemas import ProjectSpec

logging.getLogger("PIL").setLevel(logging.WARNING)

PERSIAN_BODY = (
    "زلزله‌ای به بزرگی شش ریشتر بامداد امروز غرب کشور را لرزاند و مردم به خیابان‌ها آمدند. "
    "تیم‌های امداد و نجات بلافاصله به منطقه اعزام شدند و عملیات جست‌وجو ادامه دارد. "
)
ENGLISH_BODY = (
    "Officials said the measure would take effect at the start of the month. "
    "Analysts expect the change to affect several sectors of the economy. "
)


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    """An isolated application data directory."""
    return set_paths(AppPaths.resolve(tmp_path / "data"))


@pytest.fixture
def settings(paths: AppPaths) -> SettingsManager:
    """A settings manager backed by the isolated directory."""
    return SettingsManager(paths)


@pytest.fixture
def application(tmp_path: Path):
    """A fully wired application with an isolated data directory."""
    from app.application import create_application

    app = create_application(tmp_path / "app-data", configure_logging=False)
    yield app
    app.shutdown()


@pytest.fixture
def template():
    """The shipped Persian broadsheet template."""
    from app.templates.schema import TemplateSpec

    root = Path(__file__).resolve().parents[1]
    return TemplateSpec.load(root / "templates" / "broadsheet_fa_standard.template.json")


@pytest.fixture
def english_template():
    """The shipped English magazine template."""
    from app.templates.schema import TemplateSpec

    root = Path(__file__).resolve().parents[1]
    return TemplateSpec.load(root / "templates" / "magazine_en_feature.template.json")


def make_image(path: Path, width: int = 2000, height: int = 1200, color: str = "#3a6ea5") -> Path:
    """Write a detailed test image (flat colours would read as 'blurry')."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 41):
        draw.line([x, 0, x + 120, height], fill="#ffffff", width=3)
    for y in range(0, height, 67):
        draw.line([0, y, width, y + 90], fill="#00000055", width=2)
    image.save(path, quality=94)
    return path


@pytest.fixture
def sample_images(tmp_path: Path) -> list[Path]:
    """Three usable pictures."""
    return [
        make_image(tmp_path / "images" / f"photo{i}.jpg", color=color)
        for i, color in enumerate(["#3a6ea5", "#a56b3a", "#4a7a4a"])
    ]


@pytest.fixture
def sample_articles_file(tmp_path: Path) -> Path:
    """A plain-text file holding five Persian stories."""
    stories = [
        ("زلزله شدید غرب کشور را لرزاند", "incident", PERSIAN_BODY * 14),
        ("نرخ ارز در بازار آزاد نوسان کرد", "economy", PERSIAN_BODY * 12),
        ("پیروزی تیم ملی در بازی حساس", "sport", PERSIAN_BODY * 10),
        ("افتتاح نمایشگاه کتاب تهران", "culture", PERSIAN_BODY * 9),
        ("گزارش وضعیت ترافیک شهری", "society", PERSIAN_BODY * 8),
    ]
    path = tmp_path / "content" / "news.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n\n---\n\n".join(
            f"title: {title}\ncategory: {category}\nauthor: سرویس خبر\n\n{body}"
            for title, category, body in stories
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def project(application, sample_articles_file: Path, sample_images: list[Path]):
    """A project with imported copy and pictures, ready to generate."""
    handle = application.create_project(
        ProjectSpec(
            name="Test Edition",
            publication_name="روزنامه صبح",
            page_count=2,
            language="fa",
            template_id="broadsheet_fa_standard",
        )
    )
    application.content.import_files(handle, [sample_articles_file])
    application.assets.import_files(handle, sample_images)
    return handle


@pytest.fixture
def article_blocks(template):
    """A handful of layout blocks covering every editorial area."""
    from app.layout.engine import make_image_slot
    from app.layout.strategies import ArticleBlock
    from app.models.schemas import AreaKind

    areas = [AreaKind.MAIN, AreaKind.SECONDARY, AreaKind.SMALL, AreaKind.SMALL, AreaKind.SIDEBAR]
    return [
        ArticleBlock(
            article_id=index + 1,
            headline=f"تیتر خبر شماره {index + 1} درباره رویداد مهم امروز",
            subtitle="توضیح کوتاه دوم درباره همین خبر",
            kicker="سیاسی",
            byline="سرویس خبر",
            lead=PERSIAN_BODY,
            body=PERSIAN_BODY * (14 - index * 2),
            area=area,
            priority=95 - index * 12,
            image=(
                make_image_slot(index + 1, "/tmp/does-not-exist.jpg", 2000, 1200, 82.0) if index < 2 else None
            ),
        )
        for index, area in enumerate(areas)
    ]

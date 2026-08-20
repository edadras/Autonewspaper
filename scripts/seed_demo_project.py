"""Create a demonstration project so the application can be tried immediately.

Writes a project with real Persian copy and generated test pictures, then
optionally runs the whole pipeline over it.

Usage:
    python scripts/seed_demo_project.py
    python scripts/seed_demo_project.py --generate --pages 4
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw  # noqa: E402

from app.application import create_application  # noqa: E402
from app.models.schemas import ProjectSpec  # noqa: E402

STORIES = [
    (
        "زلزله شدید غرب کشور را لرزاند",
        "incident",
        "زلزله‌ای به بزرگی شش ریشتر بامداد امروز غرب کشور را لرزاند و مردم وحشت‌زده به "
        "خیابان‌ها آمدند. تیم‌های امداد و نجات بلافاصله به منطقه اعزام شدند و عملیات "
        "جست‌وجو در روستاهای آسیب‌دیده ادامه دارد. ",
    ),
    (
        "نرخ ارز در بازار آزاد نوسان کرد",
        "economy",
        "بانک مرکزی امروز گزارش تازه‌ای از وضعیت بازار ارز منتشر کرد و قیمت دلار در بازار "
        "آزاد نوسان داشت. کارشناسان اقتصادی معتقدند این روند تا پایان فصل ادامه خواهد یافت. ",
    ),
    (
        "پیروزی تیم ملی در بازی حساس",
        "sport",
        "تیم ملی فوتبال در دیدار شب گذشته با نتیجه دو بر یک به پیروزی رسید و بازیکنان "
        "عملکرد خوبی از خود نشان دادند. سرمربی تیم از عملکرد شاگردانش ابراز رضایت کرد. ",
    ),
    (
        "افتتاح نمایشگاه بین‌المللی کتاب تهران",
        "culture",
        "نمایشگاه بین‌المللی کتاب تهران با حضور ناشران داخلی و خارجی افتتاح شد و "
        "بازدیدکنندگان از غرفه‌های مختلف استقبال کردند. ",
    ),
    (
        "گزارش تازه از وضعیت ترافیک شهری",
        "society",
        "شهرداری گزارش تازه‌ای از وضعیت ترافیک منتشر کرد و از اجرای طرح جدید در مناطق پرتردد خبر داد. ",
    ),
    (
        "نشست سران در ژنو برگزار شد",
        "world",
        "نشست سران کشورهای عضو با حضور نمایندگان بلندپایه در ژنو برگزار شد و طرفین بر "
        "ادامه گفت‌وگو تأکید کردند. ",
    ),
]


def write_content(directory: Path) -> Path:
    """Write the demonstration copy as a single import file."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "demo_news.txt"
    chunks = [
        f"title: {title}\ncategory: {category}\nauthor: سرویس خبر\n\n{body * 12}"
        for title, category, body in STORIES
    ]
    path.write_text("\n\n---\n\n".join(chunks), encoding="utf-8")
    return path


def write_images(directory: Path, count: int = 4) -> list[Path]:
    """Generate placeholder press photographs with real detail."""
    directory.mkdir(parents=True, exist_ok=True)
    palette = ["#3a6ea5", "#a56b3a", "#4a7a4a", "#7a4a6a"]
    paths = []
    for index in range(count):
        image = Image.new("RGB", (2400, 1500), palette[index % len(palette)])
        draw = ImageDraw.Draw(image)
        for x in range(0, 2400, 47):
            draw.line([x, 0, x + 260, 1500], fill="#ffffff", width=4)
        for y in range(0, 1500, 83):
            draw.line([0, y, 2400, y + 170], fill="#00000044", width=3)
        path = directory / f"demo_photo_{index + 1}.jpg"
        image.save(path, quality=94)
        paths.append(path)
    return paths


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Create a demonstration project")
    parser.add_argument("--name", default="Demo Edition")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--template", default="broadsheet_fa_standard")
    parser.add_argument("--language", default="fa")
    parser.add_argument("--generate", action="store_true", help="Run the pipeline afterwards")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()

    application = create_application(args.data_dir)
    try:
        handle = application.create_project(
            ProjectSpec(
                name=args.name,
                publication_name="روزنامه صبح" if args.language == "fa" else "The Morning Post",
                page_count=args.pages,
                language=args.language,
                template_id=args.template,
            )
        )
        source = write_content(handle.directory / "content" / "source")
        images = write_images(handle.directory / "assets" / "source")

        content = application.content.import_files(handle, [source])
        assets = application.assets.import_files(handle, images)
        print(f"Created project '{handle.slug}' at {handle.directory}")
        print(f"  {content.count} article(s), {assets.count} image(s)")

        if args.generate:
            result = application.pipeline.run(handle, mode="auto")
            print(f"  {result.summary()}")
            for warning in result.warnings:
                print(f"  warning: {warning}")
            for path in result.pdf_paths:
                print(f"  pdf: {path}")
        else:
            print("  open the application and press 'Generate Newspaper' to lay it out")
        return 0
    finally:
        application.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

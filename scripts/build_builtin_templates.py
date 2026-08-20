"""Generate the templates shipped with the application.

Run with ``python scripts/build_builtin_templates.py``; the JSON files it
writes into ``templates/`` are what the application installs on first start.
Keeping the definitions in code makes them reviewable and reproducible.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.templates.schema import (  # noqa: E402
    CharacterStyleSpec,
    ColorSpec,
    GridSpec,
    LayoutRules,
    MarginSpec,
    MasterElementSpec,
    MasterPageSpec,
    ObjectStyleSpec,
    ParagraphStyleSpec,
    PDFPresetSpec,
    SlotSpec,
    TemplateSpec,
)

OUTPUT = Path(__file__).resolve().parents[1] / "templates"

PRINT_COLORS = [
    ColorSpec(name="Black", cyan=0, magenta=0, yellow=0, black=100),
    ColorSpec(name="Paper", cyan=0, magenta=0, yellow=0, black=0),
    ColorSpec(name="Rule Grey", cyan=0, magenta=0, yellow=0, black=45),
    ColorSpec(name="Section Red", cyan=0, magenta=95, yellow=85, black=5),
    ColorSpec(name="Section Blue", cyan=92, magenta=62, yellow=0, black=8),
    ColorSpec(name="Highlight Amber", cyan=0, magenta=35, yellow=95, black=0),
]

PDF_PRESETS = [
    PDFPresetSpec(
        id="print",
        label="Print (CMYK, bleed and marks)",
        indesign_preset="[Press Quality]",
        color_space="CMYK",
        include_bleed=True,
        include_marks=True,
        downsample_dpi=300,
        compression="zip",
    ),
    PDFPresetSpec(
        id="high_quality",
        label="High quality archive",
        indesign_preset="[High Quality Print]",
        color_space="CMYK",
        include_bleed=True,
        include_marks=False,
        downsample_dpi=350,
        compression="zip",
    ),
    PDFPresetSpec(
        id="digital",
        label="Digital edition (RGB)",
        indesign_preset="[High Quality Print]",
        color_space="RGB",
        include_bleed=False,
        include_marks=False,
        downsample_dpi=180,
        compression="jpeg",
        jpeg_quality="high",
    ),
    PDFPresetSpec(
        id="web",
        label="Web / e-mail (small)",
        indesign_preset="[Smallest File Size]",
        color_space="RGB",
        include_bleed=False,
        include_marks=False,
        downsample_dpi=110,
        compression="jpeg",
        jpeg_quality="medium",
        flatten_transparency=True,
    ),
]


def persian_paragraph_styles(base_body: float = 9.5) -> list[ParagraphStyleSpec]:
    """Persian newspaper text hierarchy (RTL, justified, ZWNJ-aware)."""
    return [
        ParagraphStyleSpec(
            id="masthead", style_name="Masthead", font_family="IRANSans", font_style="Black",
            size_pt=64, leading_pt=64, alignment="center", direction="rtl",
            min_size_pt=36, max_size_pt=96, auto_fit=True, tracking=-12,
        ),
        ParagraphStyleSpec(
            id="kicker", style_name="Kicker", font_family="IRANSans", font_style="Bold",
            size_pt=10, leading_pt=12, alignment="right", direction="rtl",
            color="Section Red", min_size_pt=8, max_size_pt=14, space_after_pt=1.5,
        ),
        ParagraphStyleSpec(
            id="headline", style_name="Headline", font_family="IRANSans", font_style="Black",
            size_pt=30, leading_pt=33, alignment="right", direction="rtl",
            min_size_pt=15, max_size_pt=64, tracking=-8, space_after_pt=2,
        ),
        ParagraphStyleSpec(
            id="subheadline", style_name="Subheadline", font_family="IRANSans", font_style="Medium",
            size_pt=15, leading_pt=19, alignment="right", direction="rtl",
            min_size_pt=10, max_size_pt=26, tracking=-4, space_after_pt=2,
        ),
        ParagraphStyleSpec(
            id="lead", style_name="Lead", font_family="IRANSans", font_style="DemiBold",
            size_pt=11, leading_pt=15, alignment="justify", direction="rtl",
            min_size_pt=9, max_size_pt=14, space_after_pt=2, rule_below_pt=0.3,
        ),
        ParagraphStyleSpec(
            id="body", style_name="Body", font_family="IRANSans", font_style="Regular",
            size_pt=base_body, leading_pt=13.2, alignment="justify", direction="rtl",
            min_size_pt=7.5, max_size_pt=12, hyphenation=False, auto_fit=True,
        ),
        ParagraphStyleSpec(
            id="byline", style_name="Byline", font_family="IRANSans", font_style="Medium",
            size_pt=8.5, leading_pt=11, alignment="right", direction="rtl",
            color="Rule Grey", min_size_pt=7, max_size_pt=10, space_after_pt=1,
        ),
        ParagraphStyleSpec(
            id="caption", style_name="Caption", font_family="IRANSans", font_style="Light",
            size_pt=8, leading_pt=10.5, alignment="right", direction="rtl",
            color="Rule Grey", min_size_pt=7, max_size_pt=10, space_before_pt=1.2,
        ),
        ParagraphStyleSpec(
            id="quote", style_name="Pull Quote", font_family="IRANSans", font_style="Bold",
            size_pt=16, leading_pt=21, alignment="center", direction="rtl",
            min_size_pt=12, max_size_pt=26, color="Section Blue",
            space_before_pt=3, space_after_pt=3, rule_above_pt=0.5, rule_below_pt=0.5,
        ),
        ParagraphStyleSpec(
            id="sidebar", style_name="Sidebar", font_family="IRANSans", font_style="Regular",
            size_pt=8.5, leading_pt=12, alignment="right", direction="rtl",
            min_size_pt=7.5, max_size_pt=10,
        ),
        ParagraphStyleSpec(
            id="folio", style_name="Folio", font_family="IRANSans", font_style="Medium",
            size_pt=8, leading_pt=10, alignment="center", direction="rtl",
            color="Rule Grey", min_size_pt=7, max_size_pt=10,
        ),
    ]


COMMON_CHARACTER_STYLES = [
    CharacterStyleSpec(id="emphasis", font_style="Bold"),
    CharacterStyleSpec(id="dateline", font_style="Medium", color="Rule Grey", all_caps=False),
    CharacterStyleSpec(id="latin_run", font_family="Source Serif Pro", font_style="Regular"),
    CharacterStyleSpec(id="figure", font_style="DemiBold", color="Section Blue"),
]

COMMON_OBJECT_STYLES = [
    ObjectStyleSpec(id="text_frame", inset_mm=1.2, text_wrap_mm=0.0),
    ObjectStyleSpec(id="image_frame", stroke_weight_pt=0.0, text_wrap_mm=3.0),
    ObjectStyleSpec(id="boxed_sidebar", stroke_weight_pt=0.5, stroke_color="Rule Grey",
                    fill_color="Paper", inset_mm=3.0, text_wrap_mm=3.0),
    ObjectStyleSpec(id="section_rule", stroke_weight_pt=1.2, stroke_color="Black"),
    ObjectStyleSpec(id="advertisement", stroke_weight_pt=0.5, stroke_color="Rule Grey", inset_mm=2.0),
]


def broadsheet_fa() -> TemplateSpec:
    """Persian broadsheet, six columns, classic news hierarchy."""
    return TemplateSpec(
        id="broadsheet_fa_standard",
        name="روزنامه استاندارد (Broadsheet - Persian)",
        description=(
            "Six-column Persian broadsheet with a full-width masthead on page one, "
            "section rules, folio lines and a classic news hierarchy."
        ),
        product_type="newspaper",
        language="fa",
        direction="rtl",
        author="AI Newspaper Studio",
        page_width_mm=297.0,
        page_height_mm=420.0,
        margins=MarginSpec(top=14.0, bottom=16.0, inside=14.0, outside=12.0),
        bleed_mm=3.0,
        facing_pages=True,
        grid=GridSpec(columns=6, gutter_mm=4.0, baseline_mm=4.4, rows=14),
        colors=PRINT_COLORS,
        paragraph_styles=persian_paragraph_styles(9.5),
        character_styles=COMMON_CHARACTER_STYLES,
        object_styles=COMMON_OBJECT_STYLES,
        fonts={
            "headline": "IRANSans",
            "body": "IRANSans",
            "caption": "IRANSans",
            "latin": "Source Serif Pro",
        },
        font_fallbacks={
            "IRANSans": ["Vazirmatn", "Sahel", "Tahoma", "Arial"],
            "Source Serif Pro": ["Georgia", "Times New Roman"],
        },
        masthead_height_mm=52.0,
        master_pages=[
            MasterPageSpec(
                name="A-Front",
                applies_to="first",
                elements=[
                    MasterElementSpec(type="masthead", x_mm=14, y_mm=14, width_mm=271, height_mm=38,
                                      style_id="masthead", text="{publication_name}"),
                    MasterElementSpec(type="rule", x_mm=14, y_mm=54, width_mm=271, height_mm=0.8,
                                      style_id="section_rule"),
                    MasterElementSpec(type="folio", x_mm=14, y_mm=406, width_mm=271, height_mm=6,
                                      style_id="folio", text="{publication_name} • {edition_date} • {page_number}"),
                ],
            ),
            MasterPageSpec(
                name="B-Inside",
                applies_to="all",
                elements=[
                    MasterElementSpec(type="kicker", x_mm=14, y_mm=14, width_mm=271, height_mm=7,
                                      style_id="kicker", text="{section}"),
                    MasterElementSpec(type="rule", x_mm=14, y_mm=22, width_mm=271, height_mm=0.5,
                                      style_id="section_rule"),
                    MasterElementSpec(type="folio", x_mm=14, y_mm=406, width_mm=271, height_mm=6,
                                      style_id="folio", text="{page_number} • {publication_name} • {edition_date}"),
                ],
            ),
        ],
        layout_rules=LayoutRules(
            min_body_size_pt=7.5,
            min_image_dpi=200.0,
            max_articles_per_page=6,
            max_images_per_page=4,
            headline_min_size_pt=15.0,
            whitespace_target=0.13,
            slots=[
                SlotSpec(area="main", min_height_ratio=0.32, max_height_ratio=0.62,
                         min_columns=3, max_columns=6, image_probability=0.95),
                SlotSpec(area="secondary", min_height_ratio=0.18, max_height_ratio=0.36,
                         min_columns=2, max_columns=4, image_probability=0.6),
                SlotSpec(area="small", min_height_ratio=0.10, max_height_ratio=0.22,
                         min_columns=1, max_columns=3, image_probability=0.25),
                SlotSpec(area="sidebar", min_height_ratio=0.14, max_height_ratio=0.55,
                         min_columns=1, max_columns=2, image_probability=0.15),
            ],
        ),
        pdf_presets=PDF_PRESETS,
    )


def tabloid_fa() -> TemplateSpec:
    """Persian tabloid, five columns, bolder display type."""
    styles = persian_paragraph_styles(9.8)
    for style in styles:
        if style.id == "headline":
            style.size_pt, style.leading_pt, style.max_size_pt = 26, 29, 54
        if style.id == "masthead":
            style.size_pt, style.leading_pt = 48, 48
    return TemplateSpec(
        id="tabloid_fa_modern",
        name="تابلوید مدرن (Tabloid - Persian)",
        description="Five-column Persian tabloid with large display type and generous white space.",
        product_type="newspaper",
        language="fa",
        direction="rtl",
        author="AI Newspaper Studio",
        page_width_mm=280.0,
        page_height_mm=400.0,
        margins=MarginSpec(top=16.0, bottom=16.0, inside=14.0, outside=14.0),
        bleed_mm=3.0,
        facing_pages=True,
        grid=GridSpec(columns=5, gutter_mm=4.5, baseline_mm=4.6, rows=12),
        colors=PRINT_COLORS,
        paragraph_styles=styles,
        character_styles=COMMON_CHARACTER_STYLES,
        object_styles=COMMON_OBJECT_STYLES,
        fonts={"headline": "IRANSans", "body": "IRANSans", "caption": "IRANSans", "latin": "Inter"},
        font_fallbacks={"IRANSans": ["Vazirmatn", "Sahel", "Tahoma"], "Inter": ["Arial", "Helvetica"]},
        masthead_height_mm=46.0,
        master_pages=[
            MasterPageSpec(
                name="A-Front",
                applies_to="first",
                elements=[
                    MasterElementSpec(type="masthead", x_mm=14, y_mm=16, width_mm=252, height_mm=32,
                                      style_id="masthead", text="{publication_name}"),
                    MasterElementSpec(type="rule", x_mm=14, y_mm=50, width_mm=252, height_mm=1.2,
                                      style_id="section_rule"),
                    MasterElementSpec(type="folio", x_mm=14, y_mm=386, width_mm=252, height_mm=6,
                                      style_id="folio", text="{publication_name} • {page_number}"),
                ],
            ),
            MasterPageSpec(
                name="B-Inside",
                applies_to="all",
                elements=[
                    MasterElementSpec(type="kicker", x_mm=14, y_mm=16, width_mm=252, height_mm=7,
                                      style_id="kicker", text="{section}"),
                    MasterElementSpec(type="folio", x_mm=14, y_mm=386, width_mm=252, height_mm=6,
                                      style_id="folio", text="{page_number}"),
                ],
            ),
        ],
        layout_rules=LayoutRules(
            min_body_size_pt=8.0,
            max_articles_per_page=5,
            headline_min_size_pt=16.0,
            whitespace_target=0.18,
        ),
        pdf_presets=PDF_PRESETS,
    )


def magazine_en() -> TemplateSpec:
    """English A4 feature magazine, three columns, LTR."""
    styles = [
        ParagraphStyleSpec(id="masthead", style_name="Masthead", font_family="Playfair Display",
                           font_style="Bold", size_pt=54, leading_pt=54, alignment="center",
                           direction="ltr", min_size_pt=30, max_size_pt=80),
        ParagraphStyleSpec(id="kicker", style_name="Kicker", font_family="Inter", font_style="SemiBold",
                           size_pt=9, leading_pt=11, alignment="left", direction="ltr",
                           color="Section Blue", all_caps=True, tracking=60, min_size_pt=7, max_size_pt=12),
        ParagraphStyleSpec(id="headline", style_name="Headline", font_family="Playfair Display",
                           font_style="Bold", size_pt=34, leading_pt=36, alignment="left",
                           direction="ltr", min_size_pt=16, max_size_pt=72, tracking=-10),
        ParagraphStyleSpec(id="subheadline", style_name="Deck", font_family="Inter", font_style="Regular",
                           size_pt=14, leading_pt=19, alignment="left", direction="ltr",
                           min_size_pt=10, max_size_pt=22),
        ParagraphStyleSpec(id="lead", style_name="Lead", font_family="Source Serif Pro",
                           font_style="Semibold", size_pt=11.5, leading_pt=16, alignment="justify",
                           direction="ltr", min_size_pt=9, max_size_pt=14, drop_cap_lines=3),
        ParagraphStyleSpec(id="body", style_name="Body", font_family="Source Serif Pro",
                           font_style="Regular", size_pt=10, leading_pt=14, alignment="justify",
                           direction="ltr", hyphenation=True, min_size_pt=8.5, max_size_pt=12),
        ParagraphStyleSpec(id="byline", style_name="Byline", font_family="Inter", font_style="Medium",
                           size_pt=9, leading_pt=12, alignment="left", direction="ltr",
                           color="Rule Grey", min_size_pt=7.5, max_size_pt=11),
        ParagraphStyleSpec(id="caption", style_name="Caption", font_family="Inter", font_style="Regular",
                           size_pt=8, leading_pt=10.5, alignment="left", direction="ltr",
                           color="Rule Grey", min_size_pt=7, max_size_pt=10),
        ParagraphStyleSpec(id="quote", style_name="Pull Quote", font_family="Playfair Display",
                           font_style="Italic", size_pt=20, leading_pt=26, alignment="center",
                           direction="ltr", min_size_pt=14, max_size_pt=32, color="Section Blue"),
        ParagraphStyleSpec(id="sidebar", style_name="Sidebar", font_family="Inter", font_style="Regular",
                           size_pt=9, leading_pt=13, alignment="left", direction="ltr",
                           min_size_pt=8, max_size_pt=11),
        ParagraphStyleSpec(id="folio", style_name="Folio", font_family="Inter", font_style="Regular",
                           size_pt=8, leading_pt=10, alignment="center", direction="ltr",
                           color="Rule Grey", min_size_pt=7, max_size_pt=10),
    ]
    return TemplateSpec(
        id="magazine_en_feature",
        name="Feature Magazine (A4 - English)",
        description="Three-column A4 feature magazine with serif body text and a wide outer column.",
        product_type="magazine",
        language="en",
        direction="ltr",
        author="AI Newspaper Studio",
        page_width_mm=210.0,
        page_height_mm=280.0,
        margins=MarginSpec(top=18.0, bottom=20.0, inside=18.0, outside=15.0),
        bleed_mm=3.0,
        facing_pages=True,
        grid=GridSpec(columns=3, gutter_mm=5.0, baseline_mm=4.9, rows=10),
        colors=PRINT_COLORS,
        paragraph_styles=styles,
        character_styles=COMMON_CHARACTER_STYLES,
        object_styles=COMMON_OBJECT_STYLES,
        fonts={"headline": "Playfair Display", "body": "Source Serif Pro",
               "caption": "Inter", "latin": "Inter"},
        font_fallbacks={
            "Playfair Display": ["Georgia", "Times New Roman"],
            "Source Serif Pro": ["Georgia", "Cambria"],
            "Inter": ["Segoe UI", "Arial"],
        },
        masthead_height_mm=60.0,
        master_pages=[
            MasterPageSpec(
                name="A-Cover",
                applies_to="first",
                elements=[
                    MasterElementSpec(type="masthead", x_mm=18, y_mm=18, width_mm=177, height_mm=40,
                                      style_id="masthead", text="{publication_name}"),
                ],
            ),
            MasterPageSpec(
                name="B-Feature",
                applies_to="all",
                elements=[
                    MasterElementSpec(type="folio", x_mm=18, y_mm=266, width_mm=177, height_mm=6,
                                      style_id="folio", text="{page_number}"),
                ],
            ),
        ],
        layout_rules=LayoutRules(
            min_body_size_pt=8.5,
            max_articles_per_page=3,
            max_images_per_page=3,
            headline_min_size_pt=16.0,
            whitespace_target=0.22,
            image_aspect_choices=["3:2", "4:3", "1:1", "2:3"],
        ),
        pdf_presets=PDF_PRESETS,
    )


def main() -> int:
    """Write every built-in template and report what was produced."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for builder in (broadsheet_fa, tabloid_fa, magazine_en):
        template = builder()
        path = template.save(OUTPUT / f"{template.id}.template.json")
        print(f"wrote {path.relative_to(OUTPUT.parent)}  ({template.summary()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

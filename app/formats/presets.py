"""The named formats the studio ships with.

Sizes are the published ones: ISO 216 for the A series, the ANSI/US sizes for
North America, the standard press formats, and each platform's own
recommendation for the social and video entries. Where a platform reserves
part of the frame for its own interface, the safe area records it as a
fraction of the frame so it survives a change of resolution.
"""

from __future__ import annotations

from app.formats.catalog import Format, Medium, SafeArea, Unit


def _print(
    id: str,
    name: str,
    width: float,
    height: float,
    *,
    bleed: float = 3.0,
    dpi: int = 300,
    aliases: tuple[str, ...] = (),
    notes: str = "",
) -> Format:
    return Format(
        id=id,
        name=name,
        medium=Medium.PRINT,
        width=width,
        height=height,
        unit=Unit.MM,
        dpi=dpi,
        bleed_mm=bleed,
        aliases=aliases,
        notes=notes,
    )


def _screen(
    id: str,
    name: str,
    width: int,
    height: int,
    *,
    medium: Medium = Medium.SOCIAL,
    safe: SafeArea | None = None,
    aliases: tuple[str, ...] = (),
    notes: str = "",
) -> Format:
    return Format(
        id=id,
        name=name,
        medium=medium,
        width=width,
        height=height,
        unit=Unit.PX,
        dpi=72,
        safe_area=safe or SafeArea(),
        aliases=aliases,
        notes=notes,
    )


def _video(
    id: str,
    name: str,
    width: int,
    height: int,
    *,
    fps: float = 25.0,
    duration: float = 15.0,
    safe: SafeArea | None = None,
    aliases: tuple[str, ...] = (),
    notes: str = "",
) -> Format:
    return Format(
        id=id,
        name=name,
        medium=Medium.VIDEO,
        width=width,
        height=height,
        unit=Unit.PX,
        dpi=72,
        fps=fps,
        duration_seconds=duration,
        safe_area=safe or SafeArea(top=0.05, bottom=0.05, left=0.05, right=0.05),
        aliases=aliases,
        notes=notes,
    )


#: Instagram and TikTok put the caption, the handle and the buttons over the
#: bottom of a vertical frame, and the status bar over the top.
VERTICAL_SOCIAL_SAFE = SafeArea(top=0.14, bottom=0.20, left=0.06, right=0.06)

PRINT_FORMATS = [
    # --- ISO 216 ---------------------------------------------------------
    _print("a0", "A0", 841, 1189, aliases=("841x1189",), notes="Large format poster."),
    _print("a1", "A1", 594, 841, notes="Poster."),
    _print("a2", "A2", 420, 594, notes="Poster."),
    _print("a3", "A3", 297, 420, notes="Small poster, menu, presentation board."),
    _print("a4", "A4", 210, 297, aliases=("letter_metric",), notes="Flyer, report, magazine page."),
    _print("a5", "A5", 148, 210, notes="Leaflet, invitation, programme."),
    _print("a6", "A6", 105, 148, notes="Postcard, handbill."),
    _print("b2", "B2", 500, 707, notes="Street poster."),
    # --- North America ---------------------------------------------------
    _print("us_letter", "US Letter", 215.9, 279.4, aliases=("letter",)),
    _print("us_legal", "US Legal", 215.9, 355.6, aliases=("legal",)),
    _print("us_tabloid", "US Tabloid / Ledger", 279.4, 431.8, aliases=("ledger",)),
    # --- press -----------------------------------------------------------
    _print("broadsheet", "Broadsheet", 297, 420, aliases=("broadsheet_a3",), notes="Full-size newspaper."),
    _print("berliner", "Berliner", 315, 470, notes="Midi newspaper format."),
    _print("tabloid_press", "Tabloid (press)", 280, 400, notes="Compact newspaper."),
    # --- covers and stationery -------------------------------------------
    _print("book_cover_a5", "Book cover (A5, front)", 148, 210, bleed=5.0),
    _print("book_cover_royal", "Book cover (Royal, front)", 156, 234, bleed=5.0),
    _print("album_sleeve", "Album sleeve", 305, 305, bleed=3.0, notes="12-inch record sleeve."),
    _print("cd_cover", "CD cover", 120, 120, bleed=2.0),
    _print("business_card", "Business card", 90, 50, bleed=2.0, aliases=("card",)),
    _print("business_card_us", "Business card (US)", 88.9, 50.8, bleed=2.0),
    _print("postcard", "Postcard", 148, 105, bleed=3.0),
    _print("dl_flyer", "DL flyer", 99, 210, bleed=3.0, notes="Fits a DL envelope."),
    _print("trifold_a4", "Tri-fold brochure (A4 landscape)", 297, 210, bleed=3.0),
    _print(
        "roll_up", "Roll-up banner", 850, 2000, bleed=10.0, dpi=150, notes="Large format; 150 dpi is enough."
    ),
    _print(
        "billboard_6x3", "Billboard 6 × 3 m", 6000, 3000, bleed=20.0, dpi=25, notes="Viewed from far away."
    ),
]

SOCIAL_FORMATS = [
    _screen("instagram_post", "Instagram post (square)", 1080, 1080, aliases=("instagram", "ig_post")),
    _screen("instagram_portrait", "Instagram post (portrait)", 1080, 1350, aliases=("ig_portrait",)),
    _screen("instagram_landscape", "Instagram post (landscape)", 1080, 566),
    _screen(
        "instagram_story",
        "Instagram story",
        1080,
        1920,
        safe=VERTICAL_SOCIAL_SAFE,
        aliases=("ig_story", "story"),
        notes="Keep text inside the safe area; the caption and buttons sit over the bottom fifth.",
    ),
    _screen("instagram_reel_cover", "Instagram reel cover", 1080, 1920, safe=VERTICAL_SOCIAL_SAFE),
    _screen("facebook_post", "Facebook post", 1200, 630),
    _screen("facebook_cover", "Facebook cover", 1640, 856, safe=SafeArea(left=0.08, right=0.08)),
    _screen("x_post", "X post", 1600, 900, aliases=("twitter_post",)),
    _screen("linkedin_post", "LinkedIn post", 1200, 1200),
    _screen("linkedin_banner", "LinkedIn banner", 1584, 396, safe=SafeArea(left=0.2, right=0.05)),
    _screen("youtube_thumbnail", "YouTube thumbnail", 1280, 720, aliases=("thumbnail",)),
    _screen(
        "youtube_channel_art",
        "YouTube channel art",
        2560,
        1440,
        safe=SafeArea(top=0.28, bottom=0.28, left=0.24, right=0.24),
        notes="Only the middle is shown on a phone.",
    ),
    _screen("tiktok_cover", "TikTok cover", 1080, 1920, safe=VERTICAL_SOCIAL_SAFE),
    _screen("pinterest_pin", "Pinterest pin", 1000, 1500),
    _screen("telegram_post", "Telegram post", 1280, 1280),
    _screen("whatsapp_status", "WhatsApp status", 1080, 1920, safe=VERTICAL_SOCIAL_SAFE),
    _screen("email_header", "Email header", 1200, 400, medium=Medium.SCREEN),
    _screen("web_hero", "Web hero banner", 1920, 800, medium=Medium.SCREEN),
    _screen("presentation_16_9", "Presentation slide", 1920, 1080, medium=Medium.SCREEN),
]

VIDEO_FORMATS = [
    _video("video_1080p", "1080p (16:9)", 1920, 1080, fps=25, duration=30, aliases=("1080p", "hd")),
    _video("video_1080p_60", "1080p 60 fps", 1920, 1080, fps=60, duration=30),
    _video("video_4k", "4K UHD (16:9)", 3840, 2160, fps=25, duration=30, aliases=("4k", "uhd")),
    _video("video_dci_4k", "DCI 4K", 4096, 2160, fps=24, duration=30),
    _video(
        "video_vertical",
        "Vertical video (9:16)",
        1080,
        1920,
        fps=30,
        duration=30,
        safe=VERTICAL_SOCIAL_SAFE,
        aliases=("reel", "short", "tiktok"),
        notes="Reels, Shorts and TikTok.",
    ),
    _video("video_square", "Square video (1:1)", 1080, 1080, fps=30, duration=30),
    _video("video_720p", "720p", 1280, 720, fps=25, duration=30),
    _video("video_cinema_235", "Cinemascope (2.35:1)", 3840, 1634, fps=24, duration=60),
]

ALL_FORMATS = [*PRINT_FORMATS, *SOCIAL_FORMATS, *VIDEO_FORMATS]

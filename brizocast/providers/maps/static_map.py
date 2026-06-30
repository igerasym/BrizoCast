"""Static map image generator with score overlay — satellite + surf info.

Renders a PNG snapshot of a surf spot location using ESRI World Imagery tiles,
then overlays a score badge and spot info bar using Pillow. The image is
returned as an in-memory ``BytesIO`` buffer ready to send via Telegram.

Attribution: Map tiles © Esri — Source: Esri, DigitalGlobe, GeoEye, etc.
"""

from __future__ import annotations

import io
from typing import Final

__all__ = ["render_spot_map"]

_TILE_URL: Final = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
_MAP_WIDTH: Final = 800
_MAP_HEIGHT: Final = 540
_ZOOM: Final = 16
_MARKER_RADIUS: Final = 12
_MARKER_COLOR: Final = (255, 140, 0)  # orange


def _score_color(score: int | None) -> str:
    """Return a hex color based on score: green/yellow/orange/red."""
    if score is None:
        return "#ffffff"
    if score >= 80:
        return "#2ecc71"  # green
    if score >= 60:
        return "#f1c40f"  # yellow
    if score >= 40:
        return "#e67e22"  # orange
    return "#e74c3c"  # red


def render_spot_map(
    lat: float,
    lon: float,
    *,
    spot_name: str | None = None,
    score: int | None = None,
    wave_text: str | None = None,
) -> io.BytesIO | None:
    """Render a satellite map with score overlay.

    :param lat: Latitude of the surf spot.
    :param lon: Longitude of the surf spot.
    :param spot_name: Spot name for the bottom bar overlay.
    :param score: Surf score (0-100) for the badge color.
    :param wave_text: Short wave description (e.g. "2.1m · 8s · offshore").
    :returns: A ``BytesIO`` PNG buffer, or ``None`` on failure.
    """
    try:
        from staticmap import CircleMarker, StaticMap  # type: ignore[import-untyped]
        from PIL import Image, ImageDraw, ImageFont

        # 1. Render base map
        m = StaticMap(
            _MAP_WIDTH,
            _MAP_HEIGHT,
            url_template=_TILE_URL,
            headers={"User-Agent": "BrizoCast/0.1 (surf-alert bot; personal use)"},
        )
        color = _score_color(score)
        # No marker from staticmap — we draw a custom pin with Pillow below
        m.add_marker(CircleMarker((lon, lat), "#00000000", 1))  # invisible anchor
        image = m.render(zoom=_ZOOM)

        # 2. Draw overlays with Pillow
        draw = ImageDraw.Draw(image, "RGBA")

        # Pulsing ring marker — orange
        cx, cy = _MAP_WIDTH // 2, _MAP_HEIGHT // 2
        mc = _MARKER_COLOR
        for r in range(22, 14, -2):
            alpha = 50 + (22 - r) * 20
            draw.ellipse(
                [(cx - r, cy - r), (cx + r, cy + r)],
                outline=mc + (min(alpha, 180),),
                width=2,
            )
        draw.ellipse(
            [(cx - 12, cy - 12), (cx + 12, cy + 12)],
            fill=None,
            outline="white",
            width=3,
        )
        draw.ellipse(
            [(cx - 6, cy - 6), (cx + 6, cy + 6)],
            fill=mc,
        )

        # Bottom info bar — semi-transparent dark strip
        bar_height = 52
        bar_y = _MAP_HEIGHT - bar_height
        draw.rectangle(
            [(0, bar_y), (_MAP_WIDTH, _MAP_HEIGHT)],
            fill=(0, 0, 0, 180),
        )

        # Try to load a font; fall back to default
        try:
            font_large = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
            font_small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 15)
        except (OSError, IOError):
            font_large = ImageFont.load_default()  # type: ignore[assignment]
            font_small = font_large

        # Spot name in bottom bar
        if spot_name:
            draw.text((12, bar_y + 6), spot_name, fill="white", font=font_large)

        # Wave info in bottom bar (second line)
        if wave_text:
            draw.text((12, bar_y + 28), wave_text, fill=(200, 200, 200), font=font_small)

        # Score badge — top-right corner (semi-transparent black)
        if score is not None:
            badge_w, badge_h = 70, 50
            badge_x = _MAP_WIDTH - badge_w - 12
            badge_y = 12
            draw.rounded_rectangle(
                [(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)],
                radius=8,
                fill=(0, 0, 0, 160),
            )
            score_text = str(score)
            bbox = draw.textbbox((0, 0), score_text, font=font_large)
            tw = bbox[2] - bbox[0]
            draw.text(
                (badge_x + (badge_w - tw) // 2, badge_y + 4),
                score_text,
                fill="white",
                font=font_large,
            )
            # "score" label below the number
            label_bbox = draw.textbbox((0, 0), "score", font=font_small)
            lw = label_bbox[2] - label_bbox[0]
            draw.text(
                (badge_x + (badge_w - lw) // 2, badge_y + 24),
                "score",
                fill=(180, 180, 180),
                font=font_small,
            )

        # 3. Export
        buf = io.BytesIO()
        image.save(buf, format="PNG", quality=90)
        buf.seek(0)
        return buf
    except Exception:  # noqa: BLE001 — graceful degradation
        return None

"""Generate the app images from the original logo file.

Development tool: it needs Pillow and numpy, which the app itself does NOT
need (the app loads the PNG and ICO files produced here directly).

Usage:  python tools/generate_logo_resources.py <original_logo.jpg>

Produces:
  src/reversehire/resources/  icon.ico, icon_<n>.png,
                                 header_dark.png, header_light.png
  docs/images/                   banner.png (for the README)
"""

import sys

for _stream in (sys.stdout, sys.stderr):  # a cp1252 console: never UnicodeEncodeError
    getattr(_stream, "reconfigure", lambda **_k: None)(errors="replace")
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "src" / "reversehire" / "resources"
DOC_IMAGES = ROOT / "docs" / "images"

# Crop boxes (left, top, right, bottom) on the 2688x1520 ReverseHire logo.
ORIGINAL_SIZE = (2688, 1520)
RADAR_BOX = (977, 187, 1699, 909)      # radar scope
TITLE_BOX = (745, 945, 1950, 1105)    # "ReverseHire" lettering
BANNER_BOX = (560, 120, 2128, 1300)    # radar + title + brand, for the README

HEADER_HEIGHT = 56
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


# Backgrounds of the two app themes (must match gui/theme.py).
DARK_BACKGROUND = (0x05, 0x08, 0x06)
LIGHT_BACKGROUND = (0xF4, 0xF7, 0xF5)
# Below SOGLIA_BUIO a pixel is background (grid, binary digits); above
# SOGLIA_PIENO it is a solid stroke.
DARK_THRESHOLD = 0.30
FULL_THRESHOLD = 0.75


def recompose(image: Image.Image, background: tuple[int, int, int], colour_strength: float) -> Image.Image:
    """Redraw the bright strokes of the logo on top of `sfondo` (background).

    The original's brightness acts as ink coverage; the colour is the pixel's
    hue brought to full brightness and multiplied by `forza_colore`
    (1.0 = neon for the dark theme, ~0.5 = dark green readable on the light
    theme).
    """
    data = np.asarray(image.convert("RGB")).astype(np.float32) / 255.0
    maximum = data.max(axis=2, keepdims=True)
    coverage = np.clip((maximum - DARK_THRESHOLD) / (FULL_THRESHOLD - DARK_THRESHOLD), 0.0, 1.0)
    tint = data / np.maximum(maximum, 1e-3) * colour_strength
    bg = np.asarray(background, dtype=np.float32) / 255.0
    result = tint * coverage + bg * (1.0 - coverage)
    return Image.fromarray((np.clip(result, 0.0, 1.0) * 255).astype(np.uint8))


def resize_to_height(image: Image.Image, height: int) -> Image.Image:
    width = round(image.width * height / image.height)
    return image.resize((width, height), Image.LANCZOS)


def compose_header(radar: Image.Image, title: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """Crosshair on the left and lettering on the right, on a strip ALTEZZA_TESTATA high."""
    r = resize_to_height(radar, HEADER_HEIGHT)
    t = resize_to_height(title, HEADER_HEIGHT)
    strip = Image.new("RGB", (r.width + 10 + t.width, HEADER_HEIGHT), background)
    strip.paste(r, (0, 0))
    strip.paste(t, (r.width + 10, 0))
    return strip


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    original = Image.open(sys.argv[1]).convert("RGB")
    if original.size != ORIGINAL_SIZE:
        sys.exit(f"Unexpected size {original.size}: the crop boxes are calibrated for {ORIGINAL_SIZE}.")
    RESOURCES.mkdir(parents=True, exist_ok=True)
    DOC_IMAGES.mkdir(parents=True, exist_ok=True)

    radar = original.crop(RADAR_BOX)
    title = original.crop(TITLE_BOX)

    # Icons: always the dark version, readable on any bar.
    for size in ICON_SIZES:
        radar.resize((size, size), Image.LANCZOS).save(RESOURCES / f"icon_{size}.png")
    radar.resize((256, 256), Image.LANCZOS).save(
        RESOURCES / "icon.ico", sizes=[(m, m) for m in ICON_SIZES]
    )

    # Headers: the same strokes recomposed on each theme's background.
    compose_header(
        recompose(radar, DARK_BACKGROUND, 1.0), recompose(title, DARK_BACKGROUND, 1.0), DARK_BACKGROUND
    ).save(RESOURCES / "header_dark.png")
    compose_header(
        recompose(radar, LIGHT_BACKGROUND, 0.5), recompose(title, LIGHT_BACKGROUND, 0.5), LIGHT_BACKGROUND
    ).save(RESOURCES / "header_light.png")

    banner = original.crop(BANNER_BOX)
    banner.resize((900, round(banner.height * 900 / banner.width)), Image.LANCZOS).save(
        DOC_IMAGES / "banner.png", optimize=True
    )
    for f in sorted(RESOURCES.iterdir()):
        print(f"{f.stat().st_size:>8}  {f.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

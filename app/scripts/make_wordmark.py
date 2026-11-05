#!/usr/bin/env python3
"""Draws Speaka's title the way Caz's and Dopamine's are drawn, as Wordmark.imageset (1x, 2x and 3x).

Dopamine's title (Dopamine.imageset) is a picture of the word in Fonarto Regular, white, with the letter
spacing tightened by 0.65% of the em. Its header shows that picture 40 pt tall. These numbers were measured
from Dopamine's own image; redrawing "Dopamine" with them reproduces it at the same 810 x 160 px size.

This is Caz's scripts/make_wordmark.py with Speaka's defaults. Until the picture exists, the app writes "Speaka"
in SF Rounded Bold in the same place.

Usage:
    python3 app/scripts/make_wordmark.py --font /path/to/FonartoRegular.ttf
    python3 app/scripts/make_wordmark.py --font Fonarto.ttf --text Speaka --out app/Speaka/Assets.xcassets/Wordmark.imageset

Needs Pillow (pip install pillow). Fonarto is by Locomotype, free for personal and commercial use:
https://www.fontspace.com/fonarto-2-font-f104632
"""
import argparse
import json
import os

from PIL import Image, ImageDraw, ImageFont, features

# Measured from Dopamine@3x.png: a 160 px canvas, Fonarto Regular at 174.3 px, the baseline on row 127.
REF_HEIGHT, REF_SIZE, REF_BASELINE = 160, 174.3, 127
TRACKING_EM = -0.0065
SUPERSAMPLE = 8


def draw(font_path, text, height):
    """`text` on a canvas `height` px tall, cropped to the ink left and right, white on transparent."""
    k = height / REF_HEIGHT
    size, baseline = REF_SIZE * k, REF_BASELINE * k
    engine = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
    font = ImageFont.truetype(font_path, size * SUPERSAMPLE, layout_engine=engine)
    track = TRACKING_EM * size * SUPERSAMPLE

    # Pen positions: each glyph's advance, plus the font's kerning for the pair, plus the tracking.
    xs, x = [], 0.0
    for i, ch in enumerate(text):
        xs.append(x)
        x += font.getlength(ch)
        if i + 1 < len(text):
            x += font.getlength(text[i:i + 2]) - font.getlength(text[i]) - font.getlength(text[i + 1])
            x += track

    pad = int(size * SUPERSAMPLE)
    width, canvas_height = int(x + 2 * pad), int(round(height * SUPERSAMPLE))
    mask = Image.new("L", (width, canvas_height), 0)
    pen = ImageDraw.Draw(mask)
    for ch, px in zip(text, xs):
        pen.text((pad + px, baseline * SUPERSAMPLE), ch, font=font, fill=255, anchor="ls")

    left, _, right, _ = mask.getbbox()
    left = (left // SUPERSAMPLE) * SUPERSAMPLE
    right = -(-right // SUPERSAMPLE) * SUPERSAMPLE
    mask = mask.crop((left, 0, right, canvas_height))
    mask = mask.resize(((right - left) // SUPERSAMPLE, int(round(height))), Image.BOX)
    image = Image.new("RGBA", mask.size, (255, 255, 255, 0))
    image.putalpha(mask)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--font", required=True, help="Fonarto Regular (.ttf or .otf)")
    parser.add_argument("--text", default="Speaka")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Speaka",
                                                     "Assets.xcassets", "Wordmark.imageset"))
    parser.add_argument("--points", type=float, default=40, help="height the header shows it at (Dopamine: 40)")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    # The asset catalog itself needs its own Contents.json; Speaka has no catalog until the title is drawn.
    catalog = os.path.dirname(os.path.normpath(args.out))
    if catalog.endswith(".xcassets") and not os.path.exists(os.path.join(catalog, "Contents.json")):
        with open(os.path.join(catalog, "Contents.json"), "w") as handle:
            json.dump({"info": {"author": "xcode", "version": 1}}, handle, indent=2)
    images = []
    for scale, name in [(1, "Wordmark.png"), (2, "Wordmark@2x.png"), (3, "Wordmark@3x.png")]:
        image = draw(args.font, args.text, args.points * scale)
        image.save(os.path.join(args.out, name), optimize=True)
        images.append({"filename": name, "idiom": "universal", "scale": f"{scale}x"})
        print(f"{name}: {image.width} x {image.height} px")
    with open(os.path.join(args.out, "Contents.json"), "w") as handle:
        json.dump({"images": images, "info": {"author": "xcode", "version": 1}}, handle, indent=2)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()

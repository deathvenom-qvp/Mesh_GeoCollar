#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Draws the app's icon (the base station's fence-and-pin, redrawn large) into the Android and
iOS projects. Needs Pillow. Run from the app/ directory: python3 tools/make_icons.py"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

GREEN = (21, 128, 61, 255)
WHITE = (255, 255, 255, 255)
SS = 4  # draw large, then shrink, for smooth edges


def dashed(draw, points, width, dash, gap):
    """A closed dashed outline through `points`."""
    carry, on = 0.0, True
    for a, b in zip(points, points[1:] + points[:1]):
        length = math.dist(a, b)
        at = 0.0
        while at < length:
            step = min((dash if on else gap) - carry, length - at)
            if on:
                p = [a[i] + (b[i] - a[i]) * at / length for i in (0, 1)]
                q = [a[i] + (b[i] - a[i]) * (at + step) / length for i in (0, 1)]
                draw.line([tuple(p), tuple(q)], fill=WHITE, width=width)
                for end in (p, q):
                    draw.ellipse([end[0] - width / 2, end[1] - width / 2, end[0] + width / 2, end[1] + width / 2], fill=WHITE)
            at += step
            carry += step
            if carry >= (dash if on else gap) - 1e-6:
                carry, on = 0.0, not on


def artwork(size, scale=1.0):
    """The fence and the pin, white on transparent, centred in a square of `size` pixels.
    `scale` shrinks it towards the centre (Android's adaptive icons crop the outer third)."""
    n = size * SS
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pt = lambda x, y: (n / 2 + (x - 0.5) * n * scale, n / 2 + (y - 0.5) * n * scale)
    w = max(1, round(n * scale * 0.045))
    fence = [pt(0.5, 0.17), pt(0.83, 0.37), pt(0.83, 0.80), pt(0.17, 0.80), pt(0.17, 0.37)]
    dashed(d, fence, w, n * scale * 0.085, n * scale * 0.075)
    # The pin: a circle with a point below it, and a hole.
    cx, cy = pt(0.5, 0.47)
    r = n * scale * 0.135
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)
    tip = pt(0.5, 0.74)
    spread = math.radians(52)
    d.polygon([(cx - r * math.sin(spread), cy + r * math.cos(spread)), (cx + r * math.sin(spread), cy + r * math.cos(spread)), tip], fill=WHITE)
    h = r * 0.42
    d.ellipse([cx - h, cy - h, cx + h, cy + h], fill=GREEN)
    return im.resize((size, size), Image.LANCZOS)


def plate(size, shape):
    """The green behind the artwork: 'square' (full), 'rounded' or 'circle'."""
    n = size * SS
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if shape == "square":
        d.rectangle([0, 0, n, n], fill=GREEN)
    elif shape == "rounded":
        d.rounded_rectangle([n * 0.04, n * 0.04, n * 0.96, n * 0.96], radius=n * 0.2, fill=GREEN)
    else:
        d.ellipse([n * 0.02, n * 0.02, n * 0.98, n * 0.98], fill=GREEN)
    return im.resize((size, size), Image.LANCZOS)


def icon(size, shape, scale=1.0):
    im = plate(size, shape)
    im.alpha_composite(artwork(size, scale))
    return im


def main():
    res = Path("android/app/src/main/res")
    for name, px in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)):
        folder = res / f"mipmap-{name}"
        icon(px, "rounded", 0.86).save(folder / "ic_launcher.png")
        icon(px, "circle", 0.74).save(folder / "ic_launcher_round.png")
        # Adaptive icon foreground: 108 dp, of which the launcher shows the middle 72.
        artwork(px * 108 // 48, 0.58).save(folder / "ic_launcher_foreground.png")
    assets = Path("ios/App/App/Assets.xcassets")
    icon(1024, "square", 0.92).convert("RGB").save(assets / "AppIcon.appiconset" / "AppIcon-512@2x.png")
    # The launch screen: the same green with the artwork small in the middle.
    splash = Image.new("RGBA", (2732, 2732), GREEN)
    splash.alpha_composite(artwork(512), ((2732 - 512) // 2, (2732 - 512) // 2))
    for name in ("splash-2732x2732.png", "splash-2732x2732-1.png", "splash-2732x2732-2.png"):
        splash.convert("RGB").save(assets / "Splash.imageset" / name, optimize=True)


if __name__ == "__main__":
    main()

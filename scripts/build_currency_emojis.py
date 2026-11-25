"""Render clean, transparent 128px Discord currency emojis from the supplied references.

Run with Pillow installed: python scripts/build_currency_emojis.py
The uploaded reference images remain untouched in data/currency_*.png.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageColor

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data"
S = 4
SIZE = 128


def px(value):
    return int(round(value * S))


def box(coords):
    return tuple(px(v) for v in coords)


def gradient(size, top, bottom, mask=None):
    width, height = size
    top = ImageColor.getrgb(top)
    bottom = ImageColor.getrgb(bottom)
    im = Image.new("RGBA", size)
    pix = im.load()
    denom = max(1, height - 1)
    for y in range(height):
        t = y / denom
        col = tuple(round(a * (1-t) + b * t) for a, b in zip(top, bottom)) + (255,)
        for x in range(width):
            pix[x, y] = col
    if mask is not None:
        im.putalpha(mask)
    return im


def shape_mask(size, draw_shape):
    mask = Image.new("L", size, 0)
    draw_shape(ImageDraw.Draw(mask))
    return mask


def draw_gradient_shape(canvas, shape, top, bottom, stroke=None, stroke_width=1):
    mask = shape_mask(canvas.size, lambda d: shape(d, fill=255))
    canvas.alpha_composite(gradient(canvas.size, top, bottom, mask))
    if stroke:
        shape(ImageDraw.Draw(canvas), fill=None, outline=stroke, width=px(stroke_width))


def coin_icon():
    n = SIZE * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    # A soft shadow keeps the coin legible on both light and dark Discord themes.
    shadow = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse(box((15, 19, 117, 121)), fill=(26, 7, 45, 140))
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(px(4))).resize((n, n)))

    d = ImageDraw.Draw(im)
    # Machined, purple outer edge and a bright magenta coin rim.
    d.ellipse(box((7, 6, 121, 120)), fill="#3D2152", outline="#241632", width=px(2))
    outer = shape_mask(im.size, lambda q: q.ellipse(box((10, 8, 118, 116)), fill=255))
    im.alpha_composite(gradient(im.size, "#FFD1FB", "#A93AC6", outer))
    d = ImageDraw.Draw(im)
    d.ellipse(box((12, 10, 116, 114)), outline="#F7A9F6", width=px(1.4))
    d.ellipse(box((17, 15, 111, 109)), fill="#54205F", outline="#742A84", width=px(3))
    face = shape_mask(im.size, lambda q: q.ellipse(box((20, 18, 108, 106)), fill=255))
    im.alpha_composite(gradient(im.size, "#EC80E5", "#B53BC7", face))
    d = ImageDraw.Draw(im)
    d.ellipse(box((22, 20, 106, 104)), outline="#F7B0F4", width=px(1.3))
    d.arc(box((18, 16, 110, 108)), 200, 326, fill="#FFE0FD", width=px(1.4))
    d.arc(box((20, 18, 108, 106)), 20, 150, fill="#8D2E9E", width=px(1.5))

    # The reference's compact angular A: a solid beveled glyph with a clean counter.
    shadow_poly = [(64, 31), (86, 88), (75, 88), (64, 60), (53, 88), (42, 88)]
    d.polygon([(px(x+1), px(y+2)) for x, y in shadow_poly], fill="#8B2B9D")
    d.polygon([(px(x), px(y)) for x, y in shadow_poly], fill="#FFF8FF")
    d.polygon([tuple(px(v) for v in p) for p in ((52, 72), (76, 72), (79, 80), (49, 80))], fill="#FFF8FF")
    # Small dark inset gives the upper triangle the crisp shape of the supplied mark.
    d.polygon([tuple(px(v) for v in p) for p in ((64, 47), (70, 64), (58, 64))], fill="#C047C9")
    d.line([tuple(px(v) for v in p) for p in ((47, 87), (54, 87))], fill="#FFF8FF", width=px(2))
    return im.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def credits_icon():
    n = SIZE * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon([tuple(px(v) for v in p) for p in ((64, 11), (121, 68), (64, 125), (7, 68))], fill=(4, 25, 54, 150))
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(px(4))))

    def diamond(points, top, bottom, outline=None, width=1):
        shape = lambda d, **kw: d.polygon([tuple(px(v) for v in p) for p in points], **kw)
        draw_gradient_shape(im, shape, top, bottom, outline, width)

    diamond(((64, 5), (123, 64), (64, 123), (5, 64)), "#B6FFFF", "#13B6D5", "#007A9B", 1.4)
    diamond(((64, 12), (115, 64), (64, 116), (12, 64)), "#36F4F6", "#009BC7", "#B8FFFF", 1.2)
    diamond(((64, 20), (107, 64), (64, 108), (21, 64)), "#16C7EB", "#087BB5", "#006C9B", 1.4)
    d = ImageDraw.Draw(im)
    # Gloss facets like the cyan beveled diamond in the reference.
    d.line([tuple(px(v) for v in p) for p in ((64, 15), (104, 56))], fill="#E6FFFF", width=px(1.2))
    d.line([tuple(px(v) for v in p) for p in ((23, 64), (64, 105), (105, 64))], fill="#34E7F7", width=px(1.0))

    # A bold, custom C-plus mark, matching the reference's blocky credits glyph.
    c = [(83, 39), (74, 32), (54, 32), (43, 43), (43, 85), (54, 96),
         (74, 96), (83, 89), (75, 80), (69, 85), (58, 85), (54, 81),
         (54, 47), (58, 43), (69, 43), (75, 48)]
    d.polygon([tuple(px(v) for v in p) for p in c], fill="#045D89")
    d.polygon([tuple(px(v) for v in p) for p in [(x, y-1) for x, y in c]], fill="#F4FFFF")
    # Crossbar extends right from the C, a distinctive credit/currency cue.
    d.rounded_rectangle(box((52, 59, 86, 68)), radius=px(3), fill="#F4FFFF")
    # Tiny bevel facets on the mark prevent it from reading as a generic letter.
    d.line([tuple(px(v) for v in p) for p in ((57, 45), (68, 45), (74, 49))], fill="#D3FFFF", width=px(1.2))
    return im.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def _blueprint_sheet(width=72, height=86, trim="#1688C4", variant=0):
    w, h = px(width), px(height)
    sheet = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(sheet)
    d.rounded_rectangle((px(1), px(1), w-px(1), h-px(1)), radius=px(4), fill="#F8FCFF", outline="#183B67", width=px(1.6))
    d.rounded_rectangle((px(4), px(4), w-px(4), h-px(4)), radius=px(2.7), outline=trim, width=px(1.2))
    d.rounded_rectangle((px(6), px(6), w-px(6), px(17)), radius=px(2.2), fill=trim)
    # Header plate and small registration dots.
    d.ellipse(box((8, 9, 11, 12)), fill="#FFFFFF")
    d.line([tuple(px(v) for v in p) for p in ((15, 9), (34, 9))], fill="#FFFFFF", width=px(1.4))
    d.line([tuple(px(v) for v in p) for p in ((15, 13), (28, 13))], fill="#C8F6FF", width=px(1.0))

    # Technical grid and little colored blueprint panels.
    for x in range(10, int(width)-8, 8):
        d.line([tuple(px(v) for v in p) for p in ((x, 21), (x, height-7))], fill="#D5E8F4", width=px(.45))
    for y in range(24, int(height)-5, 8):
        d.line([tuple(px(v) for v in p) for p in ((7, y), (width-7, y))], fill="#D5E8F4", width=px(.45))
    d.rounded_rectangle(box((10, 23, 30, 42)), radius=px(1.5), fill="#D8F6FF", outline="#16A5D2", width=px(.9))
    d.polygon([tuple(px(v) for v in p) for p in ((13, 37), (19, 27), (27, 37))], outline="#148DBA", fill="#7DE6EF")
    d.ellipse(box((17, 30, 22, 35)), fill="#F8FBFF", outline="#087CA8", width=px(.8))
    d.rounded_rectangle(box((35, 23, width-9, 42)), radius=px(1.2), fill="#F1F8FF", outline="#7FA9C2", width=px(.7))
    d.line([tuple(px(v) for v in p) for p in ((38, 27), (width-13, 27))], fill="#2689B4", width=px(1.0))
    d.line([tuple(px(v) for v in p) for p in ((38, 32), (width-15, 32))], fill="#A8C9D8", width=px(.8))
    d.line([tuple(px(v) for v in p) for p in ((38, 37), (width-19, 37))], fill="#A8C9D8", width=px(.8))
    colors = ("#36BCD5", "#71CC6A", "#F1B443", "#9B70D1")
    for i, x in enumerate((11, 23, 35, 47)):
        if x + 8 > width-7: continue
        d.rounded_rectangle(box((x, 49, x+7, 56)), radius=px(1), fill=colors[(i+variant) % len(colors)])
    d.line([tuple(px(v) for v in p) for p in ((10, 62), (width-10, 62))], fill="#567F9E", width=px(.9))
    d.line([tuple(px(v) for v in p) for p in ((10, 68), (width-17, 68))], fill="#A3C0D0", width=px(.7))
    # Corner registration mark.
    d.arc(box((width-17, height-17, width-8, height-8)), 180, 90, fill=trim, width=px(1.1))
    d.ellipse(box((width-14, height-14, width-11, height-11)), fill=trim)
    return sheet


def blueprint_icon():
    n = SIZE * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    def composite_sheet(center, dims, angle, trim, variant):
        width, height = dims
        raw = _blueprint_sheet(width, height, trim, variant)
        # Build a soft alpha shadow, rotate it with the paper, then place the paper.
        alpha = raw.getchannel("A")
        shadow = Image.new("RGBA", raw.size, (7, 23, 49, 0))
        shadow.putalpha(alpha.point(lambda x: int(x * .44)).filter(ImageFilter.GaussianBlur(px(1.5))))
        shadow = shadow.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
        rotated = raw.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
        left = px(center[0]) - rotated.width // 2
        top = px(center[1]) - rotated.height // 2
        shadow_canvas = Image.new("RGBA", im.size, (0, 0, 0, 0))
        shadow_canvas.alpha_composite(shadow, (left+px(1), top+px(2)))
        im.alpha_composite(shadow_canvas.filter(ImageFilter.GaussianBlur(px(1.2))))
        im.alpha_composite(rotated, (left, top))

    # Colored sheets peek from behind the main plan, like the supplied card-stack icon.
    composite_sheet((75, 48), (70, 82), -14, "#58BD66", 1)
    composite_sheet((48, 48), (68, 82), 12, "#8E65C5", 2)
    composite_sheet((62, 67), (72, 88), -5, "#159BC7", 0)

    # Draw a small, clear white drafting compass/wrench over the plan.
    d = ImageDraw.Draw(im)
    # Dark blue outline/shadow for contrast on the miniature page.
    p1, p2 = (52, 81), (80, 43)
    d.line([tuple(px(v) for v in p) for p in (p1, p2)], fill="#123D65", width=px(8))
    d.line([tuple(px(v) for v in p) for p in (p1, p2)], fill="#F9FFFF", width=px(4.8))
    # Drafting head and splayed legs, a compact tool mark rather than text.
    d.ellipse(box((75, 38, 85, 48)), fill="#F9FFFF", outline="#123D65", width=px(1.3))
    d.ellipse(box((78, 41, 82, 45)), fill="#45C9D2")
    d.line([tuple(px(v) for v in p) for p in ((51, 79), (45, 84))], fill="#F9FFFF", width=px(4.5))
    d.line([tuple(px(v) for v in p) for p in ((53, 82), (55, 89))], fill="#F9FFFF", width=px(4.5))
    d.line([tuple(px(v) for v in p) for p in ((45, 84), (55, 89))], fill="#123D65", width=px(1.1))
    d.ellipse(box((48, 78, 56, 86)), fill="#F6C14D", outline="#123D65", width=px(1.1))
    d.ellipse(box((50, 80, 54, 84)), fill="#FFF8E8")
    return im.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def save(name, image):
    path = OUT / name
    image.save(path, format="PNG", optimize=True)
    if image.mode != "RGBA" or image.size != (128, 128) or path.stat().st_size > 256 * 1024:
        raise RuntimeError(f"Invalid emoji asset: {path}")
    print(f"{path.relative_to(ROOT)}  {path.stat().st_size:,} bytes")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    save("emoji_a_coins.png", coin_icon())
    save("emoji_credits.png", credits_icon())
    save("emoji_blueprints.png", blueprint_icon())

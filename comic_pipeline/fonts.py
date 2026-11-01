"""Pillow-compatible font fallback using actual Unicode cmap coverage."""
from functools import lru_cache
import hashlib
import math
import os
from pathlib import Path
import unicodedata

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont


@lru_cache(64)
def coverage(path, index=0):
    with TTFont(str(path), fontNumber=index, lazy=True) as font:
        return frozenset(code for code, name in (font.getBestCmap() or {}).items() if name != ".notdef")


@lru_cache(1)
def fallback_paths():
    windows = Path(os.getenv("WINDIR", "C:/Windows"))/"Fonts"
    candidates = [windows/"NotoSansTC-VF.ttf", windows/"seguisym.ttf", windows/"mingliub.ttc",
                  windows/"seguiemj.ttf", windows/"mingliu.ttc",
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                  Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    return tuple(str(path) for path in candidates if path.is_file())


@lru_cache(256)
def plain_font(path, size, index=0):
    return ImageFont.truetype(str(path), size, index=index)


@lru_cache(4096)
def font_for(char, primary, index=0):
    # Formatting and variation selectors must stay attached to their preceding
    # glyph. They are not visible characters requiring a standalone font.
    if char.isspace() or unicodedata.category(char) in {"Cf", "Mn", "Me"}:
        return primary, index
    for path, face in [(primary, index)]+[(p, 0) for p in fallback_paths() if p != primary]:
        if ord(char) in coverage(path, face):
            return path, face
    return primary, index


class FallbackFont(ImageFont.FreeTypeFont):
    """Keep the primary font's pixels; use measured fallback runs for missing glyphs.

    The renderer uses left-to-right runs and Pillow's normal horizontal anchors;
    manga vertical layout positions each character itself.
    """
    def __init__(self, path, size, index=0):
        super().__init__(str(path), size, index=index)
        self.primary = str(path)
        self.primary_index = index

    def _runs(self, text):
        if not isinstance(text, str):
            return [(text, plain_font(self.primary, self.size, self.primary_index))]
        runs = []
        for char in text:
            selected = font_for(char, self.primary, self.primary_index)
            if unicodedata.category(char) in {"Cf", "Mn", "Me"} and runs:
                selected = runs[-1][1]
            if runs and runs[-1][1] == selected:
                runs[-1] = (runs[-1][0]+char, selected)
            else:
                runs.append((char, selected))
        return [(part, plain_font(path, self.size, face)) for part, (path, face) in runs]

    def _uses_fallback(self, text):
        return isinstance(text, str) and any(font_for(c, self.primary, self.primary_index) !=
                                           (self.primary, self.primary_index) for c in text)

    def _layout(self, text, mode, direction, features, language, stroke_width, anchor):
        if direction not in {None, "ltr"}:
            raise ValueError("Mixed-font rendering currently supports left-to-right runs only")
        runs, offset, boxes = [], 0., []
        ascent = self.getmetrics()[0]
        for part, font in self._runs(text):
            box = font.getbbox(part, mode, direction, features, language, stroke_width, "ls")
            boxes.append((offset+box[0], ascent+box[1], offset+box[2], ascent+box[3]))
            runs.append((part, font, offset))
            offset += font.getlength(part, mode, direction, features, language)
        bounds = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                  max(b[2] for b in boxes), max(b[3] for b in boxes)]
        anchor = anchor or "la"
        dx = {"l": 0, "m": -offset/2, "r": -offset}[anchor[0]]
        if anchor[1] == "t":
            dy = -bounds[1]
        elif anchor[1] == "b":
            dy = -bounds[3]
        else:
            base = super().getbbox("A", anchor="la")
            aligned = super().getbbox("A", anchor="l"+anchor[1])
            dy = aligned[1]-base[1]
        return (math.floor(bounds[0]+dx), math.floor(bounds[1]+dy),
                math.ceil(bounds[2]+dx), math.ceil(bounds[3]+dy)), runs, (dx, ascent+dy), offset

    def getlength(self, text, mode="", direction=None, features=None, language=None):
        if not self._uses_fallback(text):
            return super().getlength(text, mode, direction, features, language)
        return sum(font.getlength(part, mode, direction, features, language) for part, font in self._runs(text))

    def getbbox(self, text, mode="", direction=None, features=None, language=None, stroke_width=0, anchor=None):
        if not self._uses_fallback(text):
            return super().getbbox(text, mode, direction, features, language, stroke_width, anchor)
        return self._layout(text, mode, direction, features, language, stroke_width, anchor)[0]

    def getmask2(self, text, mode="", direction=None, features=None, language=None, stroke_width=0,
                 anchor=None, ink=0, start=None, *args, **kwargs):
        if not self._uses_fallback(text):
            return super().getmask2(text, mode, direction, features, language, stroke_width, anchor, ink, start,
                                    *args, **kwargs)
        bounds, runs, (dx, baseline), _ = self._layout(text, mode, direction, features, language, stroke_width, anchor)
        mask = Image.new("L", (max(1, bounds[2]-bounds[0]), max(1, bounds[3]-bounds[1])))
        pen = ImageDraw.Draw(mask)
        fractional = start or (0, 0)
        for part, font, offset in runs:
            pen.text((dx+offset-bounds[0]+fractional[0], baseline-bounds[1]+fractional[1]), part,
                     font=font, anchor="ls", fill=255, stroke_width=stroke_width, stroke_fill=255,
                     direction=direction, features=features, language=language)
        return mask.im, bounds[:2]


@lru_cache(128)
def load_font(path, size):
    return FallbackFont(str(path), size)


def fallback_signature():
    return dict(code=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                fonts=[(p, Path(p).stat().st_size, Path(p).stat().st_mtime_ns) for p in fallback_paths()])

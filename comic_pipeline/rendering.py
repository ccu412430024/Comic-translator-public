"""Shape-aware Chinese typesetting with contrast-aware fill and outline."""
from pathlib import Path
from copy import deepcopy
import math
import os

import mahotas
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from .fonts import load_font


# Display substitutions only: stored translations and horizontal text stay intact.
VERTICAL_DASHES = "—―–"
VERTICAL_FORMS = str.maketrans("，。！？：；「」『』（）、…—―–", "︐︒︕︖︓︔﹁﹂﹃﹄︵︶︑︙︱︱︲")


def default_font():
    candidates = [Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts/msjh.ttc",
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("No CJK font found; specify --font with a Traditional Chinese font")


def wrap_text(draw, text, font, width):
    lines = []
    line = ""
    closing = "，。！？、：；）」』】》,.!?;:"
    for char in text:
        if char == "\n":
            lines.append(line)
            line = ""
        elif line and draw.textlength(line + char, font=font) > width:
            if char in closing and len(line) > 1:
                previous = line[-1]
                lines.append(line[:-1])
                line = previous + char
            else:
                lines.append(line)
                line = char
        else:
            line += char
    if line:
        lines.append(line)
    return lines or [""]


def fit_text(draw, text, font_path, box, stroke_width=0):
    x1, y1, x2, y2 = box
    width, height = max(1, x2-x1), max(1, y2-y1)
    best = None
    for size in range(max(6, min(64, height)), 5, -1):
        font = load_font(str(font_path), size)
        lines = wrap_text(draw, text, font, width)
        value = "\n".join(lines)
        bounds = draw.multiline_textbbox((0, 0), value, font=font, spacing=2,
                                         align="center", stroke_width=stroke_width)
        best = (font, value, bounds)
        if bounds[2]-bounds[0] <= width and bounds[3]-bounds[1] <= height:
            return best, False
    return best, True


def largest_rectangle(mask):
    """Largest axis-aligned rectangle containing only safe pixels; x2/y2 exclusive."""
    height, width = mask.shape
    histogram = [0] * width
    best = (0, [0, 0, 0, 0])
    for y in range(height):
        histogram = [histogram[x]+1 if mask[y, x] else 0 for x in range(width)]
        stack = []
        for x in range(width+1):
            value = histogram[x] if x < width else 0
            start = x
            while stack and stack[-1][1] > value:
                left, bar = stack.pop()
                area = (x-left)*bar
                if area > best[0]:
                    best = (area, [left, y-bar+1, x, y+1])
                start = left
            if value and (not stack or stack[-1][1] < value):
                stack.append((start, value))
    return best[1]


def _clamp(box, width, height):
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    return [max(0, min(width-1, x1)), max(0, min(height-1, y1)),
            max(1, min(width, x2)), max(1, min(height, y2))]


def _free_text_region(cleaned, box, fallback="text-box"):
    """Avoid dark artwork only when a large, uniform background is measurable."""
    x1, y1, x2, y2 = box
    roi = cleaned[y1:y2, x1:x2]
    if roi.size == 0:
        return box, fallback, None
    quantized = roi.astype(np.int32)//24
    codes = quantized[:, :, 0]*121 + quantized[:, :, 1]*11 + quantized[:, :, 2]
    values, counts = np.unique(codes, return_counts=True)
    dominant = values[int(np.argmax(counts))]
    background = np.median(roi[codes == dominant], axis=0)
    mask = np.linalg.norm(roi.astype(float)-background, axis=2) < 40
    if mask.mean() < .72:
        return box, fallback, None
    local = largest_rectangle(mask)
    area = (local[2]-local[0])*(local[3]-local[1])
    if area < roi.shape[0]*roi.shape[1]*.40 or min(local[2]-local[0], local[3]-local[1]) < 12:
        return box, fallback, None
    return [local[0]+x1, local[1]+y1, local[2]+x1, local[3]+y1], "uniform-background", None


def fill_small_holes(region):
    """Ignore small enclosed glyph remnants while retaining larger artwork holes."""
    labels, count = mahotas.label(~region)
    sizes = np.bincount(labels.ravel(), minlength=count+1)
    boundary = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    allowed = (sizes <= max(12, int(region.size*.015)))
    allowed[boundary] = False
    allowed[0] = False
    return region | allowed[labels]


def choose_region(cleaned, block, neighbors=()):
    height, width = cleaned.shape[:2]
    if block.get("layout_bbox"):
        return _clamp(block["layout_bbox"], width, height), "gemini-region", None
    box = _clamp(block["bbox"], width, height)
    bubble = block.get("bubble_bbox")
    if not bubble:
        return _free_text_region(cleaned, box)
    bubble = _clamp(bubble, width, height)
    bx1, by1, bx2, by2 = bubble
    intersection = max(0, min(box[2], bx2)-max(box[0], bx1)) * max(0, min(box[3], by2)-max(box[1], by1))
    if intersection < (box[2]-box[0])*(box[3]-box[1])*0.65:
        return _free_text_region(cleaned, box, "untrusted-bubble")
    roi = cleaned[by1:by2, bx1:bx2]
    if min(roi.shape[:2]) < 10:
        return box, "small-bubble", None
    # Find a dominant, connected, near-uniform background on the cleaned image.
    quantized = roi.astype(np.int32)//24
    codes = quantized[:, :, 0]*121 + quantized[:, :, 1]*11 + quantized[:, :, 2]
    values, counts = np.unique(codes, return_counts=True)
    dominant = values[int(np.argmax(counts))]
    background = np.median(roi[codes == dominant], axis=0)
    candidates = np.linalg.norm(roi.astype(float)-background, axis=2) < 40
    labels, count = mahotas.label(candidates)
    sx1, sy1 = max(0, box[0]-bx1), max(0, box[1]-by1)
    sx2, sy2 = min(roi.shape[1], box[2]-bx1), min(roi.shape[0], box[3]-by1)
    seed = labels[sy1:sy2, sx1:sx2]
    component_counts = np.bincount(seed.ravel(), minlength=count+1)
    component_counts[0] = 0
    region = labels == int(np.argmax(component_counts)) if component_counts.any() else np.zeros(candidates.shape, bool)
    border = np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]])
    coverage = float(region.mean())
    method = "bubble-component"
    if not (0.25 < coverage < 0.94 and border.mean() < 0.45):
        # An ellipse is a conservative fallback only for a largely uniform bubble.
        # Textured title/scene boxes must not be treated as speech balloons.
        if candidates.mean() < 0.65:
            return _free_text_region(cleaned, box, "untrusted-bubble")
        yy, xx = np.indices(region.shape)
        cx, cy = (roi.shape[1]-1)/2, (roi.shape[0]-1)/2
        region = ((xx-cx)/max(1, cx))**2 + ((yy-cy)/max(1, cy))**2 < 0.88**2
        method = "bubble-ellipse-fallback"
    else:
        region = fill_small_holes(region)
        region = mahotas.erode(region, np.ones((3, 3), bool))
        region = mahotas.erode(region, np.ones((3, 3), bool))
    if neighbors:
        # Adjacent dialogue may share a connected balloon. Give each source text
        # its own nearest-centre partition so translations cannot collide.
        yy, xx = np.indices(region.shape)
        cx, cy = (box[0]+box[2])/2, (box[1]+box[3])/2
        own = (xx+bx1-cx)**2+(yy+by1-cy)**2
        for other in neighbors:
            if min(bx2, other[2]) <= max(bx1, other[0]) or min(by2, other[3]) <= max(by1, other[1]):
                continue
            ox, oy = (other[0]+other[2])/2, (other[1]+other[3])/2
            region &= own <= (xx+bx1-ox)**2+(yy+by1-oy)**2
    local = largest_rectangle(region)
    if local[2]-local[0] < 6 or local[3]-local[1] < 6:
        return box, "small-bubble", None
    return [local[0]+bx1, local[1]+by1, local[2]+bx1, local[3]+by1], method, (bubble, region)


def contrast_style(cleaned, box):
    x1, y1, x2, y2 = box
    sample = cleaned[y1:y2, x1:x2].astype(float)/255
    if sample.size == 0:
        return (0, 0, 0), (255, 255, 255), 0.0
    linear = np.where(sample <= .04045, sample/12.92, ((sample+.055)/1.055)**2.4)
    luminance = linear @ np.array([.2126, .7152, .0722])
    black = float(np.median((luminance+.05)/.05))
    white = float(np.median(1.05/(luminance+.05)))
    foreground = (0, 0, 0) if black >= white else (255, 255, 255)
    outline = (255, 255, 255) if foreground[0] == 0 else (0, 0, 0)
    return foreground, outline, round(max(black, white), 2)


def _vertical_columns(text, capacity):
    columns = []
    column = ""
    closing = "，。！？、：；）」』】》,.!?;:"
    for char in text:
        if char == "\n":
            if column:
                columns.append(column)
            column = ""
        elif len(column) >= capacity:
            if char in closing and len(column) > 1:
                columns.append(column[:-1])
                column = column[-1] + char
            else:
                columns.append(column)
                column = char
        else:
            column += char
    if column:
        columns.append(column)
    return columns


def fit_vertical(text, font_path, box, stroke):
    width, height = max(1, box[2]-box[0]), max(1, box[3]-box[1])
    fitted = None
    for size in range(max(6, min(64, height)), 5, -1):
        font = load_font(str(font_path), size)
        cell_h = max(1, math.ceil(size*1.18))
        cell_w = max(1, math.ceil(size*1.2))
        capacity = max(1, (height-2*stroke)//cell_h)
        columns = _vertical_columns(text, capacity)
        fitted = (font, columns, cell_w, cell_h)
        if len(columns)*cell_w+2*stroke <= width and max(map(len, columns))*cell_h+2*stroke <= height:
            return fitted, False
    return fitted, True


def _draw_vertical(draw, box, fitted, color, outline, stroke):
    font, columns, cell_w, cell_h = fitted
    width = len(columns)*cell_w
    height = max(map(len, columns))*cell_h
    start_x = box[0]+(box[2]-box[0]-width)/2
    start_y = box[1]+(box[3]-box[1]-height)/2
    for column_index, column in enumerate(columns):
        for row, char in enumerate(column):
            char = char.translate(VERTICAL_FORMS)
            bounds = draw.textbbox((0, 0), char, font=font, stroke_width=stroke)
            x = start_x+(len(columns)-1-column_index)*cell_w+(cell_w-(bounds[2]-bounds[0]))/2-bounds[0]
            y = start_y+row*cell_h+(cell_h-(bounds[3]-bounds[1]))/2-bounds[1]
            draw.text((x, y), char, font=font, fill=color, stroke_width=stroke, stroke_fill=outline)


def split_translation(text, regions):
    """Preserve supplied text while distributing it across ordered safe rectangles."""
    if len(regions) == 1:
        return [text]
    weights = [(r["bbox"][2]-r["bbox"][0])*(r["bbox"][3]-r["bbox"][1]) for r in regions]
    chunks = []
    start = 0
    closing = "，。！？、：；）」』】》,.!?;:"
    for index in range(len(regions)-1):
        remaining = len(regions)-index-1
        count = round((len(text)-start)*weights[index]/sum(weights[index:]))
        end = min(len(text)-remaining, start+max(1, count))
        while end < len(text)-remaining and text[end] in closing:
            end += 1
        chunks.append(text[start:end])
        start = end
    return chunks+[text[start:]]


def expand_layout_blocks(blocks):
    expanded = []
    for block in blocks:
        regions = block.get("layout_regions", [])
        text = block.get("translation", "").strip()
        if not regions or len(regions) > len(text):
            expanded.append(block)
            continue
        for index, (region, chunk) in enumerate(zip(regions, split_translation(text, regions))):
            child = deepcopy(block)
            child.update(layout_bbox=region["bbox"], translation=chunk,
                         orientation=region["direction"], segment_index=index+1, segment_count=len(regions))
            expanded.append(child)
    return expanded


def render(cleaned, blocks, font_path, style="outline"):
    """Render exact supplied translations; never asks a model to regenerate artwork."""
    if style != "outline":
        raise ValueError("Only contrast-outline rendering is supported")
    result = Image.fromarray(cleaned).convert("RGB")
    info = []
    expanded = expand_layout_blocks(blocks)
    for index, block in enumerate(expanded):
        text = block.get("translation", "")
        if block.get("segment_count", 1) == 1:
            text = text.strip()
        if not text.strip():
            continue
        neighbors = [other.get("layout_bbox", other["bbox"]) for j, other in enumerate(expanded) if j != index]
        box, region_method, _ = choose_region(cleaned, block, neighbors)
        x1, y1, x2, y2 = box
        padding = max(1, min(4, (x2-x1)//15, (y2-y1)//15))
        box = [x1+padding, y1+padding, x2-padding, y2-padding]
        color, outline, contrast = contrast_style(cleaned, box)
        stroke = 2 if min(x2-x1, y2-y1) >= 65 else 1
        cjk_ratio = sum("\u3400" <= c <= "\u9fff" for c in text)/max(1, len(text))
        original = block["bbox"]
        vertical = block.get("orientation") == "vertical" and (block.get("layout_bbox") or
            (box[3]-box[1]) > (box[2]-box[0])*1.25 or
            (original[3]-original[1]) > (original[2]-original[0])*1.2) and cjk_ratio >= .5
        layer = Image.new("RGBA", result.size)
        pen = ImageDraw.Draw(layer)
        if vertical:
            fitted, overflow = fit_vertical(text, font_path, box, stroke)
            font, lines, _, _ = fitted
            _draw_vertical(pen, box, fitted, color, outline, stroke)
        else:
            (font, value, bounds), overflow = fit_text(pen, text, font_path, box, stroke)
            lines = value.splitlines()
            x = box[0]+(box[2]-box[0]-(bounds[2]-bounds[0]))/2-bounds[0]
            y = box[1]+(box[3]-box[1]-(bounds[3]-bounds[1]))/2-bounds[1]
            pen.multiline_text((x, y), value, font=font, fill=color, spacing=2, align="center",
                               stroke_width=stroke, stroke_fill=outline)
        # Geometry remains authoritative if minimum font size cannot fit.
        # Record clipping for review, and protect pixels outside the chosen region.
        permitted = np.zeros(cleaned.shape[:2], dtype=bool)
        permitted[max(0, box[1]):max(0, box[3]), max(0, box[0]):max(0, box[2])] = True
        alpha = np.asarray(layer.getchannel("A"))
        clipped = bool(np.any((alpha > 0) & ~permitted))
        if clipped:
            layer.putalpha(Image.fromarray(np.where(permitted, alpha, 0).astype(np.uint8)))
        result = Image.alpha_composite(result.convert("RGBA"), layer).convert("RGB")
        info.append({"id": block["id"], "font_size": font.size, "render_bbox": box,
                     "overflow": overflow, "lines": lines, "direction": "vertical-rtl" if vertical else "horizontal",
                     "region_method": region_method, "fill": list(color), "outline": list(outline),
                     "stroke_width": stroke, "median_contrast_ratio": contrast,
                     "segment_index": block.get("segment_index", 1), "segment_count": block.get("segment_count", 1),
                     "plate_applied": False, "small_text": font.size < 10, "clipped": clipped})
    return result, info


def overlay(image, blocks):
    result = Image.fromarray(image).convert("RGB")
    draw = ImageDraw.Draw(result)
    for index, block in enumerate(blocks, 1):
        draw.rectangle(block["bbox"], outline="#e34949", width=2)
        if block.get("bubble_bbox"):
            draw.rectangle(block["bubble_bbox"], outline="#1ba584", width=1)
        draw.text((block["bbox"][0], max(0, block["bbox"][1]-14)), str(index), fill="#e34949")
    return result

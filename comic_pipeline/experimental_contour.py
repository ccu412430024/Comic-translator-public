"""Experimental vertical lettering along balloon contours; opt-in only."""
from functools import lru_cache
from pathlib import Path
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from .rendering import render, choose_region, contrast_style, VERTICAL_FORMS as FORMS
from .fonts import load_font

CLOSING = "，。！？、：；）」』】》,.!?;:…"
OPENING = "（「『【《("


@lru_cache(None)
def word_boundaries(text):
    dictionary = Path(__file__).resolve().parents[1]/'.runtime/upgrade-models/jieba-dict-big.txt'
    try:
        import jieba
    except ImportError:
        return set(range(len(text)+1))
    if not dictionary.is_file():
        return set(range(len(text)+1))
    return _word_boundaries(text, str(dictionary))


@lru_cache(1)
def tokenizer(dictionary):
    import jieba
    jieba.setLogLevel(40)
    return jieba.Tokenizer(dictionary)


def _word_boundaries(text, dictionary):
    boundaries, position = {0}, 0
    for word in tokenizer(dictionary).cut(text, HMM=False):
        position += len(word)
        boundaries.add(position)
    return boundaries


def split_columns(text, capacities):
    target = len(text)/len(capacities)
    boundaries = word_boundaries(text)
    @lru_cache(None)
    def visit(index, start):
        if index == len(capacities):
            return (0, ()) if start == len(text) else None
        remaining = len(capacities)-index-1
        best = None
        for count in range(1, min(capacities[index], len(text)-start-remaining)+1):
            part = text[start:start+count]
            if (index > 0 and part[0] in CLOSING) or (start+count < len(text) and part[-1] in OPENING):
                continue
            tail = visit(index+1, start+count)
            if tail is None:
                continue
            cost = (count-target)**2+.04*(capacities[index]-count)**2
            if start+count not in boundaries:
                cost += 8
            if index < len(capacities)-1 and part[-1] in CLOSING:
                cost -= .7
            candidate = (cost+tail[0], (part,)+tail[1])
            if best is None or candidate[0] < best[0]:
                best = candidate
        return best
    return visit(0, 0)


def longest_run(values):
    edges = np.flatnonzero(np.diff(np.r_[False, values, False].astype(np.int8)))
    if not edges.size:
        return 0, 0
    index = int(np.argmax(edges[1::2]-edges[::2]))
    return int(edges[index*2]), int(edges[index*2+1])


def fit_contour(text, mask, font_path, maximum):
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    min_x, max_x = int(xs.min()), int(xs.max())+1
    integral = np.pad(np.cumsum(mask.astype(np.int32), axis=1), ((0, 0), (1, 0)))
    center = (min_x+max_x)/2
    result, first_fit = None, None
    for size in range(maximum, 5, -1):
        font = load_font(str(font_path), size)
        stroke = 2 if size >= 25 else 1
        glyphs = [font.getbbox(c.translate(FORMS), stroke_width=stroke) for c in set(text)]
        cw = max(math.ceil(size*1.18), max(b[2]-b[0] for b in glyphs)+2)
        ch = max(math.ceil(size*1.18), max(b[3]-b[1] for b in glyphs)+2)
        best = None
        for count in range(1, min(len(text), (max_x-min_x)//cw)+1):
            span = count*cw
            ideal = max(min_x, min(max_x-span, round(center-span/2)))
            starts = set(range(min_x, max_x-span+1, max(2, cw//4))) | {ideal}
            for left in starts:
                columns = []
                for index in range(count):
                    x1 = left+(count-1-index)*cw
                    top, bottom = longest_run((integral[:, x1+cw]-integral[:, x1]) == cw)
                    columns.append((x1, top, x1+cw, bottom))
                capacities = tuple((c[3]-c[1])//ch for c in columns)
                if min(capacities) < 1 or sum(capacities) < len(text):
                    continue
                split = split_columns(text, capacities)
                if split is None:
                    continue
                score = split[0]+.04*count+abs((left+span/2)-center)*.01
                candidate = dict(font=font, stroke=stroke, columns=columns, chunks=split[1], cell_height=ch, score=score)
                if best is None or score < best['score']:
                    best = candidate
        if best is not None:
            if first_fit is None:
                first_fit = size
            best['quality_score'] = best['score']+(first_fit-size)*3
            if result is None or best['quality_score'] < result['quality_score']:
                result = best
        if first_fit is not None and size <= first_fit-2:
            break
    return result


def partition_mask(mask, block, blocks):
    yy, xx = np.indices(mask.shape)
    b = block['bbox']; cx, cy = (b[0]+b[2])/2, (b[1]+b[3])/2
    own = (xx-cx)**2+(yy-cy)**2
    result = mask.copy()
    for other in blocks:
        if other['id'] != block['id']:
            b = other['bbox']; ox, oy = (b[0]+b[2])/2, (b[1]+b[3])/2
            result &= own <= (xx-ox)**2+(yy-oy)**2
    return result


def render_contour(cleaned, blocks, font_path, external_masks=None):
    active = [b for b in blocks if not b.get('preserve_original')]
    baseline, base_info = render(cleaned, active, font_path)
    pixels = np.asarray(baseline).copy()
    by_id = {entry['id']: entry for entry in base_info}
    info = []
    for block in active:
        text = block.get('translation', '').strip()
        if not text or block['id'] not in by_id:
            continue
        base = by_id[block['id']]
        metadata = dict(id=block['id'], baseline_font_size=base['font_size'], font_size=base['font_size'],
                        adopted=False, reason='current layout retained')
        if block.get('orientation') != 'vertical' or sum('\u3400' <= c <= '\u9fff' for c in text)/len(text) < .5:
            info.append(metadata)
            continue
        if external_masks is not None and block['id'] in external_masks:
            external = external_masks[block['id']]
            if isinstance(external, dict):
                allowed = np.zeros(cleaned.shape[:2], bool)
                a, b, c, d = external['bbox']
                allowed[b:d, a:c] = external['mask']
            else:
                allowed = external
            allowed = partition_mask(allowed, block, active)
            method = 'sam2-contour'
        else:
            neighbors = [other['bbox'] for other in active if other['id'] != block['id']]
            _, method, safe = choose_region(cleaned, block, neighbors)
            if method != 'bubble-component' or safe is None:
                info.append(metadata)
                continue
            bubble, region = safe
            allowed = np.zeros(cleaned.shape[:2], bool)
            allowed[bubble[1]:bubble[3], bubble[0]:bubble[2]] = region
        fitted = fit_contour(text, allowed, font_path, min(64, max(12, round(base['font_size']*1.35))))
        if fitted is None or fitted['font'].size < base['font_size']:
            metadata['reason'] = 'contour fit would shrink text'
            info.append(metadata)
            continue
        layer = Image.new('RGBA', baseline.size)
        pen = ImageDraw.Draw(layer)
        color, outline, contrast = contrast_style(cleaned, base['render_bbox'])
        font, stroke = fitted['font'], fitted['stroke']
        for column, chunk in zip(fitted['columns'], fitted['chunks']):
            x1, y1, x2, y2 = column
            top = y1+(y2-y1-len(chunk)*fitted['cell_height'])/2
            for row, char in enumerate(chunk):
                displayed = char.translate(FORMS)
                bounds = pen.textbbox((0, 0), displayed, font=font, stroke_width=stroke)
                x = x1+(x2-x1-(bounds[2]-bounds[0]))/2-bounds[0]
                y = top+row*fitted['cell_height']+(fitted['cell_height']-(bounds[3]-bounds[1]))/2-bounds[1]
                pen.text((x, y), displayed, font=font, fill=color, stroke_width=stroke, stroke_fill=outline)
        alpha = np.asarray(layer.getchannel('A'))
        outside = int(np.count_nonzero((alpha > 0) & ~allowed))
        if outside:
            metadata.update(reason='glyphs escaped safe contour; retained current layout', outside_pixels=outside)
            info.append(metadata)
            continue
        x1, y1, x2, y2 = base['render_bbox']
        pixels[y1:y2, x1:x2] = cleaned[y1:y2, x1:x2]
        pixels = np.asarray(Image.alpha_composite(Image.fromarray(pixels).convert('RGBA'), layer).convert('RGB')).copy()
        metadata.update(adopted=True, reason=method, font_size=font.size, chunks=list(fitted['chunks']),
                        column_boxes=fitted['columns'], outside_pixels=0, median_contrast_ratio=contrast)
        info.append(metadata)
    return Image.fromarray(pixels), info

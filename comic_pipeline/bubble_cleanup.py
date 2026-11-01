"""Conservative solid-background cleanup inside measured, uniform white balloons."""
import unicodedata

import numpy as np

from .rendering import choose_region


def punctuation_only(text):
    text = text.strip()
    return bool(text) and all(c.isspace() or unicodedata.category(c)[0] in "PS" for c in text)


def refine_bubbles(original, cleaned, blocks):
    result = cleaned.copy()
    changed_mask = np.zeros(original.shape[:2], np.uint8)
    notes, retained = [], []
    height, width = original.shape[:2]
    for block in blocks:
        x1, y1, x2, y2 = block["bbox"]
        translation = block.get("translation", "").strip()
        if punctuation_only(block.get("source_text", "")) and (not translation or punctuation_only(translation)):
            result[y1:y2, x1:x2] = original[y1:y2, x1:x2]
            retained.append(block["id"])
            notes.append(f'{block["id"]}: original punctuation retained')
            continue
        if not translation:
            notes.append(f'{block["id"]}: no translation; original text preserved')
            continue
        _, method, safe = choose_region(original, block)
        if method != "bubble-component" or safe is None:
            notes.append(f'{block["id"]}: uniform balloon not confirmed; retain LaMa repair')
            continue
        bubble, region = safe
        bx1, by1, bx2, by2 = bubble
        roi = original[by1:by2, bx1:bx2]
        color = np.median(roi[region], axis=0)
        if min(color) < 240 or max(color)-min(color) > 8:
            notes.append(f'{block["id"]}: background is not uniform white; retain LaMa repair')
            continue
        safe_mask = np.zeros((height, width), bool)
        safe_mask[by1:by2, bx1:bx2] = region
        influence = np.zeros((height, width), bool)
        influence[max(0, y1-3):min(height, y2+3), max(0, x1-3):min(width, x2+3)] = True
        selected = safe_mask & influence
        result[selected] = np.round(color).astype(np.uint8)
        changed_mask[selected] = 255
        notes.append(f'{block["id"]}: removed old text only inside measured white balloon')
    return result, changed_mask, {"notes": notes, "retained_punctuation_ids": retained,
        "refinement_pixels": int(np.count_nonzero(changed_mask)),
        "outside_refinement_and_retained_changed_pixels": int(np.count_nonzero(
            np.any(result != cleaned, axis=2) & (changed_mask == 0) & _exclude_retained(original.shape[:2], blocks, retained)))}


def _exclude_retained(shape, blocks, retained):
    allowed = np.ones(shape, bool)
    for block in blocks:
        if block["id"] in retained:
            x1, y1, x2, y2 = block["bbox"]
            allowed[y1:y2, x1:x2] = False
    return allowed

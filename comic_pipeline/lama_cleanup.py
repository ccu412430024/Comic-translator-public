"""Uniform bubbles first, then bounded merged crops on the selected LaMa backend."""
import numpy as np

from .upstream import configure

VERSION = "uniform-first-merged-lama-v3"


def repair_regions(mask, padding=80, max_side=768):
    configure()
    from modules.utils.inpainting import boxes_from_mask
    from pipeline.inpainting_boxes import merge_overlapping_padded_boxes
    boxes = boxes_from_mask(mask)
    merged = merge_overlapping_padded_boxes([(int(x1), int(y1), int(x2-x1), int(y2-y1))
                                           for x1, y1, x2, y2 in boxes], mask.shape, pad=padding)
    stride = max_side-2*padding
    if stride <= 0:
        raise ValueError("Repair crop must be larger than its context margins")
    def starts(low, high):
        values = list(range(low, max(low+1, high-max_side+1), stride))
        last = max(low, high-max_side)
        if values[-1] != last:
            values.append(last)
        return values
    regions = []
    for x1, y1, x2, y2 in merged:
        for y in starts(y1, y2):
            for x in starts(x1, x2):
                right, bottom = min(x+max_side, x2), min(y+max_side, y2)
                if np.any(mask[y:bottom, x:right]):
                    regions.append((x, y, right, bottom))
    return regions, dict(input_regions=len(boxes), merged_regions=len(merged))


def protect(mask, blocks):
    for block in blocks or []:
        if block.get("preserve_original") or not block.get("translation", "").strip():
            x1, y1, x2, y2 = block["bbox"]
            mask[y1:y2, x1:x2] = 0


class LamaCleaner:
    def __init__(self, load_model):
        self.load_model = load_model
        self.handler = None
        self.last_metrics = {}

    def clean(self, pixels, mask, blocks=None):
        configure()
        working, residual = pixels.copy(), np.where(mask > 0, 255, 0).astype(np.uint8)
        protect(residual, blocks)
        before = int(np.count_nonzero(residual))
        filled = white_filled = 0
        if blocks and before:
            # These exact safe white regions were already painted by our final
            # refinement. Move that proven operation ahead of neural repair.
            from .bubble_cleanup import refine_bubbles
            working, white_mask, _ = refine_bubbles(pixels, working, blocks)
            white_filled = sum(bool(np.any((white_mask[y1:y2, x1:x2] > 0) & (residual[y1:y2, x1:x2] > 0)))
                               for block in blocks for x1, y1, x2, y2 in [block["bbox"]]
                               if block.get("translation", "").strip() and not block.get("preserve_original"))
            residual[white_mask > 0] = 0
        if blocks and np.any(residual):
            from pipeline.inpainting import InpaintingHandler
            from .comic_original import text_blocks
            self.handler = self.handler or InpaintingHandler(None)
            working, residual, filled = self.handler._apply_fast_bubble_cleanup(working, residual, text_blocks(blocks))
            protect(residual, blocks)
        regions, counts = repair_regions(residual)
        source = working.copy()
        covered = np.zeros(residual.shape, dtype=bool)
        calls = processed_pixels = 0
        if regions:
            from modules.inpainting.schema import Config
            # ORIGINAL is essential: CROP would split every glyph again.
            model, config = self.load_model(), Config(hd_strategy="Original")
            for x1, y1, x2, y2 in regions:
                local_mask = residual[y1:y2, x1:x2]
                selected = (local_mask > 0) & ~covered[y1:y2, x1:x2]
                if not np.any(selected):
                    continue
                repaired = model(source[y1:y2, x1:x2].copy(), local_mask.copy(), config)
                working[y1:y2, x1:x2][selected] = np.clip(repaired, 0, 255).astype(np.uint8)[selected]
                covered[y1:y2, x1:x2] |= selected
                calls += 1
                processed_pixels += ((y2-y1+7)//8*8)*((x2-x1+7)//8*8)
        for block in blocks or []:
            if block.get("preserve_original") or not block.get("translation", "").strip():
                x1, y1, x2, y2 = block["bbox"]
                working[y1:y2, x1:x2] = pixels[y1:y2, x1:x2]
        self.last_metrics = dict(version=VERSION, fast_fill_passes=filled+white_filled,
            white_filled=white_filled, additional_uniform_passes=filled, mask_pixels=before,
            residual_pixels=int(np.count_nonzero(residual)), model_calls=calls,
            processed_pixels=processed_pixels, backend=getattr(model, "provider", "CPUExecutionProvider") if calls else "none",
            gpu_fallback=getattr(model, "fallback", "") if calls else "", **counts)
        return working

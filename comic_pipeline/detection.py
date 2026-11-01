"""Supplement full-page RT-DETR with overlapping, native-resolution tiles."""
import math

import numpy as np
from PIL import Image

TILING_VERSION = 1


def tile_starts(length, size, overlap):
    if size < 64 or not 0 <= overlap < .75:
        raise ValueError("Tile size must be >=64 and overlap must be in [0, .75)")
    if length <= size:
        return [0]
    count = math.ceil((length-size)/(size*(1-overlap)))
    return sorted({round(i*(length-size)/count) for i in range(count+1)})


def overlap_metrics(a, b):
    intersection = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    area_a = max(0, a[2]-a[0])*max(0, a[3]-a[1])
    area_b = max(0, b[2]-b[0])*max(0, b[3]-b[1])
    return intersection/max(1, area_a+area_b-intersection), intersection/max(1, area_a)


def novel_candidates(candidates, existing):
    """Keep complete high-score candidates without merging neighboring dialogues."""
    accepted = []
    for item in sorted(candidates, key=lambda item: -item["score"]):
        duplicate = False
        for box in list(existing) + [other["bbox"] for other in accepted]:
            iou, contained = overlap_metrics(item["bbox"], box)
            if iou >= .35 or contained >= .65:
                duplicate = True
                break
        if not duplicate:
            accepted.append(item)
    return accepted


def preserve_baseline_ids(ordered, baseline):
    """Supplementary boxes must not redirect existing per-ID manual edits."""
    original = {tuple(block["bbox"]): block["id"] for block in baseline}
    added = 0
    for block in ordered:
        if tuple(block["bbox"]) in original:
            block["id"] = original[tuple(block["bbox"])]
        else:
            added += 1
            block["id"] = f"t{added:04}"
    return ordered


def group_paddle_lines(lines, bubbles):
    """Join columns sharing one measured balloon; retain every free-text line.

    This is geometry only. There is no semantic verification or review gate.
    """
    groups, free = {}, []
    bubbles = [] if bubbles is None else list(bubbles)
    for line in lines:
        matches = []
        for index, bubble in enumerate(bubbles):
            _, fraction = overlap_metrics(line["bbox"], bubble)
            if fraction >= .6:
                area = max(1, (bubble[2]-bubble[0])*(bubble[3]-bubble[1]))
                matches.append((fraction, -area, index))
        if matches:
            index = max(matches)[2]
            groups.setdefault(index, []).append(line)
        else:
            free.append(dict(line, bubble_bbox=None, **{"class": "text_free"}))
    for index, items in groups.items():
        boxes = [item["bbox"] for item in items]
        box = [min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes)]
        vertical = sum(item.get("orientation") == "vertical" for item in items) > len(items)/2
        free.append(dict(bbox=box, bubble_bbox=[int(x) for x in bubbles[index]],
                         orientation="vertical" if vertical else "horizontal",
                         score=min(item.get("score", 0) for item in items),
                         **{"class": "text_bubble"}))
    return free


def tile_predictions(image, predict, size=640, overlap=.25):
    height, width = image.shape[:2]
    candidates, tiles = [], []
    for y in tile_starts(height, size, overlap):
        for x in tile_starts(width, size, overlap):
            right, bottom = min(width, x+size), min(height, y+size)
            if x == y == 0 and right == width and bottom == height:
                continue  # The full-page pass already covers this exact image.
            tiles.append([x, y, right, bottom])
            for item in predict(image[y:bottom, x:right]):
                a, b, c, d = item["bbox"]
                # Internal crop boundaries may cut sentences. Page edges remain valid.
                if ((x > 0 and a <= 4) or (y > 0 and b <= 4) or
                    (right < width and c >= right-x-4) or
                    (bottom < height and d >= bottom-y-4)):
                    continue
                box = [max(x, x+a), max(y, y+b), min(right, x+c), min(bottom, y+d)]
                if box[2]-box[0] > 5 and box[3]-box[1] > 5:
                    candidates.append(dict(item, bbox=box, tile=[x, y, right, bottom]))
    return candidates, tiles


def raw_rtdetr(session, image, threshold):
    pil = Image.fromarray(image)
    tensor = np.asarray(pil.resize((640, 640)), np.float32).transpose(2, 0, 1)[None]/255
    labels, boxes, scores = session.run(None, {"images": tensor,
        "orig_target_sizes": np.array([[pil.width, pil.height]], np.int64)})[:3]
    return [dict(bbox=[int(v) for v in box], score=float(score), label=int(label))
            for label, box, score in zip(labels.reshape(-1), boxes.reshape(-1, 4), scores.reshape(-1))
            if int(label) in (1, 2) and float(score) >= threshold]


def detect_tiled(detector, image, threshold=.3, size=640, overlap=.25):
    baseline = detector.detect(image)
    existing = [list(map(int, block.xyxy)) for block in baseline]
    candidates, tiles = tile_predictions(image, lambda crop: raw_rtdetr(detector.session, crop, threshold), size, overlap)
    novel = novel_candidates(candidates, existing)
    bubbles = np.array([block.bubble_xyxy for block in baseline if block.bubble_xyxy is not None])
    added = []
    # Build independently: the upstream all-box merger can unite adjacent bubbles.
    for item in novel:
        blocks = detector.create_text_blocks(image, np.array([item["bbox"]]), bubbles)
        for block in blocks:
            block.detection_source = "tile"
            block.detection_score = item["score"]
        added.extend(blocks)
    return baseline+added, dict(tiles=tiles, baseline_boxes=len(baseline), added_boxes=len(added),
                                raw_tile_candidates=len(candidates), accepted=novel)

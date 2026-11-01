from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import numpy as np
from PIL import Image, ImageOps

from . import rendering
from . import bubble_cleanup
from .storage import StageCache, Store, fingerprint, write_json
from .translation import create_translator
from .upstream import Engines, UPSTREAM_COMMIT
from .fonts import fallback_signature


def natural_key(path):
    return [(0, int(x)) if x.isdigit() else (1, x.casefold()) for x in re.split(r"(\d+)", str(path))]


def page_id(relative, content_hash):
    return hashlib.sha256(str(relative).replace("\\", "/").encode()).hexdigest()[:8] + "-" + content_hash[:8]


def serialize_blocks(blocks, width, height):
    result = []
    for b in blocks:
        x1, y1, x2, y2 = (int(x) for x in b.xyxy)
        box = [max(0, x1), max(0, y1), min(width, x2), min(height, y2)]
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        bubble = [int(x) for x in b.bubble_xyxy] if b.bubble_xyxy is not None else None
        if bubble:
            bubble = [max(0, bubble[0]), max(0, bubble[1]), min(width, bubble[2]), min(height, bubble[3])]
        result.append({"bbox": box, "bubble_bbox": bubble,
                       "font_color": [int(x) for x in b.font_color] if len(b.font_color) else [0, 0, 0],
                       "orientation": b.direction or "unknown", "class": b.text_class})
    # Row bands, top-to-bottom; within a band right-to-left for Japanese manga.
    # This is a heuristic, not a panel-layout recognizer.
    pending = sorted(result, key=lambda b: (b["bbox"][1], -b["bbox"][0]))
    ordered = []
    while pending:
        anchor = pending.pop(0)
        top, bottom = anchor["bbox"][1], anchor["bbox"][3]
        band = [anchor]
        remaining = []
        for b in pending:
            center = (b["bbox"][1]+b["bbox"][3])/2
            (band if top <= center <= bottom else remaining).append(b)
        ordered.extend(sorted(band, key=lambda b: -b["bbox"][0]))
        pending = remaining
    for i, b in enumerate(ordered, 1):
        b.update(id=f"b{i:04}", reading_order=i)
    return ordered


def run(args, translator=None):
    input_path = args.input.resolve()
    output = args.output.resolve()
    if not input_path.exists():
        raise ValueError(f"Input does not exist: {input_path}")
    if input_path == output or (input_path.is_dir() and input_path in output.parents):
        raise ValueError("Place --output outside the input folder to avoid re-importing results")
    root = input_path if input_path.is_dir() else input_path.parent
    paths = sorted((p for p in (root.rglob("*") if input_path.is_dir() else [input_path])
                    if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}), key=natural_key)
    if args.limit:
        paths = paths[:args.limit]
    if not paths:
        raise ValueError("No PNG/JPG/WebP images found")
    output.mkdir(parents=True, exist_ok=True)
    translator = translator or create_translator(args.translator, args.translations)
    font = args.font.resolve() if args.font else rendering.default_font()
    if not font.is_file():
        raise FileNotFoundError(f"Font does not exist: {font}")
    font_hash = hashlib.sha256(font.read_bytes()).hexdigest()
    render_style = getattr(args, "render_style", "outline")
    edits = json.loads(args.edits.read_text(encoding="utf-8")) if args.edits else {}
    engine = Engines(args.threshold, args.ocr_model)
    detection_mode = getattr(args, "detection_mode", "standard")
    tile_size, tile_overlap = getattr(args, "tile_size", 640), getattr(args, "tile_overlap", .25)
    engine.detection_mode, engine.tile_size, engine.tile_overlap = detection_mode, tile_size, tile_overlap
    store = Store(output / "project.sqlite3")
    summary = {"book": args.book, "source": "ja", "target": "zh-TW", "memory_enabled": False,
               "translator": translator.signature, "ocr_model": args.ocr_model, "input_mode": args.input_mode,
               "render_style": render_style, "upstream_commit": UPSTREAM_COMMIT, "pages": []}
    summary["cleanup_profile"] = "bubble-refined"
    summary["detection_profile"] = dict(mode=detection_mode, tile_size=tile_size, tile_overlap=tile_overlap)
    failures = []
    try:
        for index, path in enumerate(paths, 1):
            relative = path.relative_to(root)
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            pid = page_id(relative, sha)
            folder = output / "pages" / pid
            folder.mkdir(parents=True, exist_ok=True)
            chapter = args.chapter or (relative.parent.as_posix() if relative.parent != Path(".") else "chapter-001")
            page = {"id": pid, "filename": relative.as_posix(), "chapter": chapter, "order": index,
                    "image_sha256": sha, "status": "processing", "blocks": [], "warnings": []}
            cache = StageCache(folder, args.from_stage)
            print(f"[{index}/{len(paths)}] {relative}", flush=True)
            try:
                with Image.open(path) as opened:
                    image = ImageOps.exif_transpose(opened).convert("RGB")
                    image.save(folder / "original.png")
                    pixels = np.asarray(image)
                if hasattr(translator, "set_page_image"):
                    translator.set_page_image(image)
                width, height = image.size
                page.update(width=width, height=height)
                detector_key = fingerprint([sha, UPSTREAM_COMMIT, "rtdetr-v4-s-int8", args.threshold, args.input_mode, 1])
                if detection_mode == "tiled" and args.input_mode == "page":
                    from . import detection
                    detector_key = fingerprint([detector_key, detection_mode, tile_size, tile_overlap,
                                                hashlib.sha256(Path(detection.__file__).read_bytes()).hexdigest()])
                elif detection_mode == "paddle" and args.input_mode == "page":
                    from . import detection
                    from .experimental import text_detectors
                    detector_key = fingerprint([detector_key, detection_mode,
                        hashlib.sha256(Path(detection.__file__).read_bytes()).hexdigest(),
                        hashlib.sha256(Path(text_detectors.__file__).read_bytes()).hexdigest(),
                        "eb13b44b25bb36f89528b68720af8a61d9cf381176107f465db1757b65d086e1"])
                def do_detect():
                    if args.input_mode == "text-crop":
                        return [{"id": "b0001", "reading_order": 1, "bbox": [0, 0, width, height],
                                 "bubble_bbox": None, "font_color": [0, 0, 0],
                                 "orientation": "unknown", "class": "user-specified-text-crop"}]
                    found = engine.detect(pixels)
                    ordered = serialize_blocks(found, width, height)
                    if detection_mode == "tiled":
                        from .detection import preserve_baseline_ids
                        baseline = serialize_blocks([b for b in found if getattr(b, "detection_source", None) != "tile"], width, height)
                        preserve_baseline_ids(ordered, baseline)
                    return ordered
                detected = cache.run("detect", detector_key,
                                     do_detect)
                if not detected:
                    page["warnings"].append("No text detected; output preserves the original page")
                rendering.overlay(pixels, detected).save(folder / "detected.png")
                ocr_key = fingerprint([sha, detected, args.ocr_model, UPSTREAM_COMMIT, 1])

                def do_ocr():
                    texts = engine.recognize(pixels, detected) if detected else []
                    if len(texts) != len(detected):
                        raise ValueError("OCR returned an unexpected number of text blocks")
                    return [dict(b, source_text=text) for b, text in zip(detected, texts)]

                blocks = deepcopy(cache.run("ocr", ocr_key, do_ocr))
                page_edits = edits.get(pid, {})
                unknown = set(page_edits)-{b["id"] for b in blocks}
                if unknown:
                    raise ValueError(f"Unknown edited block IDs: {sorted(unknown)}")
                for b in blocks:
                    correction = page_edits.get(b["id"], {})
                    if "source_text" in correction:
                        b["original_ocr"] = b["source_text"]
                        b["source_text"] = correction["source_text"]
                    if not b["source_text"].strip():
                        page["warnings"].append(f"{b['id']}: empty OCR; preserved without erasing")
                page["blocks"] = blocks
                text_key = fingerprint([[(b["id"], b["source_text"]) for b in blocks],
                                        translator.signature, "ja", "zh-TW", page_edits, 1])

                def do_translate():
                    needed = [b for b in blocks if b["source_text"].strip() and
                              "translation" not in page_edits.get(b["id"], {})]
                    values = translator.translate(needed, page_id=pid) if needed else {}
                    for b in blocks:
                        corrected = page_edits.get(b["id"], {}).get("translation")
                        if corrected is not None:
                            if not isinstance(corrected, str):
                                raise ValueError("Edited translation must be a string")
                            values[b["id"]] = corrected
                    return values

                values = cache.run("translate", text_key, do_translate)
                for b in blocks:
                    b["translation"] = values.get(b["id"], "")
                write_json(folder / "blocks.json", blocks)
                clean_key = fingerprint([sha, [(b["bbox"], bool(b["translation"].strip())) for b in blocks],
                                         "lama-manga-dynamic", UPSTREAM_COMMIT, args.mask_dilation,
                                         "uniform-first-merged-lama-v3",
                                         [(b.get("bubble_bbox"), b.get("class"), b.get("preserve_original", False)) for b in blocks]])

                def do_clean():
                    mask = engine.make_mask(pixels, blocks, args.mask_dilation)
                    cleaned = engine.clean_fast(pixels, mask, blocks) if hasattr(engine, "clean_fast") else engine.clean(pixels, mask)
                    Image.fromarray(mask).save(folder / "mask.png")
                    Image.fromarray(cleaned).save(folder / "cleaned.png")
                    return {"masked_pixels": int(np.count_nonzero(mask)),
                            "unmasked_changed_pixels": int(np.count_nonzero(np.any(cleaned != pixels, axis=2) & (mask == 0)))}

                clean_info = cache.run("clean", clean_key, do_clean, ["mask.png", "cleaned.png"])
                page["cleaning"] = clean_info
                refine_key = fingerprint([clean_key, blocks,
                    hashlib.sha256(Path(bubble_cleanup.__file__).read_bytes()).hexdigest(),
                    hashlib.sha256(Path(rendering.__file__).read_bytes()).hexdigest()])

                def do_refine():
                    with Image.open(folder / "cleaned.png") as clean_image:
                        refined, mask, details = bubble_cleanup.refine_bubbles(
                            pixels, np.asarray(clean_image.convert("RGB")), blocks)
                    Image.fromarray(refined).save(folder / "cleaned-refined.png")
                    Image.fromarray(mask).save(folder / "refinement-mask.png")
                    return details

                page["refinement"] = cache.run("refine", refine_key, do_refine,
                                               ["cleaned-refined.png", "refinement-mask.png"])
                retained = set(page["refinement"]["retained_punctuation_ids"])
                for b in blocks:
                    b["preserve_original"] = b["id"] in retained
                write_json(folder / "blocks.json", blocks)
                render_key = fingerprint([refine_key, blocks, font_hash, render_style, fallback_signature(),
                    hashlib.sha256(Path(rendering.__file__).read_bytes()).hexdigest()])

                def do_render():
                    with Image.open(folder / "cleaned-refined.png") as clean_image:
                        render_blocks = [b for b in blocks if not b["preserve_original"]]
                        result, info = rendering.render(np.asarray(clean_image.convert("RGB")), render_blocks, font, render_style)
                    result.save(folder / "translated.png")
                    return info

                page["rendering"] = cache.run("render", render_key, do_render, ["translated.png"])
                for entry in page["rendering"]:
                    if entry["overflow"]:
                        page["warnings"].append(f"{entry['id']}: translation does not fit its text box")
                    if entry.get("small_text"):
                        page["warnings"].append(f"{entry['id']}: font smaller than 10 pixels; inspect readability")
                    if entry.get("clipped"):
                        page["warnings"].append(f"{entry['id']}: text clipped at safe region; inspect layout")
                if args.reference:
                    ref = args.reference / relative
                    if ref.is_file():
                        with Image.open(ref) as ref_image:
                            ref_image.convert("RGB").save(folder / "reference.png")
                        page["has_reference"] = True
                page["status"] = "needs_review" if page["warnings"] else "complete"
                print("  " + ", ".join(x["stage"] + (":cache" if x["cached"] else ":run") for x in cache.events), flush=True)
            except Exception as exc:
                page.update(status="error", error=f"{type(exc).__name__}: {exc}")
                failures.append({"page": pid, "error": page["error"]})
                print(f"  ERROR: {page['error']}", flush=True)
            page["stages"] = cache.events
            write_json(folder / "page.json", page)
            store.save(args.book, "ja", "zh-TW", page)
            summary["pages"].append(page)
            write_json(output / "run.json", summary)
    finally:
        store.close()
    summary["failures"] = failures
    write_json(output / "run.json", summary)
    from .report import make_report
    make_report(output, summary)
    print(f"Report: {output / 'index.html'}", flush=True)
    return 1 if failures else 0

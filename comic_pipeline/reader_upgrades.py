"""Reusable experimental models, with bounded in-memory caches only."""
from collections import OrderedDict
import io
import json
import os
import subprocess

import numpy as np

from .experimental.ballons_ctd import translated_area
from .experimental.models import ComicTextMask, AOTInpainting, restrict_text_mask
from .reader_options import ROOT, ProcessingError


def remember(cache, key, value, limit=2):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)
    return value


class ReaderUpgrades:
    def __init__(self):
        self.ctd = self.aot = None
        self.probabilities = OrderedDict()
        self.probability_metrics = {}
        self.last_mask = {}
        self.sam_cache = OrderedDict()
        self.last_cleaning = {}

    def mask(self, pixels, blocks, preset, engine, page_key):
        self.last_mask = dict(provider="CPU-rules", cached=False)
        active = [b for b in blocks if b.get("translation", "").strip() and not b.get("preserve_original")]
        if not active:
            return np.zeros(pixels.shape[:2], np.uint8)
        if preset == "standard":
            return engine.make_mask(pixels, active)
        if page_key not in self.probabilities:
            self.ctd = self.ctd or ComicTextMask()
            probability, details = self.ctd.predict(pixels)
            remember(self.probabilities, page_key, probability)
            self.probability_metrics[page_key] = details
            self.probability_metrics = {key:value for key,value in self.probability_metrics.items() if key in self.probabilities}
            cached = False
        else:
            cached = True
        self.last_mask = dict(self.probability_metrics.get(page_key, {}), cached=cached)
        probability = self.probabilities[page_key]
        hybrid = preset.startswith("hybrid")
        mask = restrict_text_mask(probability, blocks, threshold=.15 if hybrid else .3, dilation=7 if hybrid else 3)
        if hybrid:
            mask = np.maximum(mask, engine.make_mask(pixels, active))
        return np.where(translated_area(pixels.shape[:2], blocks), mask, 0).astype(np.uint8)

    def clean(self, pixels, mask, preset, engine, blocks=None):
        self.last_cleaning = {}
        if not preset.endswith("aot"):
            net = getattr(self.aot, "net", None)
            if hasattr(net, "release_gpu"):
                net.release_gpu()
            if hasattr(engine, "clean_fast"):
                result = engine.clean_fast(pixels, mask, blocks)
                self.last_cleaning = dict(engine.last_cleanup)
                return result
            return engine.clean(pixels, mask)
        if not np.any(mask):
            return pixels.copy()
        self.aot = self.aot or AOTInpainting()
        # AOT and LaMa are alternative inpainting routes. Free an idle LaMa
        # GPU worker when switching to AOT, without evicting either CPU model.
        net = getattr(self.aot, "net", None)
        lama = getattr(engine, "_inpainter", None)
        if getattr(net, "device", "cpu") == "dml" and getattr(lama, "provider", "") == "DmlExecutionProvider":
            lama.close()
            engine._inpainter = None
        if pixels.shape[0]*pixels.shape[1] <= 1_500_000:
            result, details = self.aot.predict(pixels, mask)
            self.last_cleaning = dict(details, backend=details.get("provider", "CPUExecutionProvider"), model_calls=1)
            return result
        # Bound AOT's peak memory on high-DPI captures. Only masked pixels are
        # pasted; overlap supplies context at tile boundaries.
        from .detection import tile_starts
        result = pixels.copy()
        calls, details = 0, {}
        for y in tile_starts(pixels.shape[0], 1024, .25):
            for x in tile_starts(pixels.shape[1], 1024, .25):
                crop = pixels[y:y+1024, x:x+1024]
                selected = mask[y:y+1024, x:x+1024]
                if np.any(selected):
                    repaired, details = self.aot.predict(crop, selected)
                    calls += 1
                    result[y:y+crop.shape[0], x:x+crop.shape[1]][selected > 0] = repaired[selected > 0]
        self.last_cleaning = dict(details, backend=details.get("provider", "CPUExecutionProvider"), model_calls=calls)
        return result

    def release_gpu(self):
        released = []
        for name, model in (("ctd", self.ctd), ("aot", self.aot)):
            net = getattr(model, "net", None)
            if hasattr(net, "release_gpu") and net.release_gpu():
                released.append(name)
        return released

    def close(self):
        for model in (self.ctd, self.aot):
            net = getattr(model, "net", None)
            if net is not None and hasattr(net, "close"):
                net.close()
        self.ctd = self.aot = None
        self.probabilities.clear()
        self.probability_metrics.clear()

    def sam_masks(self, pixels, blocks, page_key):
        prompts = [dict(id=b["id"], bbox=b["bbox"], bubble_bbox=b["bubble_bbox"]) for b in blocks
                   if b.get("translation", "").strip() and not b.get("preserve_original") and b.get("bubble_bbox")]
        if not prompts:
            return {}, dict(requested=0, accepted=0)
        key = (page_key, json.dumps(prompts, sort_keys=True))
        if key in self.sam_cache:
            masks, metadata = self.sam_cache[key]
            return masks, dict(metadata)
        request = io.BytesIO()
        np.savez(request, image=pixels, prompts=np.frombuffer(json.dumps(prompts).encode(), np.uint8))
        environment = dict(os.environ, HF_HUB_OFFLINE="1")
        environment.pop("GEMINI_API_KEY", None)
        try:
            result = subprocess.run([str(ROOT/".venv-upgrades/Scripts/python.exe"), str(ROOT/"scripts/reader_sam_worker.py")],
                input=request.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=ROOT,
                timeout=180, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProcessingError("SAM 執行失敗或逾時，請檢查 SAM 模型與隔離環境") from exc
        if result.returncode:
            raise ProcessingError("SAM 執行失敗，請檢查 SAM 模型與隔離環境")
        with np.load(io.BytesIO(result.stdout), allow_pickle=False) as data:
            metadata = json.loads(data["metadata"].tobytes())
            if not metadata.get("ok"):
                raise ProcessingError("SAM 執行失敗，請檢查 SAM 模型與隔離環境")
            masks = {item["id"]: dict(bbox=item["bbox"], mask=data[item["id"]].astype(bool)) for item in metadata["masks"]}
        remember(self.sam_cache, key, (masks, dict(requested=len(prompts), accepted=len(masks))))
        return masks, dict(self.sam_cache[key][1])

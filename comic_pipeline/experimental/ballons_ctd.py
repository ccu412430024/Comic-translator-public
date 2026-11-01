"""Run the pinned, unmodified BallonsTranslator CTD backend without its GUI.

Only package registration is bypassed. Inference, line grouping and mask
refinement execute directly from the upstream checkout, including OpenCV DNN.
"""
import importlib
import os
from pathlib import Path
import sys
import subprocess
import time
import types

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "fa3e1374b4ca7c040a1a04effb96c95aac2a5e5c"
MODEL_SHA256 = "1a86ace74961413cbd650002e7bb4dcec4980ffa21b2f19b86933372071d718f"


def configure():
    checkout = ROOT / "vendor/ballonstranslator"
    if not (checkout / "ballontranslator").is_dir():
        raise RuntimeError("Missing vendor/ballonstranslator; see the experiment report.")
    actual = subprocess.run(["git", "-c", "safe.directory=" + checkout.as_posix(),
                             "-C", str(checkout), "rev-parse", "HEAD"],
                            check=True, text=True, capture_output=True).stdout.strip()
    if actual != COMMIT:
        raise RuntimeError("BallonsTranslator checkout differs from the pinned experiment version")
    sys.path.insert(0, str(checkout))
    os.environ.setdefault("BALLOONTRANS_APP_ROOT", str(checkout))
    # Their modules/__init__.py registers all GUI/translators/inpainters. None
    # is needed for this standalone backend. Keep all actual submodules intact.
    for suffix in ("modules", "modules.textdetector", "modules.textdetector.ctd",
                   "modules.textdetector.yolov5"):
        name = "ballontranslator." + suffix
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = [str(checkout / "ballontranslator" / Path(*suffix.split(".")))]
            package.__package__ = name
            sys.modules[name] = package
    return importlib.import_module("ballontranslator.modules.textdetector.ctd.inference")


def translated_area(shape, blocks):
    """Same active-box margins as the earlier experiment; protect skipped boxes."""
    height, width = shape
    allowed = np.zeros(shape, bool)
    skipped = []
    for block in blocks:
        x1, y1, x2, y2 = block["bbox"]
        if not block.get("translation", "").strip() or block.get("preserve_original"):
            skipped.append((x1, y1, x2, y2))
            continue
        margin = max(8, min(24, (y2-y1)//8))
        allowed[max(0, y1-margin):min(height, y2+margin),
                max(0, x1-margin):min(width, x2+margin)] = True
    for x1, y1, x2, y2 in skipped:
        allowed[max(0, y1):min(height, y2), max(0, x1):min(width, x2)] = False
    return allowed


class BallonsCTD:
    def __init__(self):
        import cv2
        import torch
        cv2.setNumThreads(4)
        torch.set_num_threads(4)
        upstream = configure()
        self.detector = upstream.TextDetector(
            str(ROOT / ".runtime/upgrade-models/ctd.onnx"), device="cpu", detect_size=1024)

    def predict(self, rgb):
        import cv2
        started = time.perf_counter()
        # Official app imread returns RGB. Keep their preprocessing unchanged.
        raw, refined, blocks = self.detector(np.ascontiguousarray(rgb), keep_undetected_mask=False)
        # Match detector_ctd.py's default final dilation: radius 3, 7x7 ellipse.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7), (3, 3))
        dilated = cv2.dilate(refined, kernel)
        metadata = dict(
            commit=COMMIT, model_sha256=MODEL_SHA256,
            seconds=round(time.perf_counter()-started, 3), device="CPU",
            backend="OpenCV DNN", detect_size=1024, mask_dilate_radius=3,
            keep_undetected_mask=False,
            blocks=[dict(bbox=[int(x) for x in block.xyxy],
                         lines=np.asarray(block.lines).tolist(),
                         source_vertical=bool(block.src_is_vertical),
                         detected_font_size=float(block._detected_font_size)) for block in blocks])
        return raw, refined, dilated, metadata

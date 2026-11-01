"""Independent DBNet and PaddleOCR-v6 detector experiments, CPU only."""
import ast
import importlib
import json
import math
from pathlib import Path
import sys
import time
import types

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / ".runtime/detection-models"


def records(polygons, scores, width, height):
    result = []
    for polygon, score in zip(polygons, scores):
        points = np.asarray(polygon, np.float64)
        points[:, 0] = points[:, 0].clip(0, width)
        points[:, 1] = points[:, 1].clip(0, height)
        box = [math.floor(points[:, 0].min()), math.floor(points[:, 1].min()),
               math.ceil(points[:, 0].max()), math.ceil(points[:, 1].max())]
        if box[2]-box[0] <= 2 or box[3]-box[1] <= 2:
            continue
        result.append(dict(bbox=box, polygon=points.round(2).tolist(), score=float(score),
                           orientation="vertical" if box[3]-box[1] > 1.3*(box[2]-box[0]) else "horizontal"))
    result.sort(key=lambda b: (b["bbox"][1], -b["bbox"][0]))
    for i, block in enumerate(result, 1):
        block["id"] = f"line{i:04}"
    return result


class DBNetDetector:
    def __init__(self, size=2048):
        import cv2
        import torch
        cv2.setNumThreads(4)
        torch.set_num_threads(4)
        folder = ROOT/"vendor/manga-image-translator/manga_translator/detection/default_utils"
        package = types.ModuleType("mit_default_utils")
        package.__path__ = [str(folder)]
        package.__package__ = "mit_default_utils"
        sys.modules["mit_default_utils"] = package
        module = importlib.import_module("mit_default_utils.DBNet_resnet34")
        decoder = importlib.import_module("mit_default_utils.dbnet_utils")
        self.model = module.TextDetection()
        state = torch.load(ASSETS/"dbnet-resnet34.ckpt", map_location="cpu", weights_only=True)
        self.model.load_state_dict(state.get("model", state))
        self.model.eval()
        self.decoder = decoder.SegDetectorRepresenter(thresh=.5, box_thresh=.75, unclip_ratio=2.3)
        self.size = size

    def predict(self, rgb):
        import cv2
        import torch
        started = time.perf_counter()
        height, width = rgb.shape[:2]
        scale = self.size/max(height, width)
        rh, rw = round(height*scale), round(width*scale)
        image = cv2.resize(cv2.bilateralFilter(rgb, 17, 80, 80), (rw, rh), interpolation=cv2.INTER_LINEAR)
        ph, pw = math.ceil(rh/256)*256, math.ceil(rw/256)*256
        padded = np.zeros((ph, pw, 3), np.uint8)
        padded[:rh, :rw] = image
        tensor = torch.from_numpy((padded.astype(np.float32)/127.5-1).transpose(2, 0, 1)[None])
        with torch.inference_mode():
            db, _ = self.model(tensor)
            probability = db.sigmoid().numpy()
        polygons, scores = self.decoder({"shape": [(ph, pw)]}, probability)
        valid = polygons[0].reshape(len(polygons[0]), -1).sum(axis=1) > 0 if len(polygons[0]) else []
        points = polygons[0][valid].astype(np.float64)/scale if len(polygons[0]) else []
        values = scores[0][valid] if len(scores[0]) else []
        boxes = records(points, values, width, height)
        return boxes, dict(model="manga-image-translator DBNet ResNet34", device="CPU",
                           seconds=round(time.perf_counter()-started, 3), input_size=[pw, ph],
                           text_threshold=.5, box_threshold=.75, unclip_ratio=2.3,
                           preprocessing="RGB / 127.5 - 1; bilateral; right/bottom padding to 256")


def paddle_postprocessor():
    """Load official NumPy polygon methods, omitting its unused Paddle import.

    We call boxes_from_bitmap directly with NumPy arrays, so the framework
    Tensor-conversion branch in __call__ is never needed. Algorithm unchanged.
    """
    path = ROOT/"vendor/paddleocr-det/db_postprocess.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tree.body = [node for node in tree.body if not
                 (isinstance(node, ast.Import) and any(alias.name == "paddle" for alias in node.names))]
    namespace = {"__name__": "paddle_numpy_postprocess"}
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace["DBPostProcess"](thresh=.2, box_thresh=.45, max_candidates=3000, unclip_ratio=1.4)


class PaddleTextDetector:
    def __init__(self, size=2048):
        import cv2
        import onnxruntime as ort
        cv2.setNumThreads(4)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        from ..model_backend import model_session
        self.net = model_session("paddle", options)
        self.decoder = paddle_postprocessor()
        self.size = size

    def predict(self, rgb):
        import cv2
        started = time.perf_counter()
        height, width = rgb.shape[:2]
        scale = self.size/max(height, width)
        rh, rw = max(32, round(height*scale/32)*32), max(32, round(width*scale/32)*32)
        # Model's own inference.yml specifies BGR and these ImageNet moments.
        bgr = cv2.resize(rgb[:, :, ::-1], (rw, rh), interpolation=cv2.INTER_LINEAR).astype(np.float32)/255
        tensor = ((bgr-np.array([.485, .456, .406], np.float32))/np.array([.229, .224, .225], np.float32))
        tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[None])
        output = self.net.run(None, {self.net.get_inputs()[0].name: tensor})[0]
        probability = output[0, 0]
        polygons, scores = self.decoder.boxes_from_bitmap(probability, probability > .2, width, height)
        boxes = records(polygons, scores, width, height)
        providers = getattr(self.net, "get_providers", lambda: ["CPUExecutionProvider"])()
        return boxes, dict(model="PP-OCRv6_medium_det ONNX", device="GPU" if providers[0] == "DmlExecutionProvider" else "CPU",
                           provider=providers[0], gpu_fallback=getattr(self.net,"fallback",""),
                           seconds=round(time.perf_counter()-started, 3), input_size=[rw, rh],
                           text_threshold=.2, box_threshold=.45, unclip_ratio=1.4,
                           preprocessing="BGR; ImageNet normalization; longest side 2048, rounded to 32")

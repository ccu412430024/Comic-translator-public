"""Isolated ONNX inference for learned text masks and alternative inpainting."""
from pathlib import Path
import time

import mahotas
import numpy as np
import onnxruntime as ort
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]


def session(path):
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    from ..model_backend import model_session
    model = "ctd" if Path(path).name == "ctd.onnx" else "aot"
    return model_session(model, options)


def backend(session):
    providers = getattr(session, "get_providers", lambda: ["CPUExecutionProvider"])()
    return dict(provider=providers[0], gpu_fallback=getattr(session, "fallback", ""), gpu_evictions=getattr(session, "evictions", 0))


class ComicTextMask:
    def __init__(self):
        self.net = session(ROOT/".runtime/upgrade-models/ctd.onnx")

    def predict(self, rgb):
        height, width = rgb.shape[:2]
        scale = min(1024/width, 1024/height)
        resized_w, resized_h = round(width*scale), round(height*scale)
        resized = Image.fromarray(rgb).resize((resized_w, resized_h), Image.Resampling.BILINEAR)
        # Official CTD pads black on the right/bottom, rather than YOLO's common symmetric padding.
        canvas = np.zeros((1024, 1024, 3), dtype=np.uint8)
        canvas[:resized_h, :resized_w] = np.asarray(resized)
        tensor = canvas.transpose(2, 0, 1)[None].astype(np.float32)/255
        started = time.perf_counter()
        probability = self.net.run(["seg"], {"images": tensor})[0][0, 0]
        probability = probability[:resized_h, :resized_w]
        probability = np.asarray(Image.fromarray(probability).resize((width, height), Image.Resampling.BILINEAR))
        return probability, {"seconds": round(time.perf_counter()-started, 3), "input_size": 1024,
                             **backend(self.net), "preprocessing_version": 2}


def restrict_text_mask(probability, blocks, threshold=.3, dilation=3):
    height, width = probability.shape
    allowed = np.zeros((height, width), bool)
    for block in blocks:
        if not block.get("translation", "").strip() or block.get("preserve_original"):
            continue
        x1, y1, x2, y2 = block["bbox"]
        margin = max(8, min(24, (y2-y1)//8))
        allowed[max(0, y1-margin):min(height, y2+margin), max(0, x1-margin):min(width, x2+margin)] = True
    mask = mahotas.dilate(probability > threshold, np.ones((dilation, dilation), bool)) & allowed
    return mask.astype(np.uint8)*255


class AOTInpainting:
    def __init__(self):
        self.net = session(ROOT/".runtime/upgrade-models/aot.onnx")

    def predict(self, rgb, mask):
        if not np.any(mask):
            return rgb.copy(), {"seconds": 0}
        height, width = rgb.shape[:2]
        padded_h = max(128, (height+7)//8*8)
        padded_w = max(128, (width+7)//8*8)
        image = np.pad(rgb, ((0, padded_h-height), (0, padded_w-width), (0, 0)), mode="reflect")
        padded_mask = np.pad(mask, ((0, padded_h-height), (0, padded_w-width)), mode="constant")
        binary = (padded_mask > 127).astype(np.float32)
        image = (image.astype(np.float32)/127.5-1)*(1-binary[:, :, None])
        names = self.net.get_inputs()
        started = time.perf_counter()
        output = self.net.run(None, {names[0].name: image.transpose(2, 0, 1)[None],
                                    names[1].name: binary[None, None]})[0][0].transpose(1, 2, 0)
        repaired = np.clip(np.round((output[:height, :width]+1)*127.5), 0, 255).astype(np.uint8)
        result = np.where((mask > 127)[:, :, None], repaired, rgb)
        return result, {"seconds": round(time.perf_counter()-started, 3), **backend(self.net),
                        "unmasked_changed_pixels": int(np.count_nonzero(np.any(result != rgb, axis=2) & (mask == 0)))}

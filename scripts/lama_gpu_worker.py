"""Persistent DirectML process: arrays travel over pipes, never page files."""
import base64
import json
from pathlib import Path
import sys

import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from comic_pipeline.gpu_devices import adapters, tested_adapter


def send(value):
    sys.stdout.buffer.write(json.dumps(value, separators=(",", ":")).encode()+b"\n")
    sys.stdout.buffer.flush()


def main():
    selected = tested_adapter(adapters())
    if selected is None or "DmlExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("Tested DirectML adapter unavailable")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.enable_mem_pattern = False
    options.log_severity_level = 3
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    session = ort.InferenceSession(str(ROOT/".runtime/ComicTranslate/models/inpainting/lama-manga-dynamic.onnx"),
        sess_options=options, providers=[("DmlExecutionProvider", {"device_id": selected["index"]}), "CPUExecutionProvider"])
    if "DmlExecutionProvider" not in session.get_providers():
        raise RuntimeError("DirectML session fell back to CPU")
    names = [item.name for item in session.get_inputs()]
    send(dict(ready=True, adapter=selected["name"], provider="DmlExecutionProvider"))
    while True:
        line = sys.stdin.buffer.readline(8*1024*1024)
        if not line:
            break
        try:
            request = json.loads(line)
            height, width = request["shape"]
            if not (0 < height <= 768 and 0 < width <= 768):
                raise ValueError("Crop exceeds bounded repair size")
            image = np.frombuffer(base64.b64decode(request["image"], validate=True), np.uint8).reshape(height, width, 3)
            mask = np.frombuffer(base64.b64decode(request["mask"], validate=True), np.uint8).reshape(height, width)
            pad = ((0, (-height) % 8), (0, (-width) % 8))
            image = np.pad(image, (*pad, (0, 0)), mode="symmetric")
            mask = np.pad(mask, pad, mode="symmetric")
            image_n = image.transpose(2, 0, 1).astype(np.float32)[None]/255
            mask_n = (mask > 0).astype(np.float32)[None, None]
            output = session.run(None, {names[0]: image_n, names[1]: mask_n})[0]
            if not np.isfinite(output).all():
                raise RuntimeError("Nonfinite model output")
            result = np.clip(output[0].transpose(1, 2, 0)*255, 0, 255).astype(np.uint8)[:height, :width]
            send(dict(image=base64.b64encode(result.tobytes()).decode()))
        except Exception as error:
            send(dict(error=type(error).__name__))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        send(dict(error=type(error).__name__))
        sys.exit(1)

"""Single-page SAM worker: NumPy input/output pipes, no page files or API keys."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def predict(image, prompts):
    import mahotas
    import torch
    from sam2.build_sam import build_sam2
    from sam2.sam2_image_predictor import SAM2ImagePredictor
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    model = build_sam2("configs/sam2.1/sam2.1_hiera_t.yaml", str(ROOT/".runtime/upgrade-models/sam2.1_hiera_tiny.pt"),
                       device="cpu", apply_postprocessing=False)
    predictor = SAM2ImagePredictor(model)
    arrays, entries = {}, []
    with torch.inference_mode():
        predictor.set_image(image)
        for block in prompts:
            x1, y1, x2, y2 = block["bubble_bbox"]
            predictions, scores, _ = predictor.predict(box=np.array([x1, y1, x2, y2]), multimask_output=True)
            raw = predictions[int(np.argmax(scores))].astype(bool)
            inside_pixels = int(raw[y1:y2, x1:x2].sum())
            fraction = inside_pixels/max(1, int(raw.sum()))
            # Cropped, eroded masks preserve the earlier experiment's safety
            # thresholds without returning a full-page mask for every bubble.
            selected = mahotas.erode(np.pad(raw[y1:y2, x1:x2], 1), np.ones((3, 3), bool))[1:-1, 1:-1]
            labels, count = mahotas.label(selected)
            sizes = np.bincount(labels.ravel(), minlength=count+1); sizes[0] = 0
            selected = labels == int(np.argmax(sizes)) if sizes.any() else selected
            a, b, c, d = block["bbox"]
            a, b, c, d = max(0, a-x1), max(0, b-y1), min(x2-x1, c-x1), min(y2-y1, d-y1)
            coverage = float(selected[b:d, a:c].mean()) if c > a and d > b else 0
            if fraction >= .8 and coverage >= .45 and selected.sum() >= 80:
                arrays[block["id"]] = selected
                entries.append(dict(id=block["id"], bbox=[x1, y1, x2, y2]))
    return arrays, dict(ok=True, masks=entries)


def main():
    try:
        with np.load(io.BytesIO(sys.stdin.buffer.read()), allow_pickle=False) as data:
            image, prompts = data["image"], json.loads(data["prompts"].tobytes())
        with redirect_stdout(sys.stderr):
            arrays, metadata = predict(image, prompts)
    except Exception:
        arrays, metadata = {}, dict(ok=False, masks=[])
    output = io.BytesIO()
    np.savez_compressed(output, metadata=np.frombuffer(json.dumps(metadata).encode(), np.uint8), **arrays)
    sys.stdout.buffer.write(output.getvalue())


if __name__ == "__main__":
    main()

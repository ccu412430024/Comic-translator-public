"""Headless integration with the pinned, unmodified Comic Translate checkout."""
from pathlib import Path
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_COMMIT = "8977b91a4f7a40c3917c5a268e9e7d78e1d818da"
_configured = False


def configure():
    global _configured
    if _configured:
        return
    vendor = ROOT / "vendor" / "comic-translate"
    if not (vendor / "modules").is_dir():
        raise RuntimeError("Missing vendor/comic-translate. Run scripts/setup.ps1 first.")
    sys.path.insert(0, str(vendor))
    # Redirect upstream model/config caches into this project, before registry import.
    from modules.utils import paths
    paths.get_user_data_dir = lambda app_name="ComicTranslate": str(ROOT / ".runtime" / app_name)
    _configured = True


def prepare_models(include_base=False):
    configure()
    from modules.utils.download import ModelDownloader, ModelID
    models = [ModelID.RTDETR_INT8_ONNX, ModelID.MANGA_OCR_MOBILE_ONNX, ModelID.LAMA_ONNX]
    if include_base:
        models.append(ModelID.MANGA_OCR_INT8_ONNX)
    for model in models:
        print(f"Preparing {model.value}", flush=True)
        ModelDownloader.get(model)


class Engines:
    def __init__(self, threshold=0.3, ocr_model="base", detection_mode="standard", tile_size=640, tile_overlap=.25):
        configure()
        self.threshold = threshold
        self.ocr_model = ocr_model
        self.detection_mode = detection_mode
        self.tile_size, self.tile_overlap = tile_size, tile_overlap
        self.last_detection = None
        self._detector = self._ocr = self._inpainter = self._paddle = None
        self._ocr_backend = None
        self._ocr_kind = None
        self.last_ocr = {}
        self._lama_cleaner = None
        self.last_cleanup = {}

    def detect(self, image):
        if self._detector is None:
            from modules.detection.rtdetr_v2_onnx import RTDetrV2ONNXDetection
            self._detector = RTDetrV2ONNXDetection()
            from .model_backend import measured_device, model_session
            if measured_device("rtdetr") == "dml":
                from modules.detection.rtdetr_v2_onnx import _make_rtdetr_session_options
                self._detector.confidence_threshold = self.threshold
                self._detector.session = model_session("rtdetr", _make_rtdetr_session_options())
            else:
                self._detector.initialize(device="cpu", confidence_threshold=self.threshold)
        if self.detection_mode == "paddle":
            from .experimental.text_detectors import PaddleTextDetector
            from .detection import group_paddle_lines
            from modules.utils.textblock import TextBlock
            if self._paddle is None:
                self._paddle = PaddleTextDetector()
            lines, details = self._paddle.predict(image)
            # RT-DETR supplies balloon geometry only; all text comes from PP-OCRv6.
            bubbles, _ = self._detector.image_slicer.process_slices_for_detection(
                image, self._detector._detect_single_image)
            grouped = group_paddle_lines(lines, bubbles)
            self.last_detection = dict(details, raw_lines=len(lines), grouped_blocks=len(grouped),
                                       bubble_provider=self._detector.session.get_providers()[0])
            return [TextBlock(text_bbox=np.asarray(item["bbox"]),
                              bubble_bbox=np.asarray(item["bubble_bbox"]) if item["bubble_bbox"] else None,
                              text_class=item["class"], direction=item["orientation"], font_color=(0, 0, 0))
                    for item in grouped]
        if self.detection_mode == "tiled":
            from .detection import detect_tiled
            blocks, self.last_detection = detect_tiled(self._detector, image, self.threshold,
                                                       self.tile_size, self.tile_overlap)
            self.last_detection.update(provider=self._detector.session.get_providers()[0], gpu_fallback=getattr(self._detector.session,"fallback",""))
            return blocks
        blocks = self._detector.detect(image)
        self.last_detection = dict(provider=self._detector.session.get_providers()[0], gpu_fallback=getattr(self._detector.session,"fallback",""))
        return blocks

    def recognize(self, image, blocks):
        # Use the recognizer directly: upstream process_image swallows model errors.
        crops = [image[b[1]:b[3], b[0]:b[2]] for b in (item["bbox"] for item in blocks)]
        if not crops:
            return []
        if self.ocr_model in {"nar", "paddle"}:
            from .ocr_backend import OCRBackend
            self._ocr_backend = self._ocr_backend or OCRBackend()
            values = self._ocr_backend.recognize(self.ocr_model, crops)
            self.last_ocr = dict(self._ocr_backend.info)
            return values
        if self._ocr_backend is not None:
            self._ocr_backend.release_gpu()
        if self._ocr is None or self._ocr_kind != self.ocr_model:
            if self.ocr_model == "mobile":
                from modules.ocr.manga_ocr.mobile.onnx_engine import MangaOCRMobileONNX
                self._ocr = MangaOCRMobileONNX()
            else:
                from modules.ocr.manga_ocr.onnx_engine import MangaOCRONNX
                self._ocr = MangaOCRONNX(device="cpu")
            self._ocr_kind = self.ocr_model
        self.last_ocr = dict(model=self.ocr_model, provider="CPUExecutionProvider")
        return self._ocr.process_batch(crops)

    def make_mask(self, image, blocks, dilation=3):
        import numpy as np
        import imkit as imk
        from modules.detection.utils.content import get_inpaint_mask
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        for block in blocks:
            if block.get("translation", "").strip():
                mask = np.maximum(mask, get_inpaint_mask(block["bbox"], image))
        if dilation > 1:
            mask = imk.dilate(mask, np.ones((dilation, dilation), dtype=np.uint8), iterations=1)
        return mask

    def _load_lama(self):
        if self._inpainter is None:
            from .lama_backend import LamaBackend
            def cpu():
                from modules.inpainting.lama import LaMa
                return LaMa("cpu", backend="onnx")
            self._inpainter = LamaBackend(cpu)
        return self._inpainter

    def clean_fast(self, image, mask, blocks=None):
        from .lama_cleanup import LamaCleaner
        self._lama_cleaner = self._lama_cleaner or LamaCleaner(self._load_lama)
        result = self._lama_cleaner.clean(image, mask, blocks)
        self.last_cleanup = dict(self._lama_cleaner.last_metrics)
        return result

    def clean(self, image, mask):
        return self.clean_fast(image, mask)

    def release_gpu(self):
        released = []
        if self._ocr_backend is not None:
            released.extend(self._ocr_backend.release_gpu())
        for name, session in (("rtdetr", getattr(self._detector, "session", None)),
                              ("paddle", getattr(self._paddle, "net", None)),
                              ("lama", self._inpainter)):
            if hasattr(session, "release_gpu") and session.release_gpu():
                released.append(name)
        return released

    def close(self):
        if self._ocr_backend is not None:
            self._ocr_backend.close()
        if self._inpainter is not None and hasattr(self._inpainter, "close"):
            self._inpainter.close()
        for model, attr in ((self._detector, "session"), (self._paddle, "net")):
            session = getattr(model, attr, None)
            if session is not None and hasattr(session, "close"):
                session.close()

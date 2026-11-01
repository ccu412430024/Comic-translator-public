"""Adapters to the pinned Comic Translate mask, inpaint and Qt export pipeline.

Only page capture/OCR/translation and preservation of skipped blocks belong to
the reader. No upstream source is changed, and no desktop account is created.
"""
import hashlib
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image

from .reader_options import ASSETS, ROOT, ProcessingError
from .upstream import configure


def text_blocks(blocks):
    configure()
    from modules.utils.textblock import TextBlock
    return [TextBlock(text_bbox=np.asarray(b["bbox"], dtype=np.int32),
                      bubble_bbox=np.asarray(b["bubble_bbox"], dtype=np.int32) if b.get("bubble_bbox") else None,
                      text_class=b.get("class", "text_bubble" if b.get("bubble_bbox") else "text_free"),
                      text=b.get("source_text", ""), translation=b.get("translation", ""),
                      source_lang="ja", target_lang="zh-TW", direction=b.get("orientation", ""),
                      font_color=tuple(b.get("font_color", (0, 0, 0))))
            for b in blocks if b.get("translation", "").strip() and not b.get("preserve_original")]


def make_mask(pixels, blocks):
    configure()
    from modules.utils.image_utils import generate_mask
    mask = generate_mask(pixels, text_blocks(blocks))
    # The reader intentionally does not translate punctuation-only/skipped boxes.
    for block in blocks:
        if block.get("preserve_original") or not block.get("translation", "").strip():
            x1, y1, x2, y2 = block["bbox"]
            mask[y1:y2, x1:x2] = 0
    return mask


def ensure_aot():
    """Reuse the already downloaded, hash-verified upstream weights, offline."""
    configure()
    from modules.utils.download import ModelDownloader, ModelID
    # primary_path() itself triggers a download; resolve the registered path
    # directly so reuse of our existing weights never attempts the network.
    spec = ModelDownloader.registry[ModelID.AOT_ONNX]
    name = spec.files[0]
    target = Path(spec.save_dir)/(spec.save_as.get(name, name) if spec.save_as else name)
    checksum = spec.sha256[0]
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
        source = ASSETS/"aot.onnx"
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != checksum:
            raise ProcessingError("請先準備 Comic Translate 原版 AOT 模型")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


class OriginalCleaner:
    def __init__(self):
        configure()
        from pipeline.inpainting import InpaintingHandler
        class Handler(InpaintingHandler):
            def _ensure_inpainter(self):
                if self.inpainter_cache is None:
                    ensure_aot()
                    from modules.inpainting.aot import AOT
                    self.inpainter_cache = AOT("cpu", backend="onnx")

            def _apply_fast_bubble_cleanup(self, *args):
                result = super()._apply_fast_bubble_cleanup(*args)
                self.fast_filled = result[2]
                self.residual_pixels = int(np.count_nonzero(result[1]))
                return result

            def _inpaint_full_image(self, *args):
                self.nn_routes += 1
                return super()._inpaint_full_image(*args)

            def _inpaint_by_patches(self, *args):
                self.nn_routes += 1
                return super()._inpaint_by_patches(*args)
        self.handler = Handler(None)
        self.last_metrics = {}

    def clean(self, pixels, mask, blocks):
        from modules.inpainting.schema import Config
        self.handler.fast_filled = self.handler.residual_pixels = self.handler.nn_routes = 0
        # ToolsPage defaults in the pinned desktop checkout: AOT / Resize / 960.
        config = Config(hd_strategy="Resize", hd_strategy_resize_limit=960)
        cleaned = self.handler.inpaint_image(pixels.copy(), mask, config, text_blocks(blocks))
        cleaned = np.clip(np.round(cleaned), 0, 255).astype(np.uint8)
        for block in blocks:
            if block.get("preserve_original") or not block.get("translation", "").strip():
                x1, y1, x2, y2 = block["bbox"]
                cleaned[y1:y2, x1:x2] = pixels[y1:y2, x1:x2]
        self.last_metrics = dict(fast_filled=self.handler.fast_filled,
                                 residual_pixels=self.handler.residual_pixels,
                                 nn_routes=self.handler.nn_routes, model="AOT ONNX", resize_limit=960)
        return cleaned


def render(pixels, blocks):
    """Qt must own its application's main thread, so render in a hidden process."""
    if not any(b.get("translation", "").strip() and not b.get("preserve_original") for b in blocks):
        return Image.fromarray(pixels.copy()), []
    request = io.BytesIO()
    np.savez(request, image=pixels, blocks=np.frombuffer(json.dumps(blocks).encode(), np.uint8))
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", HF_HUB_OFFLINE="1")
    environment.pop("GEMINI_API_KEY", None)
    try:
        result = subprocess.run([sys.executable, str(ROOT/"scripts/reader_comic_worker.py")],
            input=request.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=ROOT,
            timeout=180, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ProcessingError("Comic Translate Qt 排版失敗，請檢查上游排版依賴")
        with np.load(io.BytesIO(result.stdout), allow_pickle=False) as data:
            return Image.fromarray(data["image"].copy()), json.loads(data["metadata"].tobytes())
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ProcessingError("Comic Translate Qt 排版失敗或逾時，請檢查上游排版依賴") from exc


@contextmanager
def _vertical_dash_placement():
    """Correct dash orientation inside the isolated Qt worker, without editing upstream."""
    from app.ui.canvas.text.metrics import GlyphPlacement, PlacementRule
    from .rendering import VERTICAL_DASHES

    descriptor = GlyphPlacement.__dict__["get_rule"]
    original = GlyphPlacement.get_rule

    def rule(cls, char):
        if char in VERTICAL_DASHES:
            return PlacementRule.ROTATE_CENTERED, False, False
        return original(char)

    GlyphPlacement.get_rule = classmethod(rule)
    try:
        yield
    finally:
        GlyphPlacement.get_rule = descriptor


@_vertical_dash_placement()
def render_qt(pixels, blocks):
    """Use the actual upstream batch state builder and supersampled exporter."""
    from types import SimpleNamespace
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFontDatabase
    from modules.rendering.render import TextRenderingSettings
    from pipeline.webtoon_batch.render import RenderMixin
    from app.ui.canvas.save_renderer import ImageSaveRenderer
    from .rendering import default_font

    application = QApplication.instance() or QApplication([])
    font_id = QFontDatabase.addApplicationFont(str(default_font()))
    families = QFontDatabase.applicationFontFamilies(font_id)
    family = families[0] if families else "Microsoft JhengHei"
    settings = TextRenderingSettings(alignment_id=0, font_family=family, min_font_size=9,
        max_font_size=40, color="#000000", upper_case=False, outline=True,
        outline_color="#ffffff", outline_width="1", bold=False, italic=False, underline=False,
        line_spacing="1.0", direction=Qt.LayoutDirection.LeftToRight)
    state = {"target_lang": "Traditional Chinese"}
    main = SimpleNamespace(image_states={"memory": state}, render_settings=lambda: settings,
        button_to_alignment={0: Qt.AlignmentFlag.AlignCenter}, webtoon_mode=False,
        image_viewer=SimpleNamespace(webtoon_manager=None))
    context = SimpleNamespace(main_page=main, _get_page_scene_offset=lambda _: 0)
    # This pinned Qt vertical layout indexes UTF-16 offsets as Python character
    # offsets, and crashes on supplementary-plane characters. Keep those blocks
    # in our existing renderer rather than dropping or substituting characters.
    fallback = [b for b in blocks if b.get("orientation") == "vertical" and
                any(ord(c) > 0xffff for c in b.get("translation", "")) and not b.get("preserve_original")]
    upstream_blocks = text_blocks([b for b in blocks if b not in fallback])
    RenderMixin._store_page_text_items(context, 0, "memory", upstream_blocks, pixels.shape)
    exporter = ImageSaveRenderer(np.ascontiguousarray(pixels))
    exporter.add_state_to_image(state["viewer_state"])
    result = exporter.render_to_image()
    # The authentic exporter resamples its whole scene at 2x, including artwork.
    for block in blocks:
        if block.get("preserve_original") or not block.get("translation", "").strip():
            x1, y1, x2, y2 = block["bbox"]
            result[y1:y2, x1:x2] = pixels[y1:y2, x1:x2]
    metadata = [dict(font_size=item["font_size"], text=item["text"], position=item["position"],
                     width=item["width"], height=item["height"], vertical=item["vertical"], renderer="comic-translate-qt")
                for item in state["viewer_state"]["text_items_state"]]
    if fallback:
        from .rendering import render as render_fallback
        rendered, details = render_fallback(result, fallback, default_font())
        result = np.asarray(rendered)
        metadata.extend(dict(item, text=block["translation"], renderer="reader-unicode-fallback")
                        for item, block in zip(details, fallback))
    exporter.scene.clear()
    application.processEvents()
    return result, metadata

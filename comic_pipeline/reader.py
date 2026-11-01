"""Page processing with optional persistent work text; images and keys stay in RAM."""
from collections import OrderedDict
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from copy import deepcopy
import base64
import hashlib
import io
import os
import secrets
import threading
import time

import numpy as np
import requests
from PIL import Image, ImageOps

from .bubble_cleanup import punctuation_only, refine_bubbles
from .gemini import DEFAULT_MODEL, DEFAULT_RESEARCH_MODEL, MODEL_PRESETS, MAX_503_RETRIES, GeminiClient, GeminiError, GeminiCancelled, valid_model
from .gemini_rate import MAX_429_RETRIES,MAX_GENERATION_ATTEMPTS,MAX_RETRY_WAIT_SECONDS
from .pipeline import serialize_blocks
from .rendering import default_font
from .translation import GeminiTranslator, GoogleWebTranslator, TranslationError
from .upstream import Engines, configure
from .reader_options import CLEANING, LAYOUTS, ProcessingError, capabilities
from .reader_upgrades import ReaderUpgrades, remember
from .ocr_backend import OCR_MODELS, DEFAULT_OCR
from .page_identity import validate_identity
from .reader_chronology import cache_policy as chronology_policy, geometry
from .vertex import ConnectionStore,VertexClient,VertexCredentials,connection_key,storage_model


def gemini_client(settings,**options):
    if settings.get("gemini_backend")=="vertex":
        return VertexClient(project=settings["vertex_project"],location=settings["vertex_location"],
                            model=settings["model"],auth=settings.get("_vertex_auth"),**options)
    return GeminiClient(key=settings["key"],model=settings["model"],**options)
from .gemini_cache import GeminiCacheManager
from .memory_budget import MEMORY_TOKEN_BUDGET, MEMORY_TOKEN_CHOICES, CACHED_FIXED_BUDGET, SPLIT_CACHE_POLICY, memory_token_limit, bounded_context
from .prefetch import MAX_PREFETCH

MODES = {"standard": "RT-DETR", "tiled": "RT-DETR 分塊版", "paddle": "PP-OCRv6"}
MAX_IMAGE_BYTES = 12*1024*1024
MAX_PIXELS = 20_000_000


def decode_image(value):
    if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES*4//3+200:
        raise ValueError("圖片太大，請縮小擷取範圍")
    if value.startswith("data:"):
        header, separator, value = value.partition(",")
        if not separator or header not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"}:
            raise ValueError("請使用 PNG、JPEG 或 WebP 圖片")
    try:
        data = base64.b64decode(value, validate=True)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("圖片太大")
        with Image.open(io.BytesIO(data)) as opened:
            if opened.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("不支援的圖片格式")
            width, height = opened.size
            if min(width, height) < 16 or max(width, height) > 8192 or width*height > MAX_PIXELS:
                raise ValueError("圖片尺寸須在 16–8192 像素內，總像素不超過 2000 萬")
            return ImageOps.exif_transpose(opened).convert("RGB")
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("無法讀取這張圖片") from exc


def png_bytes(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class Canceled(Exception):
    pass


class ReaderProcessor:
    def release_gpu(self):
        with self.local_lock:
            return self.engine.release_gpu() + self.upgrades.release_gpu()

    def __init__(self, engine=None, translator_factory=None, memory=None):
        if engine is None:
            # Several ORT sessions coexist in the reader. Unbounded, spinning
            # thread pools otherwise starve its HTTP/UI thread during inpainting.
            configure()
            from modules.utils import onnx as upstream_onnx
            if not getattr(upstream_onnx.make_session_options, "reader_bounded", False):
                original_options = upstream_onnx.make_session_options
                def bounded_options(**kwargs):
                    options = original_options(**kwargs)
                    options.intra_op_num_threads = 4
                    options.inter_op_num_threads = 1
                    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
                    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
                    return options
                bounded_options.reader_bounded = True
                upstream_onnx.make_session_options = bounded_options
        self.engine = engine or Engines()
        self.translator_factory = translator_factory or self.make_translator
        self.upgrades = ReaderUpgrades()
        self.translations = OrderedDict()
        self.cleaned_pages = OrderedDict()
        self.original_cleaner = None
        self.memory = memory
        self.gemini_cache=GeminiCacheManager()
        self.local_lock=threading.RLock()

    def make_translator(self, provider, settings):
        if provider == "gemini":
            return GeminiTranslator(gemini_client(settings,cache_manager=self.gemini_cache,
                cache_mode=settings.get("gemini_cache","off"),service_tier=settings.get("gemini_tier","standard"),
                cancel_event=settings.get("_cancel_event"),on_retry=settings.get("_retry_progress"),on_status=settings.get("_gemini_progress")))
        return GoogleWebTranslator()

    def process(self, image, mode, provider, settings, progress):
        started = time.perf_counter()
        timings = {stage: 0.0 for stage in ("detection", "ocr", "translation", "mask", "clean", "bubble_refine", "layout", "encode")}
        with self.local_lock:
            pixels = np.asarray(image)
            cleaning, layout = settings.get("cleaning", "standard"), settings.get("layout", "contour")
            ocr_model = settings.get("ocr_model", DEFAULT_OCR)
            if ocr_model not in OCR_MODELS:
                raise ValueError("無效的 OCR 模型")
            memory_mode = mode if ocr_model == DEFAULT_OCR else mode+":ocr="+ocr_model
            memory_tokens = memory_token_limit(settings.get("memory_tokens", MEMORY_TOKEN_BUDGET))
            if settings.get("work_id") and memory_tokens != MEMORY_TOKEN_BUDGET:
                memory_mode += ":memory="+str(memory_tokens)
            split_cache = settings.get("gemini_cache") == "explicit" and bool(settings.get("work_id"))
            if split_cache:
                memory_mode += ":cache="+SPLIT_CACHE_POLICY
            if cleaning not in CLEANING or layout not in LAYOUTS:
                raise ValueError("無效的消字或排版方案")
            pixel_key = hashlib.sha256(pixels.tobytes()+str(image.size).encode()).hexdigest()
            identity = settings.get("page_identity")
            chronology=chronology_policy(identity) if settings.get("work_id") else ""
            if chronology:memory_mode+=":order="+chronology
            page_key = pixel_key
            if identity:
                from .work_memory import packed
                page_key = hashlib.sha256((pixel_key+packed(geometry(identity))).encode()).hexdigest()
            memory_model=storage_model(settings)
            translation_key = (page_key, mode, provider, memory_model, hashlib.sha256(settings["key"].encode()).hexdigest(), ocr_model)
            if identity:
                from .work_memory import packed
                translation_key += (packed(identity),)
            if chronology:translation_key+=(chronology,)
            if settings.get("catalogue_id"):
                translation_key += (settings["catalogue_id"], settings["catalogue_epoch"])
            selection = context = None
            segment = ""
            memory_delta = {}
            force = settings.get("force_translation", False)
            memory = settings.get("_memory_store", self.memory)
            if settings.get("_prefetch_id"):
                translation_key += (settings["_prefetch_id"],)
            if settings.get("work_id"):
                if memory is None or provider != "gemini":
                    raise ValueError("作品記憶需要 Gemini 翻譯引擎")
                selection, segment = memory.selection(settings["work_id"], settings.get("segment_id", ""))
                translation_key += (selection["id"], segment, selection["epoch"], selection["revision"], memory_tokens, SPLIT_CACHE_POLICY if split_cache else "combined", settings.get("force_token", ""))
                if not force and translation_key not in self.translations:
                    saved = memory.saved_translation(selection, segment, page_key, memory_mode, memory_model)
                    if saved is None and identity:
                        saved = memory.saved_translation(selection, segment, pixel_key, memory_mode, memory_model)
                    if saved is not None:
                        remember(self.translations, translation_key, saved, limit=4)
            if force:
                for candidate in list(self.translations):
                    if candidate == translation_key or selection and candidate[:-1] == translation_key[:-1]:
                        self.translations.pop(candidate, None)
            translation_cached = translation_key in self.translations
            tokens = 0
            acceleration=dict(cache_state="local_cache" if translation_cached else "not_used",cached_tokens=None,
                              requested_tier=settings.get("gemini_tier","standard"),actual_tier="not_requested",api_calls=0)
            if translation_cached:
                progress("translate", "重用本頁已完成的譯文", 50)
                blocks = deepcopy(self.translations[translation_key])
                if selection:
                    branch=settings.get("_prefetch")
                    context = branch.context_for(selection,segment,pixel_key,blocks,identity,settings["_prefetch_page"]) if branch else memory.context(selection, segment, pixel_key, blocks, identity,token_limit=memory_tokens,cache_fixed=split_cache)
            else:
                self.engine.detection_mode = mode
                self.engine.ocr_model = ocr_model
                progress("detect", "正在尋找文字", 10)
                phase = time.perf_counter()
                found = self.engine.detect(pixels)
                blocks = serialize_blocks(found, image.width, image.height)
                if mode == "tiled":
                    from .detection import preserve_baseline_ids
                    base = serialize_blocks([b for b in found if getattr(b, "detection_source", None) != "tile"], image.width, image.height)
                    preserve_baseline_ids(blocks, base)
                timings["detection"] = round(time.perf_counter()-phase, 4)
                progress("ocr", "正在辨識文字："+OCR_MODELS[ocr_model], 30)
                phase = time.perf_counter()
                texts = self.engine.recognize(pixels, blocks) if blocks else []
                timings["ocr"] = round(time.perf_counter()-phase, 4)
                if len(texts) != len(blocks):
                    raise RuntimeError("OCR returned an unexpected number of crops")
                for block, text in zip(blocks, texts):
                    block.update(source_text=text, translation="", preserve_original=False)
                    if punctuation_only(text):
                        block.update(translation=text, preserve_original=True)
                active = [b for b in blocks if b["source_text"].strip() and not b["preserve_original"]]
                progress("translate", "正在翻譯成繁體中文", 50)
                phase = time.perf_counter()
                translator = self.translator_factory(provider, settings)
                if selection:
                    progress("context", "正在整理本頁的作品資料與閱讀記憶", 45)
                    branch=settings.get("_prefetch")
                    context = branch.context_for(selection,segment,pixel_key,blocks,identity,settings["_prefetch_page"]) if branch else memory.context(selection, segment, pixel_key, blocks, identity,token_limit=memory_tokens,cache_fixed=split_cache)
                    if not hasattr(translator, "set_memory_context"):
                        raise ValueError("這個翻譯引擎尚未支援作品記憶")
                    translator.set_memory_context(context)
                if hasattr(translator, "set_page_image"):
                    progress("prepare", "正在準備本頁圖片與翻譯請求", 48)
                    translator.set_page_image(image)
            detection_metrics=dict(cached=True) if translation_cached else deepcopy(getattr(self.engine,"last_detection",None) or {})
            ocr_metrics=dict(cached=True) if translation_cached else deepcopy(getattr(self.engine,"last_ocr",None) or {})
        if not translation_cached:
            progress("translate", "正在準備送往翻譯服務", 50)
            values = translator.translate(active) if active else {}
            if context is not None:
                effective = getattr(translator,"effective_memory_context",None)
                context = effective if active and isinstance(effective,dict) else bounded_context(context,
                    "".join(b["source_text"] for b in blocks),stable_fixed=True,token_limit=memory_tokens)
            timings["translation"] = round(time.perf_counter()-phase, 4)
            progress("render_wait", "譯文已備妥，等待本機消字與排版", 60)
        with self.local_lock:
            progress("translate", "譯文已備妥，準備消字與排版", 60)
            if context is not None and translation_cached:
                # A local translation reuse performs no cloud request.
                context = bounded_context(context,stable_fixed=True,token_limit=memory_tokens)
            if not translation_cached:
                memory_delta = getattr(translator, "memory_delta", {})
                for block in active:
                    value = values.get(block["id"])
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError("翻譯服務沒有回傳完整譯文")
                    block["translation"] = value.strip()
                tokens = sum(item.get("usage", {}).get("totalTokenCount", 0) for item in getattr(translator, "requests", []))
                for request in getattr(translator,"requests",[]):
                    if "acceleration" in request:acceleration=request["acceleration"]
                if acceleration.get("api_calls"):
                    self.last_gemini_acceleration=dict(deepcopy(acceleration),model=settings["model"])
                if not selection:
                    remember(self.translations, translation_key, deepcopy(blocks), limit=4)
            active = [b for b in blocks if b["source_text"].strip() and not b["preserve_original"]]
            clean_key = (translation_key, cleaning)
            clean_cached = clean_key in self.cleaned_pages
            if clean_cached:
                progress("clean", "重用本頁已完成的消字底圖", 78)
                refined, cleanup_details = self.cleaned_pages[clean_key]
            else:
                progress("mask", "正在建立字跡遮罩："+CLEANING[cleaning], 65)
                phase = time.perf_counter()
                if cleaning == "comic-original":
                    from .comic_original import make_mask, OriginalCleaner
                    mask = make_mask(pixels, blocks)
                    self.original_cleaner = self.original_cleaner or OriginalCleaner()
                else:
                    mask = self.upgrades.mask(pixels, blocks, cleaning, self.engine, page_key)
                timings["mask"] = round(time.perf_counter()-phase, 4)
                progress("clean", "正在消字："+CLEANING[cleaning], 70)
                phase = time.perf_counter()
                cleanup_details = {}
                if cleaning == "comic-original":
                    refined = self.original_cleaner.clean(pixels, mask, blocks)
                    cleanup_details = dict(self.original_cleaner.last_metrics)
                    timings["clean"] = round(time.perf_counter()-phase, 4)
                else:
                    cleaned = self.upgrades.clean(pixels, mask, cleaning, self.engine, blocks)
                    cleanup_details = dict(self.upgrades.last_cleaning)
                    cleanup_details["mask_model"] = dict(self.upgrades.last_mask)
                    timings["clean"] = round(time.perf_counter()-phase, 4)
                    phase = time.perf_counter()
                    refined, _, _ = refine_bubbles(pixels, cleaned, blocks)
                    timings["bubble_refine"] = round(time.perf_counter()-phase, 4)
                remember(self.cleaned_pages, clean_key, (refined, cleanup_details))
            sam = dict(requested=0, accepted=0)
            masks = None
            if layout == "sam":
                progress("segment", "正在用 SAM 2.1 分割氣泡", 83)
                masks, sam = self.upgrades.sam_masks(pixels, blocks, page_key)
            progress("render", "正在排版", 90)
            phase = time.perf_counter()
            upstream_layout = {}
            if layout == "comic-original":
                from .comic_original import render
                result, rendered = render(refined, blocks)
                upstream_layout = dict(blocks=len(rendered), unicode_fallback=sum(
                    item.get("renderer") == "reader-unicode-fallback" for item in rendered))
            elif layout == "rectangle":
                from .rendering import render
                result, _ = render(refined, [b for b in blocks if not b["preserve_original"]], default_font())
            else:
                from .experimental_contour import render_contour
                result, rendered = render_contour(refined, blocks, default_font(), external_masks=masks)
                if layout == "sam":
                    sam["used"] = sum(item.get("adopted", False) and item.get("reason") == "sam2-contour" for item in rendered)
            timings["layout"] = round(time.perf_counter()-phase, 4)
            phase = time.perf_counter()
            encoded = png_bytes(result)
            timings["encode"] = round(time.perf_counter()-phase, 4)
            progress("complete", "翻譯完成", 100)
            adoption = bool(selection and translation_cached and identity and memory.needs_page_adoption(selection,segment,identity))
            commit = dict(selection=selection, segment=segment, page=page_key, context=context, blocks=blocks,
                          delta=memory_delta, mode=memory_mode, model=memory_model, translation_key=translation_key,
                          page_identity=identity, image_size=image.size, legacy_page=pixel_key,
                          adopt_cached=adoption) if selection and context is not None and (not translation_cached or adoption) else None
            return dict(image=encoded, blocks=blocks, _memory_commit=commit,
                        metrics=dict(mode=mode, mode_name=MODES[mode], blocks=len(blocks), translated_blocks=len(active),
                                     seconds=round(time.perf_counter()-started, 2), gemini_tokens=tokens,
                                     gemini_acceleration=acceleration,
                                     cleaning=cleaning, cleaning_name=CLEANING[cleaning], layout=layout, layout_name=LAYOUTS[layout],
                                     translation_cached=translation_cached, cleaning_cached=clean_cached, sam=sam,
                                     timings=timings, cleanup=cleanup_details,
                                     ocr_model=ocr_model, ocr_name=OCR_MODELS[ocr_model],
                                     ocr=ocr_metrics,detection=detection_metrics,
                                     upstream_cleanup=cleanup_details if cleaning == "comic-original" else {}, upstream_layout=upstream_layout,
                                     translator=provider, model=settings["model"] if provider == "gemini" else "",gemini_backend=settings.get("gemini_backend","developer"),
                                     memory=dict(enabled=bool(selection), work_id=selection["id"] if selection else "",
                                         work_title=selection["title"] if selection else "", segment_id=segment,
                                         profile_revision=selection["revision"] if selection else 0,
                                         budget=context.get("budget",{}) if context else {},
                                         page_identity=identity,
                                         recent_pages=len(context["dynamic"].get("recent_pages", [])) if context else 0),
                                     width=image.width, height=image.height))


@dataclass
class Job:
    id: str
    state: str = "queued"
    stage: str = "queued"
    message: str = "等待處理"
    progress: int = 0
    canceled: bool = False
    cancel_event: object = field(default_factory=threading.Event,repr=False)
    image: bytes = field(default=b"", repr=False)
    metrics: dict = field(default_factory=dict)
    error: str = ""
    cache_group: str = field(default="", repr=False)
    catalogue_id: str = field(default="", repr=False)
    work_id: str = field(default="", repr=False)
    created_at: float = field(default_factory=time.monotonic,repr=False)
    stage_started_at: float = field(default_factory=time.monotonic,repr=False)
    finished_at: float | None = field(default=None,repr=False)
    failure: dict = field(default_factory=dict)

    def stop(self):
        self.canceled=True
        self.cancel_event.set()

    def public(self):
        now = self.finished_at if self.finished_at is not None else time.monotonic()
        elapsed = max(0, int(now-self.created_at))
        stage_seconds = max(0, int(now-self.stage_started_at))
        message = self.message
        if self.state == "running" and self.stage in {"translate", "retry"}:
            message += f" · 已等待 {stage_seconds} 秒"
            if stage_seconds >= 60:
                message += " · 回應較慢，可停止後重試"
        return dict(id=self.id, state=self.state, stage=self.stage, message=message,
                    progress=self.progress, metrics=self.metrics, error=self.error,
                    elapsed_seconds=elapsed, stage_seconds=stage_seconds, failure=self.failure,
                    image_url=f"/api/jobs/{self.id}/image" if self.state == "done" else None)


class ReaderService:
    def __init__(self, processor=None, cache_size=12, memory=None, researcher_factory=None, connection_path=None, story_client_factory=None):
        from .work_memory import WorkMemory
        self.memory = memory or WorkMemory(None if processor is None else ":memory:")
        from .library import WorkLibrary
        self.library = WorkLibrary(self.memory)
        if processor is None and memory is None:
            self.memory.recover_interrupted_research()
        self.processor = processor or ReaderProcessor()
        if isinstance(self.processor, ReaderProcessor):
            self.processor.memory = self.memory
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comic-reader")
        self.prefetch_pool = ThreadPoolExecutor(max_workers=MAX_PREFETCH, thread_name_prefix="comic-prefetch")
        self.research_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comic-research")
        self.researcher_factory = researcher_factory
        self.research_pending = 0
        self.lock = threading.RLock()
        self.idle=threading.Condition(self.lock)
        self.jobs = OrderedDict()
        self.cache = OrderedDict()
        self.cache_size = cache_size
        self.prefetches = OrderedDict()
        self._key = os.getenv("GEMINI_API_KEY", "").strip()
        self._model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
        if connection_path is None and processor is None and memory is None:
            from .vertex import ROOT
            connection_path=ROOT/".runtime/gemini-connection.json"
        self.connection_store=ConnectionStore(connection_path)
        self.connection=self.connection_store.load()
        self.vertex_auth=VertexCredentials()
        self.gpu_epoch = 0
        self._gpu_status = dict(state="ready", message="GPU 模型依需要載入。", unloaded_models=[])
        from .recent_story import RecentStoryUpdates
        self.story_updates = RecentStoryUpdates(self, story_client_factory or (lambda settings: gemini_client(settings)))

    def settings(self):
        from .model_backend import gpu_residency, measured_device
        with self.lock:
            auth=self.vertex_auth.status();vertex=self.connection["gemini_backend"]=="vertex"
            ready=bool(self.connection["vertex_project"] and auth["ready"]) if vertex else bool(self._key)
            return dict(has_gemini_key=bool(self._key),gemini_ready=ready,vertex_auth=auth,**self.connection, model=self._model, modes=MODES,
                        ocr_models=OCR_MODELS, default_ocr_model=DEFAULT_OCR,
                        gemini_models=deepcopy(MODEL_PRESETS),
                        cleaning_modes=CLEANING, layout_modes=LAYOUTS, capabilities=capabilities(),
                        default_translator="gemini", credentials_saved_to_disk=False,
                        renderer="selectable-processing-v10-vertical-dashes", inference_policy={name:measured_device(name) for name in ("rtdetr","paddle","ctd","aot")}, gpu_residency=gpu_residency(),
                        gpu_release=True, gpu_epoch=self.gpu_epoch, gpu_state=self.gpu_status(),
                        work_memory=True, bookwalker_prefetch=True, bookwalker_prefetch_version=2,reader_prefetch=True,reader_platforms=["bookwalker","pixiv"],reader_adapter_version=1,prefetch_parallelism_max=MAX_PREFETCH,prefetch_count_max=MAX_PREFETCH,
                        memory_persistence="local-sqlite-text-only", library_management=True, recent_story=True, rolling_story_outline=True,
                        pixiv_chapter_order=True,default_research_model=DEFAULT_RESEARCH_MODEL,
                        memory_budget=dict(limit_estimated_tokens=MEMORY_TOKEN_BUDGET,choices=list(MEMORY_TOKEN_CHOICES),
                            cached_fixed_limit_estimated_tokens=CACHED_FIXED_BUDGET,split_cache_policy=SPLIT_CACHE_POLICY,estimator="utf8-bytes-conservative"),
                        stable_bookwalker_pages=True,gemini_acceleration=True,gemini_cache=self.cloud_cache_status(),
                        gemini_transport="google-genai",gemini_retry=dict(status_codes=[429,503],maximum_retries=MAX_503_RETRIES,delays_seconds=[0]*MAX_503_RETRIES,
                            mode="status-aware",rate_limit_retries=MAX_429_RETRIES,maximum_generation_attempts=MAX_GENERATION_ATTEMPTS,
                            maximum_wait_seconds=MAX_RETRY_WAIT_SECONDS,shared_cooldown=True))

    def cloud_cache_status(self):
        manager=getattr(self.processor,"gemini_cache",None)
        value=manager.status() if isinstance(manager,GeminiCacheManager) else dict(active=0,pending_deletions=0,ttl_seconds=600,maximum=4)
        last=getattr(self.processor,"last_gemini_acceleration",None)
        if isinstance(last,dict):value["last_translation"]=deepcopy(last)
        return value

    def clear_cloud_cache(self):
        manager=getattr(self.processor,"gemini_cache",None)
        return manager.clear() if isinstance(manager,GeminiCacheManager) else dict(deleted=0,pending_deletions=0,message="目前沒有已追蹤的雲端快取。")

    def gpu_status(self):
        with self.lock:
            return dict(deepcopy(self._gpu_status), gpu_epoch=self.gpu_epoch)

    def _check_gpu_request(self, payload):
        if self._gpu_status["state"] != "ready":
            raise ValueError("GPU 已停止使用或正在釋放，請重新按「開始翻譯」。")
        epoch = payload.get("gpu_epoch", 0)
        if type(epoch) is not int or epoch != self.gpu_epoch:
            raise ValueError("翻譯工作已因釋放 GPU 而停止，請重新開始。")

    def release_gpu(self):
        with self.lock:
            if self._gpu_status["state"] in {"releasing", "released"}:
                return self.gpu_status()
            self.gpu_epoch += 1
            self._gpu_status = dict(state="releasing", message="已停止全部翻譯，等待目前步驟結束後釋放 GPU…", unloaded_models=[])
            for job in self.jobs.values():
                if job.state in {"queued", "running"}:
                    job.stop()
            for session in self.prefetches.values():
                session.active = False
            # Serialize unloading behind the active inference. New and stale
            # uploads stay blocked until the user explicitly starts again.
            self.pool.submit(self._release_gpu_models)
            return self.gpu_status()

    def _release_gpu_models(self):
        try:
            with self.idle:
                while any(job.state=="running" for job in self.jobs.values()):self.idle.wait()
            unload = getattr(self.processor, "release_gpu", lambda: [])
            released = unload()
            with self.lock:
                for session in self.prefetches.values():
                    session.close()
                self._gpu_status = dict(state="released", message="GPU 模型已釋放；下次開始翻譯時會重新載入。", unloaded_models=released)
        except Exception as error:
            with self.lock:
                self._gpu_status = dict(state="error", message=f"GPU 釋放未完成（{type(error).__name__}），請重試。", unloaded_models=[])

    def resume_gpu(self, payload):
        with self.lock:
            epoch = payload.get("gpu_epoch", 0)
            if type(epoch) is not int or epoch != self.gpu_epoch:
                raise ValueError("GPU 狀態已變更，請重新按「開始翻譯」。")
            if self._gpu_status["state"] == "releasing":
                raise ValueError("正在釋放 GPU，完成後再開始翻譯。")
            if self._gpu_status["state"] == "error":
                raise ValueError("GPU 釋放未完成，請先重試「釋放 GPU」。")
            self._gpu_status = dict(state="ready", message="GPU 模型依需要載入。", unloaded_models=[])
            return self.gpu_status()

    def library_runtime(self, identifier=None):
        with self.lock:
            jobs = [dict(job.public(), catalogue_id=job.catalogue_id or job.work_id,
                         image_bytes=len(job.image)) for job in self.jobs.values()
                    if identifier is None or identifier in {job.catalogue_id, job.work_id}]
            sessions = [dict(id=s.id, active=s.active, pages=len(s.entries),
                             catalogue_id=s.options.get("catalogue_id") or s.options.get("work_id", ""))
                        for s in self.prefetches.values() if s.active and (identifier is None or identifier in
                            {s.options.get("catalogue_id"), s.options.get("work_id")})]
            return dict(jobs=jobs, sessions=sessions, image_bytes=sum(j["image_bytes"] for j in jobs))

    def _discard_work_runtime(self, identifier):
        # Called under the service lock after the SQL transaction succeeds.
        for session in list(self.prefetches.values()):
            if identifier in {session.options.get("work_id"), session.options.get("catalogue_id")}:
                self.cancel_prefetch(session.id)
        for key, job in list(self.jobs.items()):
            if identifier in {job.catalogue_id, job.work_id}:
                job.stop();job.image=b""
                self.jobs.pop(key, None)
        for signature, key in list(self.cache.items()):
            if key not in self.jobs:
                self.cache.pop(signature, None)
        # Local stages and cache access share a lock; remote requests may overlap.
        def clear_processor():
            with getattr(self.processor,"local_lock",nullcontext()):
                for name in ("translations", "cleaned_pages"):
                    cache = getattr(self.processor, name, {})
                    for key in list(cache):
                        nested = key[0] if name == "cleaned_pages" else key
                        if identifier in nested:
                            cache.pop(key, None)
        self.pool.submit(clear_processor)

    def manage_work(self, identifier, action, value):
        with self.lock:
            operations = {"update": self.library.update, "restore-profile": self.library.restore_profile,
                          "edit-page": self.library.edit_page,
                          "delete-page": lambda i,v: self.library.edit_page(i,v,delete=True),
                          "clear-memory": self.library.clear,
                          "clear-translations": lambda i,v: self.library.clear(i,v,translations=True),
                          "segment": self.library.segment, "delete": self.library.delete}
            if action == "clear-runtime":
                with self.memory.transaction() as db:
                    self.library._check(identifier, value)
                    self.library._invalidate(db, identifier)
                result = self.library.detail(identifier)
            elif action in operations:
                result = operations[action](identifier, value)
            else:
                raise ValueError("找不到這個管理功能")
            self._discard_work_runtime(identifier)
            return result

    def research_work(self, identifier, value):
        from .work_research import WorkResearcher
        from .research_identity import ResearchError
        with self.lock:
            key, model = self._key, value.get("model", DEFAULT_RESEARCH_MODEL)
            connection=self._connection_snapshot()
            organizer_model = value.get("organizer_model", self._model)
            method = value.get("method", "web")
            if method not in {"web","gemini"}:
                raise ValueError("無效的作品搜尋方式")
            if not key and connection["gemini_backend"]!="vertex":
                raise ValueError("請先套用 Gemini API 金鑰，再搜尋作品資料")
            if not valid_model(model) or not valid_model(organizer_model):
                raise ValueError("無效的建檔模型名稱")
            if self.research_pending >= 3:
                raise ValueError("作品建檔佇列已滿，請稍後再試")
            token = self.memory.start_research(identifier)
            if token is None:
                return self.memory.get(identifier)
            work = self.memory.get(identifier)
            self.research_pending += 1
        def run():
            try:
                client = gemini_client(dict(connection,key=key,model=model))
                researcher = self.researcher_factory(client) if self.researcher_factory else WorkResearcher(client, gemini_client(dict(connection,key=key,model=organizer_model)),method=method)
                profile, research, usage = researcher.build(work)
                self.memory.finish_research(identifier, token, profile, research, usage, model)
            except (GeminiError, ValueError) as error:
                # Only our deliberately safe validation/API messages leave this worker.
                message = str(error) if isinstance(error, (GeminiError,ResearchError)) else "作品資料整理或格式驗證失敗，可重新搜尋；這不代表書名或作者有誤。"
                self.memory.research_error(identifier, token, message)
            except Exception as error:
                self.memory.research_error(identifier, token, f"作品建檔失敗（{type(error).__name__}），請重試。")
            finally:
                with self.lock:
                    self.research_pending -= 1
        self.research_pool.submit(run)
        return self.memory.get(identifier)

    def create_work(self, value):
        from .library import text
        from .reader_platforms import catalogue_ref
        from .work_memory import normalized
        with self.lock:
            if not self._key and self.connection["gemini_backend"]!="vertex":
                raise ValueError("請先套用 Gemini API 金鑰，再建立作品")
            if self.research_pending >= 3:
                raise ValueError("作品建檔佇列已滿，請稍後再試")
            if not valid_model(value.get("model",DEFAULT_RESEARCH_MODEL)) or not valid_model(value.get("organizer_model",self._model)):
                raise ValueError("無效的建檔模型名稱")
            title, author, volume = (text(value.get(name, ""), limit, label, name=="title") for name,limit,label in
                                     (("title",300,"書名"),("author",200,"作者"),("volume",200,"卷數")))
            ref = value.get("ref", "")
            if not isinstance(ref, str) or ref and not catalogue_ref(ref):
                raise ValueError("書籍識別碼格式無效")
            if ref:
                bound = self.library.register(dict(ref=ref, title=title, author=author))
                candidates = [self.memory.get(bound["catalogue_id"])]
            else:
                candidates = [w for w in self.library.list() if not w["platforms"] and w["status"] in {"empty","error","interrupted"} and not w["revision"] and not w["pages"]
                          and normalized(w["title"]) == normalized(title) and (not w["author"] or not author or normalized(w["author"]) == normalized(author))
                          and (not w["volume"] or not volume or normalized(w["volume"]) == normalized(volume))]
            if candidates:
                # Opening a reader already creates a title-only catalogue. The
                # subsequent explicit research action fills it rather than
                # creating a duplicate, while keeping memory opt-in unchanged.
                old = candidates[0]
                work = self.library.update(old["id"], dict(version=old["data_version"], title=title, author=author or old["author"], volume=volume or old["volume"]))
            else:
                work = self.memory.create(title, author, volume)
            return self.research_work(work["id"], value)

    def configure(self, value):
        model = value.get("model", self._model)
        key = value.get("key", self._key)
        if not valid_model(model):
            raise ValueError("模型名稱無效")
        if not isinstance(key, str) or len(key) > 1024:
            raise ValueError("API 金鑰格式無效")
        with self.lock:
            connection=ConnectionStore.validate(dict(self.connection,**{k:value[k] for k in ("gemini_backend","vertex_project","vertex_location") if k in value}))
            changed_connection=connection!=self.connection
            self.connection_store.save(connection)
            self.connection=connection
            changed_key=self._key!=key.strip()
            self._model, self._key = model, key.strip()
        if changed_key or changed_connection:self.clear_cloud_cache()
        return self.settings()

    def _connection_snapshot(self,value=None):
        value=value or {}
        connection=ConnectionStore.validate(dict(self.connection,**{k:value[k] for k in ("gemini_backend","vertex_project","vertex_location") if k in value}))
        if connection["gemini_backend"]=="vertex":
            if not connection["vertex_project"]:raise ValueError("請先在設定頁填入 GCP 專案 ID")
            state=self.vertex_auth.status()
            if not state["ready"]:raise GeminiError(state["message"],code="vertex_auth_missing")
            connection["_vertex_auth"]=self.vertex_auth
        return connection

    def check_gemini(self, value):
        with self.lock:
            key, model = self._key, value.get("model", self._model)
            connection=self._connection_snapshot()
        if not valid_model(model):
            raise ValueError("模型名稱無效，請輸入 Gemini 模型 ID")
        if not key and connection["gemini_backend"]!="vertex":
            raise ValueError("請先在本機設定頁套用 Gemini API 金鑰")
        details = gemini_client(dict(connection,key=key,model=model)).check_model()
        if "generateContent" not in (details.get("supportedGenerationMethods") or []):
            raise GeminiError("這個模型不支援目前的翻譯 API，請選擇可處理文字與圖片的 Gemini 模型。")
        backend=connection["gemini_backend"]
        return dict(ok=True, model=model, gemini_tokens=0,gemini_backend=backend,
                    message=f"{'Vertex AI' if backend=='vertex' else 'Gemini'} 連線正常：{model}。未呼叫翻譯生成。")

    def create_prefetch(self, payload):
        from .prefetch import PrefetchSession
        with self.lock:
            self._check_gpu_request(payload)
            self._catalogue_selection(payload)
            self.library.assert_selection(payload.get("work_id", ""), payload.get("catalogue_id", ""), payload.get("book_ref", ""))
            for session in list(self.prefetches.values()):
                if time.monotonic()-session.touched > 1800:
                    self.cancel_prefetch(session.id)
            active = [s for s in self.prefetches.values() if s.active]
            if len(active) >= 2:
                self.cancel_prefetch(active[0].id)
            frozen=dict(payload)
            if payload.get("translator","gemini")=="gemini":
                frozen.update({k:v for k,v in self._connection_snapshot(payload).items() if not k.startswith("_")})
            session = PrefetchSession.create(self.memory, frozen)
            self.prefetches[session.id] = session
            # Discard inactive metadata only after every worker using it ended.
            for identifier, old in list(self.prefetches.items()):
                if not old.active and all(self.jobs.get(e["job"],Job("")).state not in {"queued","running"} for e in old.entries):
                    old.close()
                    self.prefetches.pop(identifier,None)
            return dict(id=session.id, limit=MAX_PREFETCH,parallelism=session.parallelism)

    def submit_prefetch(self, identifier, payload):
        with self.lock:
            session = self.prefetches.get(identifier)
            if not session or not session.active:
                raise ValueError("預翻譯已停止，請重新開始")
            page = payload.get("page_id", "")
            if not isinstance(page,str) or not page.isascii() or not page.isdigit() or not 0 < int(page) <= 100000:
                raise ValueError("預翻譯頁碼無效")
            existing = next((e for e in session.entries if e["page_id"]==page),None)
            if existing:
                job = self.jobs.get(existing["job"])
                if job:return job.public()
                raise ValueError("預翻譯已清除，請重新開始")
            pending = len(session.entries)-session.activated-1
            if pending>=MAX_PREFETCH or session.entries and (not 0 < int(page)-int(session.entries[-1]["page_id"]) <= 2):
                raise ValueError("請依頁序提交預翻譯，最多保留五個未閱讀畫面")
            identity=validate_identity(payload.get("page_identity"))
            if identity and identity["book_ref"]!=session.book_ref:
                raise ValueError("預翻譯原始頁面不屬於這本書")
            job = self.submit(dict(session.options,image=payload.get("image"),page_identity=identity,_prefetch_page=page), _prefetch=session)
            with session.lock:session.entries.append(dict(page_id=page,job=job["id"]))
            session.touched = time.monotonic()
            return job

    def activate_prefetch(self, identifier, payload):
        with self.lock:
            session = self.prefetches.get(identifier)
            if not session or payload.get("book_ref") != session.book_ref:
                raise ValueError("書籍或預翻譯已變更，請重新開始")
            self._catalogue_selection(session.options)
            entry = next((e for e in session.entries if e["page_id"]==payload.get("page_id")),None)
            job = self.jobs.get(entry["job"]) if entry else None
            if not job or job.state != "done":
                raise ValueError("預翻譯圖片已清除，請重新開始")
            session.activate(self.memory,payload.get("page_id"))
            return job.public()

    def cancel_prefetch(self, identifier):
        with self.lock:
            session = self.prefetches.get(identifier)
            if session:
                session.active = False
                for entry in session.entries:
                    self.cancel(entry["job"])
                if all(self.jobs.get(e["job"],Job("")).state not in {"queued","running"} for e in session.entries):
                    session.close()
            return dict(stopped=True)

    def submit(self, payload, _prefetch=None):
        mode, provider = payload.get("mode", "standard"), payload.get("translator", "gemini")
        if mode not in MODES or provider not in {"gemini", "google-web"}:
            raise ValueError("請選擇有效的偵測模式與翻譯引擎")
        cleaning, layout = payload.get("cleaning", "standard"), payload.get("layout", "contour")
        if cleaning not in CLEANING or layout not in LAYOUTS:
            raise ValueError("請選擇有效的消字與排版方案")
        available = capabilities()
        ocr_model = payload.get("ocr_model", DEFAULT_OCR)
        if not isinstance(ocr_model,str) or ocr_model not in OCR_MODELS:
            raise ValueError("請選擇有效的 OCR 模型")
        if ocr_model != DEFAULT_OCR and not available.get("ocr",{}).get(ocr_model,{}).get("available"):
            raise ValueError(available.get("ocr",{}).get(ocr_model,{}).get("reason") or "所選 OCR 模型尚未備妥")
        for group, selected in [("cleaning", cleaning), ("layout", layout)]:
            if not available[group][selected]["available"]:
                raise ValueError(available[group][selected]["reason"])
        with self.lock:
            self._check_gpu_request(payload)
            model = payload.get("model", self._model) if provider == "gemini" else self._model
            if not valid_model(model):
                raise ValueError("模型名稱無效，請輸入 Gemini 模型 ID")
            settings = dict(key=self._key, model=model, cleaning=cleaning, layout=layout, ocr_model=ocr_model)
            if provider=="gemini":
                settings.update(self._connection_snapshot(payload))
                if settings["gemini_backend"]=="vertex":settings["key"]=""
            cache_mode,tier=payload.get("gemini_cache","off"),payload.get("gemini_tier","standard")
            if not isinstance(cache_mode,str) or cache_mode not in {"off","explicit"} or not isinstance(tier,str) or tier not in {"standard","priority"}:
                raise ValueError("請選擇有效的 Gemini 快取與服務等級")
            settings.update(gemini_cache=cache_mode if provider=="gemini" else "off",gemini_tier=tier if provider=="gemini" else "standard")
            settings["memory_tokens"] = memory_token_limit(payload.get("memory_tokens", MEMORY_TOKEN_BUDGET))
            identity=validate_identity(payload.get("page_identity"))
            if identity:
                settings["page_identity"]=identity
            catalogue = self._catalogue_selection(payload)
            settings.update(catalogue)
            work_id, segment_id = payload.get("work_id", ""), payload.get("segment_id", "")
            if not isinstance(work_id, str) or not isinstance(segment_id, str) or max(len(work_id),len(segment_id)) > 64:
                raise ValueError("作品或閱讀段落格式無效")
            self.library.assert_selection(work_id, catalogue.get("catalogue_id", ""),
                                          identity["book_ref"] if identity else payload.get("book_ref", ""))
            if work_id:
                if provider != "gemini":
                    raise ValueError("作品記憶需要 Gemini 翻譯引擎")
                selected, segment_id = self.memory.selection(work_id, segment_id)
                settings.update(work_id=work_id, segment_id=segment_id, memory_signature=(selected["epoch"], selected["revision"]))
            force = payload.get("force_translation", False)
            if not isinstance(force, bool):
                raise ValueError("重新翻譯選項格式無效")
            if force:
                settings.update(force_translation=True, force_token=secrets.token_hex(12))
        if provider == "gemini" and not settings["key"] and settings.get("gemini_backend")!="vertex":
            raise ValueError("請先在設定中填入 Gemini API 金鑰，或選擇 Google 測試引擎")
        if _prefetch:
            settings.update(_memory_store=_prefetch.memory,_prefetch_id=_prefetch.id,_prefetch=_prefetch,_prefetch_page=payload.get("_prefetch_page"))
        image = decode_image(payload.get("image"))
        signature = hashlib.sha256(image.tobytes()+str((image.size, mode, ocr_model, provider, storage_model(settings), cleaning, layout,
                                   work_id, segment_id, catalogue, settings.get("memory_signature"),settings.get("force_token"),settings.get("_prefetch_id"),identity,settings["gemini_cache"],settings["gemini_tier"],settings["memory_tokens"] if work_id else None)).encode()+
                                   hashlib.sha256(settings["key"].encode()).digest()).hexdigest()
        group = hashlib.sha256(image.tobytes()+str((mode,ocr_model,provider,storage_model(settings),work_id,segment_id,settings.get("memory_signature"),identity)).encode()).hexdigest()
        with self.lock:
            self._check_gpu_request(payload)
            if catalogue:
                current = self._catalogue_selection(catalogue)
            if work_id and self.memory.get(work_id)["epoch"] != selected["epoch"]:
                raise ValueError("作品記憶已變更，請重新啟動翻譯。")
            previous = self.cache.get(signature)
            if previous and previous in self.jobs:
                self.cache.move_to_end(signature)
                return self.jobs[previous].public()
            if sum(job.state in {"queued", "running"} for job in self.jobs.values()) >= MAX_PREFETCH:
                raise ValueError("目前已有五張圖片等待處理，請稍後再試")
            job = Job(secrets.token_hex(12), cache_group=group, catalogue_id=catalogue.get("catalogue_id", ""), work_id=work_id)
            self.jobs[job.id] = job
            pool=self.prefetch_pool if _prefetch else self.pool
            pool.submit(self._run, job, image, mode, provider, settings, signature)
        return job.public()

    def _catalogue_selection(self, payload):
        identifier = payload.get("catalogue_id", "")
        if not isinstance(identifier, str) or len(identifier) > 64:
            raise ValueError("書庫作品識別碼無效")
        if not identifier:
            return {}
        work = self.memory.get(identifier)
        if payload.get("catalogue_epoch") and payload["catalogue_epoch"] != work["epoch"]:
            raise ValueError("作品資料已在管理頁變更，請重新開啟插件並啟動翻譯。")
        return dict(catalogue_id=identifier, catalogue_epoch=work["epoch"])

    def _run(self, job, image, mode, provider, settings, signature):
        branch=settings.get("_prefetch")
        with branch.gate if branch else nullcontext():
            self._run_job(job,image,mode,provider,settings,signature)

    def _run_job(self, job, image, mode, provider, settings, signature):
        speculative = settings.get("_prefetch")
        committed=[]
        def progress(stage, message, percent):
            with self.lock:
                if job.canceled:
                    raise Canceled()
                if (job.stage,job.message)!=(stage,message):job.stage_started_at=time.monotonic()
                job.state, job.stage, job.message, job.progress = "running", stage, message, percent
        def retry_progress(event):
            attempt=f"{event['attempt']}／{event['maximum']}"
            if event.get("status")==429:
                message=(f"Gemini 429 暫時受限，共用冷卻 {event['remaining_seconds']} 秒"+
                         (f"（重試 {attempt}）" if event["attempt"] else "")) if event.get("state")=="waiting" else f"Gemini 429，正在重新送出（{attempt}）"
            else:message=f"Gemini 暫時不可用（503），正在立即重試（{attempt}）"
            progress("retry",message,50)
        def gemini_progress(event):
            if event["phase"] == "auth":
                progress("vertex_auth", "正在準備 Vertex AI 本機登入憑證", 49)
            elif event["phase"] == "response":
                progress("validate", "已收到 Gemini 回應，正在核對譯文與記憶格式", 58)
            elif event["phase"] == "cache":
                progress("cloud_cache", "正在檢查、建立或續期 Gemini 固定提示詞快取", 49)
            elif event["phase"]=="request":
                if event["retry_count"]:
                    status=event.get("retry_status",503)
                    retry_progress(dict(status=status,attempt=event.get("retry_429" if status==429 else "retry_503",event["retry_count"]),
                                        maximum=MAX_429_RETRIES if status==429 else MAX_503_RETRIES,state="requesting"))
                else:progress("translate", "已送出 Gemini 請求，正在等待譯文與記憶更新", 50)
        settings.update(_cancel_event=job.cancel_event,_retry_progress=retry_progress,_gemini_progress=gemini_progress)
        try:
            progress("detect", "正在準備翻譯", 1)
            result = self.processor.process(image, mode, provider, settings, progress)
            with self.lock:
                if job.canceled:
                    raise Canceled()
                commit = result.get("_memory_commit")
                if speculative:
                    try:committed=speculative.complete(settings["_prefetch_page"],commit)
                    except ValueError:
                        raise ProcessingError("預翻譯的暫存記憶更新失敗，已停止後續畫面。") from None
                elif commit:
                    try:
                        saved = self.memory.commit_page(commit)
                        result["metrics"]["memory"]["saved"] = saved
                        if saved:committed.append(commit)
                    except Exception:
                        result["metrics"]["memory"].update(saved=False, error="譯圖已完成，但本機記憶儲存失敗。")
                if speculative:
                    result["metrics"]["prefetch"] = dict(session_id=speculative.id,page_id=settings["_prefetch_page"],staged=True,parallelism=speculative.parallelism)
                    result["metrics"].setdefault("memory",{}).update(saved=False,staged=True)
                job.image, job.metrics = result["image"], result["metrics"]
                job.state, job.stage, job.message, job.progress = "done", "complete", "翻譯完成", 100
                if settings.get("force_translation"):
                    for signature_key, identifier in list(self.cache.items()):
                        if self.jobs.get(identifier) and self.jobs[identifier].cache_group == job.cache_group:
                            self.cache.pop(signature_key, None)
                self.cache[signature] = job.id
                while len(self.cache) > self.cache_size:
                    _, identifier = self.cache.popitem(last=False)
                    self.jobs.pop(identifier, None)
            # Never take local_lock while holding the service lock: inference
            # progress reports acquire those locks in the opposite direction.
            if isinstance(self.processor,ReaderProcessor):
                with self.processor.local_lock:
                    for value in committed:
                        remember(self.processor.translations,value["translation_key"],deepcopy(value["blocks"]),limit=4)
        except (Canceled,GeminiCancelled):
            with self.lock:
                job.state, job.message = "canceled", "已停止這張圖片"
        except Exception as exc:
            # No arbitrary exception strings, HTTP URLs, OCR text or credentials.
            if isinstance(exc, (GeminiError, ProcessingError, TranslationError)):
                safe = str(exc)
            elif isinstance(exc, requests.RequestException):
                safe = "無法連接翻譯服務，請檢查網路與所選翻譯引擎後重試"
            else:
                safe = f"處理失敗（{type(exc).__name__}），請檢查服務與模型設定後重試"
            with self.lock:
                # Retain only a code location, never exception values or locals.
                location={}
                trace=exc.__traceback__
                while trace:
                    name=trace.tb_frame.f_code.co_filename.replace("\\","/")
                    if "/comic_pipeline/" in name:
                        location=dict(module=name.rsplit("/",1)[-1],line=trace.tb_lineno)
                    trace=trace.tb_next
                code=exc.code if isinstance(exc,(GeminiError,TranslationError)) and exc.code else type(exc).__name__
                job.failure=dict(code=code,stage=job.stage,**location)
                if isinstance(exc,GeminiError):job.failure.update(http_status=exc.status_code,quota=exc.quota)
                job.state, job.error, job.message = "error", safe, "翻譯失敗"
        finally:
            settings.clear()
            with self.idle:
                job.finished_at=time.monotonic()
                if speculative:
                    if job.state in {"error","canceled"}:
                        speculative.active=False
                        for entry in speculative.entries:self.cancel(entry["job"])
                    if not speculative.active and all(self.jobs.get(e["job"],Job("",state="canceled")).state not in {"queued","running"} for e in speculative.entries):
                        speculative.close()
                ended = [key for key, value in self.jobs.items() if value.state in {"error", "canceled"}]
                for identifier in ended[:-8]:
                    self.jobs.pop(identifier, None)
                self.idle.notify_all()

    def get(self, identifier):
        with self.lock:
            job = self.jobs.get(identifier)
            return job.public() if job else None

    def result_image(self, identifier):
        with self.lock:
            job = self.jobs.get(identifier)
            return job.image if job and job.state == "done" else None

    def cancel(self, identifier):
        with self.lock:
            job = self.jobs.get(identifier)
            if job and job.state in {"queued", "running"}:
                job.stop()
            return job.public() if job else None

    def close(self):
        with self.lock:
            for job in self.jobs.values():
                job.stop()
        self.story_updates.close()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.prefetch_pool.shutdown(wait=True, cancel_futures=True)
        self.research_pool.shutdown(wait=True, cancel_futures=True)
        for session in self.prefetches.values():
            session.close()
        engine = getattr(self.processor, "engine", None)
        if engine is not None and hasattr(engine, "close"):
            engine.close()
        upgrades = getattr(self.processor, "upgrades", None)
        if upgrades is not None and hasattr(upgrades, "close"):
            upgrades.close()
        self.clear_cloud_cache()
        self.memory.close()

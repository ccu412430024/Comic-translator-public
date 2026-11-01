from collections import Counter
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from comic_pipeline.reader import ReaderProcessor, ReaderService
from comic_pipeline.reader_options import CLEANING, LAYOUTS
from comic_pipeline.reader_upgrades import ReaderUpgrades
from test_reader import FakeProcessor, payload, finished


class FakeEngine:
    def __init__(self):
        self.calls = Counter()
    def detect(self, pixels):
        self.calls["detect"] += 1
        return [SimpleNamespace(xyxy=[50, 30, 130, 200], bubble_xyxy=[10, 5, 170, 235],
                                direction="vertical", font_color=[0, 0, 0], text_class="text_bubble")]
    def recognize(self, pixels, blocks):
        self.calls["ocr"] += 1
        return ["明日会おう。"]
    def make_mask(self, pixels, blocks):
        return np.zeros(pixels.shape[:2], np.uint8)
    def clean(self, pixels, mask):
        self.calls["clean"] += 1
        return pixels.copy()


class ProcessingTests(unittest.TestCase):
    def test_model_switch_retranslates_and_returning_to_same_model_reuses_its_translation(self):
        calls = Counter()
        class Translator:
            requests = [dict(usage=dict(totalTokenCount=20))]
            def translate(self, blocks):
                calls["translate"] += 1
                return {b["id"]: "明天見。" for b in blocks}
        processor = ReaderProcessor(FakeEngine(), lambda *_: Translator())
        image = Image.new("RGB", (180, 240), "white")
        def run(model):
            return processor.process(image, "standard", "gemini", dict(key="test", model=model,
                cleaning="standard", layout="rectangle"), lambda *_: None)
        first = run("gemini-3.5-flash-lite")
        second = run("gemini-3.6-flash")
        repeated = run("gemini-3.5-flash-lite")
        self.assertEqual(calls["translate"], 2)
        self.assertFalse(first["metrics"]["translation_cached"])
        self.assertFalse(second["metrics"]["translation_cached"])
        self.assertTrue(repeated["metrics"]["translation_cached"])
        self.assertEqual(repeated["metrics"]["gemini_tokens"], 0)
        self.assertEqual(second["metrics"]["model"], "gemini-3.6-flash")
        self.assertEqual(repeated["metrics"]["model"], "gemini-3.5-flash-lite")

    def test_cleanup_and_layout_switches_reuse_translation_and_cleaned_pages(self):
        calls = Counter()
        class Translator:
            requests = [dict(usage=dict(totalTokenCount=20))]
            def translate(self, blocks):
                calls["translate"] += 1
                return {b["id"]: "明天和你在這裡見面，好嗎？" for b in blocks}
        engine = FakeEngine()
        processor = ReaderProcessor(engine, lambda *_: Translator())
        image = Image.new("RGB", (180, 240), "#888888")
        ImageDraw.Draw(image).ellipse((10, 5, 170, 235), fill="white", outline="black", width=3)
        base = dict(key="", model="test")
        def run(cleaning="standard", layout="contour", provider="test"):
            return processor.process(image, "standard", provider, dict(base, cleaning=cleaning, layout=layout), lambda *_: None)
        first = run()
        rectangle = run(layout="rectangle")
        self.assertEqual(first["metrics"]["gemini_tokens"], 20)
        self.assertEqual(rectangle["metrics"]["gemini_tokens"], 0)
        self.assertTrue(rectangle["metrics"]["translation_cached"])
        self.assertTrue(rectangle["metrics"]["cleaning_cached"])
        self.assertEqual(calls["translate"], 1)
        self.assertEqual(engine.calls["detect"], 1)
        self.assertEqual(engine.calls["clean"], 1)
        self.assertEqual(Image.open(io.BytesIO(first["image"])).size, image.size)
        with patch.object(processor.upgrades, "mask", return_value=np.zeros((240, 180), np.uint8)) as mask, \
             patch.object(processor.upgrades, "clean", return_value=np.asarray(image)) as clean:
            result = run(cleaning="ctd-aot")
            self.assertEqual(mask.call_args.args[2], "ctd-aot")
            self.assertEqual(clean.call_args.args[2], "ctd-aot")
            self.assertTrue(result["metrics"]["translation_cached"])
            self.assertFalse(result["metrics"]["cleaning_cached"])
        with patch.object(processor.upgrades, "sam_masks", return_value=({}, dict(requested=1, accepted=0))) as sam:
            result = run(layout="sam")
            sam.assert_called_once()
            self.assertTrue(result["metrics"]["cleaning_cached"])
            self.assertEqual(result["metrics"]["sam"], dict(requested=1, accepted=0, used=0))
        self.assertEqual(calls["translate"], 1)
        with patch("comic_pipeline.comic_original.make_mask", return_value=np.zeros((240, 180), np.uint8)), \
             patch("comic_pipeline.comic_original.OriginalCleaner") as cleaner, \
             patch("comic_pipeline.comic_original.render", return_value=(image, [])), \
             patch("comic_pipeline.reader.refine_bubbles") as extra_refine:
            cleaner.return_value.clean.return_value = np.asarray(image)
            cleaner.return_value.last_metrics = dict(fast_filled=1)
            original = run(cleaning="comic-original", layout="comic-original")
            cached = run(cleaning="comic-original", layout="rectangle")
            self.assertTrue(original["metrics"]["translation_cached"])
            self.assertEqual(original["metrics"]["upstream_cleanup"]["fast_filled"], 1)
            self.assertTrue(cached["metrics"]["cleaning_cached"])
            cleaner.return_value.clean.assert_called_once()
            extra_refine.assert_not_called()
        run(provider="other")
        self.assertEqual(calls["translate"], 2)
        self.assertLessEqual(len(processor.translations), 4)
        self.assertLessEqual(len(processor.cleaned_pages), 2)

    @patch("comic_pipeline.reader.capabilities", return_value=dict(
        cleaning={key: dict(available=True, reason="") for key in CLEANING},
        layout={key: dict(available=True, reason="") for key in LAYOUTS}))
    def test_all_presets_reach_processor_and_do_not_reuse_other_preset_results(self, available):
        class Recorder(FakeProcessor):
            def process(self, image, mode, provider, settings, progress):
                self.calls.append((settings["cleaning"], settings["layout"]))
                return super().process(image, mode, provider, settings, progress)
        recorder = Recorder()
        service = ReaderService(recorder, cache_size=20)
        self.addCleanup(service.close)
        ids = []
        for cleaning in CLEANING:
            for layout in LAYOUTS:
                job = service.submit(dict(payload(), cleaning=cleaning, layout=layout))
                self.assertEqual(finished(service, job["id"])["state"], "done")
                ids.append(job["id"])
        self.assertEqual(len(set(ids)), len(CLEANING)*len(LAYOUTS))
        self.assertEqual(service.submit(dict(payload(), cleaning="hybrid-aot", layout="sam"))["id"], ids[-1])
        self.assertTrue(all((c, l) in recorder.calls for c in CLEANING for l in LAYOUTS))
        for key, bad in [("layout", "unknown"), ("cleaning", "unknown")]:
            with self.assertRaises(ValueError):
                service.submit(dict(payload(), **{key: bad}))

    def test_hybrid_mask_protects_skipped_neighbors_and_reuses_ctd_probability(self):
        upgrades = ReaderUpgrades()
        calls = Counter()
        class CTD:
            def predict(self, pixels):
                calls["ctd"] += 1
                return np.ones(pixels.shape[:2], np.float32), {}
        upgrades.ctd = CTD()
        pixels = np.full((80, 80, 3), 255, np.uint8)
        blocks = [dict(id="a", bbox=[10, 10, 50, 50], translation="你好"),
                  dict(id="p", bbox=[52, 20, 60, 40], translation="…", preserve_original=True)]
        for preset in ["ctd-lama", "hybrid-lama", "ctd-aot", "hybrid-aot"]:
            mask = upgrades.mask(pixels, blocks, preset, FakeEngine(), "page")
            self.assertTrue(mask[30, 30])
            self.assertFalse(mask[20:40, 52:60].any())
            self.assertFalse(mask[75, 75])
        self.assertEqual(calls["ctd"], 1)

    def test_large_aot_capture_covers_mask_edges_without_touching_unmasked_artwork(self):
        upgrades = ReaderUpgrades()
        shapes = []
        class AOT:
            def predict(self, pixels, mask):
                shapes.append(pixels.shape)
                return np.zeros_like(pixels), {}
        upgrades.aot = AOT()
        pixels = np.full((1600, 1200, 3), 77, np.uint8)
        mask = np.zeros((1600, 1200), np.uint8)
        mask[1000:1050, 970:1080] = 255
        mask[-10:, -10:] = 255
        result = upgrades.clean(pixels, mask, "ctd-aot", FakeEngine())
        self.assertTrue(np.all(result[mask > 0] == 0))
        self.assertTrue(np.all(result[mask == 0] == 77))
        self.assertTrue(all(max(shape[:2]) <= 1024 for shape in shapes))

    def test_sam_worker_uses_memory_pipes_and_cached_metadata_is_not_mutated(self):
        upgrades = ReaderUpgrades()
        output = io.BytesIO()
        metadata = dict(ok=True, masks=[dict(id="b1", bbox=[10, 10, 40, 50])])
        np.savez_compressed(output, metadata=np.frombuffer(json.dumps(metadata).encode(), np.uint8), b1=np.ones((40, 30), bool))
        block = dict(id="b1", bbox=[15, 15, 35, 45], bubble_bbox=[10, 10, 40, 50], translation="你好")
        with patch("comic_pipeline.reader_upgrades.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=output.getvalue())) as worker, \
             patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            masks, first = upgrades.sam_masks(np.zeros((80, 80, 3), np.uint8), [block], "page")
            first["used"] = 1
            _, second = upgrades.sam_masks(np.zeros((80, 80, 3), np.uint8), [block], "page")
            self.assertNotIn("used", second)
            self.assertEqual(masks["b1"]["mask"].shape, (40, 30))
            self.assertEqual(worker.call_count, 1)
            self.assertNotIn("GEMINI_API_KEY", worker.call_args.kwargs["env"])
            self.assertIsInstance(worker.call_args.kwargs["input"], bytes)


if __name__ == "__main__":
    unittest.main()

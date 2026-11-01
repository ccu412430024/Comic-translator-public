import base64
import http.client
import io
import json
import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from comic_pipeline.detection import group_paddle_lines
from comic_pipeline.reader import ReaderService, decode_image, png_bytes
from comic_pipeline.reader_http import ReaderHTTPServer, extension_id


def payload(mode="standard"):
    return dict(image="data:image/png;base64,"+base64.b64encode(png_bytes(Image.new("RGB", (32, 48), "white"))).decode(),
                mode=mode, translator="google-web")


def finished(service, identifier):
    deadline = time.monotonic()+3
    while time.monotonic() < deadline:
        job = service.get(identifier)
        if job["state"] not in {"queued", "running"}:
            return job
        time.sleep(.005)
    raise AssertionError("Job did not finish")


class FakeProcessor:
    def __init__(self):
        self.calls = []

    def process(self, image, mode, provider, settings, progress):
        self.calls.append((mode, provider, settings["model"]))
        progress("render", "render", 90)
        return dict(image=png_bytes(image), metrics=dict(mode=mode))


class ReaderTests(unittest.TestCase):
    def test_per_job_model_overrides_do_not_change_defaults_or_mix_cached_results(self):
        processor = FakeProcessor()
        service = ReaderService(processor)
        self.addCleanup(service.close)
        service.configure(dict(key="test-secret", model="gemini-3.5-flash-lite"))
        def submit(model):
            return dict(payload(), translator="gemini", model=model)
        first = service.submit(submit("gemini-3.6-flash"))
        self.assertEqual(finished(service, first["id"])["state"], "done")
        second = service.submit(submit("gemini-3.1-pro-preview"))
        self.assertEqual(finished(service, second["id"])["state"], "done")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(service.submit(submit("gemini-3.6-flash"))["id"], first["id"])
        self.assertEqual([call[2] for call in processor.calls], ["gemini-3.6-flash", "gemini-3.1-pro-preview"])
        self.assertEqual(service.settings()["model"], "gemini-3.5-flash-lite")
        self.assertIn(service.settings()["gpu_residency"], {"resident", "phase"})
        self.assertTrue(any(item["id"] == "gemini-3.8-flash" for item in service.settings()["gemini_models"]))
        for invalid in [None, "", "models/gemini-example", "https://example.com", "x"*101, ["gemini"]]:
            with self.assertRaises(ValueError):
                service.submit(submit(invalid))

    def test_queued_job_keeps_its_original_model_when_defaults_change(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(FakeProcessor):
            def process(self, image, mode, provider, settings, progress):
                entered.set()
                release.wait(2)
                return super().process(image, mode, provider, settings, progress)
        processor = Slow()
        service = ReaderService(processor)
        self.addCleanup(service.close)
        self.addCleanup(release.set)
        service.configure(dict(key="test-secret", model="gemini-3.5-flash-lite"))
        first = service.submit(dict(payload(), translator="gemini", model="gemini-3.6-flash"))
        self.assertTrue(entered.wait(1))
        service.configure(dict(model="gemini-4-argon"))
        second = service.submit(dict(payload(), translator="gemini", model="gemini-3.1-pro-preview"))
        release.set()
        self.assertEqual(finished(service, first["id"])["state"], "done")
        self.assertEqual(finished(service, second["id"])["state"], "done")
        self.assertEqual([call[2] for call in processor.calls], ["gemini-3.6-flash", "gemini-3.1-pro-preview"])

    def test_modes_and_provider_settings_have_independent_caches(self):
        processor = FakeProcessor()
        service = ReaderService(processor=processor, cache_size=3)
        self.addCleanup(service.close)
        ids = []
        for mode in ["standard", "tiled", "paddle"]:
            job = service.submit(payload(mode))
            self.assertEqual(finished(service, job["id"])["state"], "done")
            ids.append(job["id"])
        self.assertEqual(service.submit(payload("paddle"))["id"], ids[-1])
        self.assertEqual(len(processor.calls), 3)
        service.configure(dict(model="gemini-3.5-flash-lite", key="test-secret"))
        other = dict(payload(), translator="gemini")
        job = service.submit(other)
        self.assertEqual(finished(service, job["id"])["state"], "done")
        self.assertIsNone(service.get(ids[0]))
        self.assertEqual(len(service.cache), 3)
        self.assertNotIn("test-secret", json.dumps(service.settings()))
        self.assertNotIn("test-secret", json.dumps(service.get(job["id"])))

    def test_cancel_stops_result_and_never_enters_cache(self):
        entered, release = threading.Event(), threading.Event()
        class Slow:
            def process(self, image, mode, provider, settings, progress):
                entered.set()
                release.wait(2)
                progress("render", "render", 90)
                return dict(image=b"wrong-page", metrics={})
        service = ReaderService(processor=Slow())
        self.addCleanup(service.close)
        self.addCleanup(release.set)
        job = service.submit(payload())
        self.assertTrue(entered.wait(1))
        service.cancel(job["id"])
        release.set()
        self.assertEqual(finished(service, job["id"])["state"], "canceled")
        self.assertIsNone(service.result_image(job["id"]))
        self.assertFalse(service.cache)

    def test_arbitrary_errors_do_not_expose_key_or_ocr(self):
        class Broken:
            def process(self, *args):
                raise RuntimeError("private OCR, key=test-secret, URL")
        service = ReaderService(processor=Broken())
        self.addCleanup(service.close)
        result = finished(service, service.submit(payload())["id"])
        self.assertEqual(result["state"], "error")
        self.assertNotIn("private OCR", json.dumps(result))
        self.assertNotIn("test-secret", json.dumps(result))

    def test_image_validation_rejects_nonimages_and_oversized_dimensions(self):
        self.assertEqual(decode_image(payload()["image"]).size, (32, 48))
        for data in [None, "data:text/html;base64,SGVsbG8=", "not-base64", base64.b64encode(b"not an image").decode()]:
            with self.assertRaises(ValueError):
                decode_image(data)
        data = base64.b64encode(png_bytes(Image.new("RGB", (8200, 16)))).decode()
        with self.assertRaises(ValueError):
            decode_image(data)

    def test_ppocr_groups_dialogue_without_discarding_free_text(self):
        lines = [dict(bbox=[40, 10, 50, 90], orientation="vertical", score=.8),
                 dict(bbox=[20, 10, 30, 90], orientation="vertical", score=.7),
                 dict(bbox=[110, 20, 125, 45], orientation="vertical", score=.5)]
        blocks = group_paddle_lines(lines, [[10, 0, 60, 100]])
        self.assertEqual(len(blocks), 2)
        dialogue = next(b for b in blocks if b["bubble_bbox"])
        self.assertEqual(dialogue["bbox"], [20, 10, 50, 90])
        self.assertEqual(next(b for b in blocks if not b["bubble_bbox"])["bbox"], [110, 20, 125, 45])


class ReaderHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ReaderHTTPServer(("127.0.0.1", 0), service=ReaderService(FakeProcessor()))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, data=None, origin=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
        response = connection.getresponse()
        value = response.read()
        result = response.status, dict(response.getheaders()), value
        connection.close()
        return result

    def test_local_ui_and_extension_work_but_other_sites_cannot_read_or_write(self):
        self.assertEqual(self.request("POST", "/api/settings", dict(key="private-key"))[0], 403)
        self.assertEqual(self.request("POST", "/api/settings", dict(key="private-key"), "https://example.com")[0], 403)
        self.assertEqual(self.request("GET", "/api/settings", origin="https://example.com")[0], 403)
        self.assertEqual(self.request("GET", "/api/settings", host=f"other.test:{self.port}")[0], 403)
        origin = "chrome-extension://"+extension_id()
        status, headers, body = self.request("POST", "/api/settings", dict(key="private-key"), origin)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], origin)
        self.assertNotIn(b"private-key", body)
        self.assertEqual(self.request("OPTIONS", "/api/jobs", origin=origin)[0], 204)

    def test_job_round_trip_png_and_extension_package(self):
        status, _, body = self.request("POST", "/api/jobs", payload("paddle"), self.origin)
        self.assertEqual(status, 202)
        identifier = json.loads(body)["id"]
        finished(self.server.service, identifier)
        status, headers, image = self.request("GET", f"/api/jobs/{identifier}/image")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(Image.open(io.BytesIO(image)).size, (32, 48))
        _, _, archive = self.request("GET", "/download/extension.zip")
        with zipfile.ZipFile(io.BytesIO(archive)) as opened:
            self.assertIn("comic-translator/popup.html", opened.namelist())
            self.assertIn("comic-translator/readers/registry.js", opened.namelist())
            self.assertIn("comic-translator/readers/pixiv-native.js", opened.namelist())
            manifest = json.loads(opened.read("comic-translator/manifest.json"))
            self.assertEqual(manifest["host_permissions"], ["http://127.0.0.1:8765/*"])
            self.assertTrue(all(Path(name).suffix in {".json", ".js", ".html", ".css", ".md"}
                                for name in opened.namelist()))
        self.assertEqual(self.request("GET", "/../../.env")[0], 404)

    def test_built_in_demos_and_overlay_harness_never_serve_private_samples(self):
        public = png_bytes(Image.new("RGB", (32, 48), "white"))
        private = png_bytes(Image.new("RGB", (64, 80), "magenta"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ["samples/user-provided/pages/01-right.png", "samples/user-provided/pages/02-left.png",
                         "outputs/reader-smoke/tiled.png", "samples/smoke/01.png", "samples/smoke/02.png"]:
                target = root/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(public if "/smoke/" in name else private)
            with patch("comic_pipeline.reader_http.ROOT", root):
                for path in ["/demo/right.png", "/demo/left.png"]:
                    self.assertEqual(self.request("GET", path)[0], 404)
                for path in ["/demo/synthetic-01.png", "/demo/synthetic-02.png"]:
                    status, headers, body = self.request("GET", path)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["Content-Type"], "image/png")
                    self.assertEqual(body, public)
                self.assertEqual(self.request("GET", "/test/translated.png")[0], 404)
                self.server.test_harness = True
                self.assertEqual(self.request("GET", "/test/translated.png")[2], public)

    def test_work_research_export_segments_and_sources_keep_origin_boundaries(self):
        import time
        class Researcher:
            def build(self,work):
                return dict(title_ja=work["title"]),dict(sources=[dict(id="s1",title="Wiki",url="https://example.org/wiki")],
                    search_suggestions='<p>Google suggestions</p>'),[],"unused"
        class Builder:
            def build(self,work):
                profile,research,usage,_=Researcher().build(work)
                return profile,research,usage
        self.server.service.researcher_factory=lambda _:Builder()
        self.request("POST","/api/settings",dict(key="private-test-key"),self.origin)
        status,_,body=self.request("POST","/api/works",dict(title="測試作品"),self.origin)
        self.assertEqual(status,202)
        identifier=json.loads(body)["id"]
        deadline=time.monotonic()+2
        while True:
            _,_,body=self.request("GET",f"/api/works/{identifier}")
            work=json.loads(body)
            if work["status"]!="building":break
            if time.monotonic()>deadline:self.fail("Research did not finish")
            time.sleep(.01)
        self.assertEqual(work["status"],"ready",work)
        self.assertNotIn("private-test-key",body.decode())
        self.assertEqual(self.request("GET","/api/works",origin="https://example.com")[0],403)
        self.assertEqual(self.request("POST",f"/api/works/{identifier}/delete",{},"https://example.com")[0],403)
        _,headers,body=self.request("GET",f"/api/works/{identifier}/search-suggestions")
        self.assertIn("sandbox",headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"],"SAMEORIGIN")
        _,_,body=self.request("GET",f"/api/works/{identifier}/export")
        self.assertEqual(json.loads(body)["format"],"comic-work-memory-v1")
        status,_,body=self.request("POST",f"/api/works/{identifier}/segment",dict(label="第二卷"),self.origin)
        self.assertEqual(status,200)
        self.assertEqual(len(json.loads(body)["segments"]),2)
        self.assertEqual(self.request("POST",f"/api/works/{identifier}/delete",{},self.origin)[0],200)
        self.assertEqual(self.request("GET",f"/api/works/{identifier}")[0],404)

    def test_prefetch_routes_require_origin_and_book_identity(self):
        options=dict(book_ref="bookwalker:00000000-0000-0000-0000-000000000001",translator="google-web")
        self.assertEqual(self.request("POST","/api/prefetch",options,"https://example.com")[0],403)
        status,_,body=self.request("POST","/api/prefetch",options,self.origin)
        self.assertEqual(status,201)
        session=json.loads(body)["id"]
        status,_,body=self.request("POST",f"/api/prefetch/{session}/pages",dict(payload(),page_id="135"),self.origin)
        self.assertEqual(status,202)
        self.assertEqual(finished(self.server.service,json.loads(body)["id"])["state"],"done")
        self.assertEqual(self.request("POST",f"/api/prefetch/{session}/activate",dict(page_id="135",book_ref="other"),self.origin)[0],400)
        self.assertEqual(self.request("POST",f"/api/prefetch/{session}/activate",dict(page_id="135",book_ref=options["book_ref"]),self.origin)[0],200)
        self.assertEqual(self.request("POST",f"/api/prefetch/{session}/cancel",{},self.origin)[0],200)

    def test_reader_diagnostics_are_local_and_not_persisted(self):
        report=dict(adapter="bookwalker-dom-v1",supported=True,nodes=[])
        self.assertEqual(self.request("POST","/api/reader-diagnostics",report,"https://example.com")[0],403)
        self.assertEqual(self.request("POST","/api/reader-diagnostics",report,self.origin)[0],200)
        self.assertEqual(json.loads(self.request("GET","/api/reader-diagnostics")[2]),report)
        self.assertEqual(self.request("GET","/api/reader-diagnostics",origin="https://example.com")[0],403)
        current=dict(adapter="reader-v1",platform="pixiv",phase="display",current_position=2,job_id="test-job",image="private-image")
        self.assertEqual(self.request("POST","/api/reader-diagnostics",current,self.origin)[0],200)
        history=json.loads(self.request("GET","/api/readers/diagnostics")[2])["reports"]
        self.assertEqual(history[-1]["report"]["current_position"],2)
        self.assertNotIn("private-image",json.dumps(history))
        self.assertEqual(self.request("GET","/api/readers/diagnostics",origin="https://example.com")[0],403)

    def test_pixel_probe_requires_opt_in_test_server_and_origin(self):
        images={name:payload()["image"] for name in ("current","candidate","source")}
        self.assertEqual(self.request("POST","/test/bookwalker-probe",images,self.origin)[0],404)
        self.server.test_harness=True
        self.assertEqual(self.request("POST","/test/bookwalker-probe",images,"https://example.com")[0],403)
        self.assertEqual(self.request("POST","/test/bookwalker-probe",images,self.origin)[0],200)
        status,headers,body=self.request("GET","/test/bookwalker-probe/current.png")
        self.assertEqual(status,200);self.assertEqual(headers["Content-Type"],"image/png")
        self.assertEqual(Image.open(io.BytesIO(body)).size,(32,48))


if __name__ == "__main__":
    unittest.main()

"""Loopback-only HTTP UI/API for the current visible-page translator."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import zipfile
import threading

from .reader import ReaderService, decode_image, png_bytes
from .gemini import GeminiError
from .library import Conflict
from .reader_diagnostics import append_report

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT/"web/reader"
EXTENSION = ROOT/"chrome-extension"


def extension_id():
    public = json.loads((EXTENSION/"manifest.json").read_text(encoding="utf-8"))["key"]
    digest = hashlib.sha256(base64.b64decode(public)).digest()[:16].hex()
    return "".join(chr(ord("a")+int(n, 16)) for n in digest)


class ReaderHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address=("127.0.0.1", 8765), service=None):
        if address[0] != "127.0.0.1":
            raise ValueError("The reader server must bind to 127.0.0.1")
        self.service = None
        self.reader_diagnostics_history=[]
        self.reader_diagnostics_lock=threading.Lock()
        super().__init__(address, ReaderHandler)
        try:
            self.extension_origin = "chrome-extension://"+extension_id()
            self.service = service or ReaderService()
        except BaseException:
            super().server_close()
            raise

    def server_close(self):
        super().server_close()
        if self.service is not None:self.service.close()


class ReaderHandler(BaseHTTPRequestHandler):
    server_version = "ComicReader/0.9"
    sys_version = ""

    def log_message(self, *_):
        pass  # Do not log request bodies, URLs, private page text or keys.

    def allowed(self, require_origin=False):
        port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return False
        origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", self.server.extension_origin}
        return origin in origins if origin else not require_origin

    def send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", (extra or {}).get("X-Frame-Options", "DENY"))
        origin = self.headers.get("Origin")
        if origin and self.allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        if content_type.startswith("text/html") and "Content-Security-Policy" not in (extra or {}):
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        for key, value in (extra or {}).items():
            if key != "X-Frame-Options":
                self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # A closed browser tab is a normal cancellation.

    def json(self, status, value):
        self.send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def discard_small_body(self):
        # On Windows, closing a rejected POST with unread bytes can reset the
        # socket before its 403 reaches the client. Drain only small bounded
        # bodies with a short timeout; authorization is never relaxed.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        if not 0 < length <= 65536:
            return
        previous = self.connection.gettimeout()
        try:
            self.connection.settimeout(1)
            self.rfile.read(length)
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous)

    def do_OPTIONS(self):
        if not self.allowed(require_origin=True):
            return self.json(403, {"error": "不允許的來源"})
        self.send(204, b"")

    def do_GET(self):
        if not self.allowed():
            return self.json(403, {"error": "不允許的來源"})
        path = urlsplit(self.path).path
        if path == "/api/health":
            return self.json(200, dict(app="comic-reader", version="0.22.2", ready=True,database="sqlite",database_ready=True,
                                       extension_id=extension_id()))
        if path == "/api/settings":
            return self.json(200, dict(self.server.service.settings(), extension_path=str(EXTENSION)))
        if path == "/api/gpu":
            return self.json(200, self.server.service.gpu_status())
        if path == "/api/gemini/cache":
            return self.json(200,self.server.service.cloud_cache_status())
        if path == "/api/reader-diagnostics":
            return self.json(200, getattr(self.server, "reader_diagnostics", {}))
        if path == "/api/readers/diagnostics":
            with self.server.reader_diagnostics_lock:
                return self.json(200, dict(reports=list(self.server.reader_diagnostics_history)))
        if path.startswith("/api/library"):
            parts = path.strip("/").split("/")
            service = self.server.service
            query = parse_qs(urlsplit(self.path).query)
            arg = lambda name, default="": query.get(name, [default])[0]
            try:
                if parts == ["api", "library"]:
                    return self.json(200, dict(works=service.library.list(), runtime=service.library_runtime()))
                if parts == ["api", "library", "export"]:
                    with service.memory.lock:
                        return self.json(200, dict(format="comic-library-backup-v1", works=[service.library.export(w["id"]) for w in service.memory.list()]))
                if len(parts) == 3:
                    return self.json(200, dict(service.library.detail(parts[2]), runtime=service.library_runtime(parts[2])))
                if len(parts) == 4:
                    identifier, action = parts[2:]
                    if action == "pages":
                        return self.json(200, service.library.pages(identifier, arg("segment"), arg("offset", "0"), arg("limit", "25")))
                    if action == "page":
                        return self.json(200, service.library.page(identifier, arg("segment"), arg("key")))
                    if action == "export":
                        return self.json(200, service.library.export(identifier))
                    if action == "recent-story":
                        return self.json(200, service.story_updates.status(identifier, arg("task")))
            except (ValueError, TypeError):
                return self.json(404, {"error": "找不到資料或查詢範圍無效"})
            return self.json(404, {"error": "找不到書庫功能"})
        if path == "/api/works":
            return self.json(200, dict(works=self.server.service.library.list()))
        if path.startswith("/api/works/"):
            parts = path.strip("/").split("/")
            try:
                if len(parts) == 3:
                    return self.json(200, self.server.service.library.detail(parts[2]))
                if len(parts) == 4 and parts[3] == "export":
                    return self.json(200, self.server.service.memory.export(parts[2]))
                if len(parts) == 4 and parts[3] == "search-suggestions":
                    work = self.server.service.memory.get(parts[2])
                    html = (work.get("research") or {}).get("search_suggestions", "")
                    # Provider-rendered suggestions stay in an opaque, scriptless
                    # iframe; never insert third-party HTML into our document.
                    return self.send(200, html.encode(), "text/html; charset=utf-8", {
                        "X-Frame-Options": "SAMEORIGIN",
                        "Content-Security-Policy": "sandbox allow-popups allow-popups-to-escape-sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; base-uri 'none'; frame-ancestors 'self'"})
            except ValueError:
                return self.json(404, {"error": "找不到這部作品"})
            return self.json(404, {"error": "找不到作品功能"})
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            if len(parts) not in {3, 4}:
                return self.json(404, {"error": "找不到這張圖片"})
            identifier = parts[2]
            if len(parts) == 4 and parts[3] == "image":
                result = self.server.service.result_image(identifier)
                return self.send(200, result, "image/png") if result else self.json(404, {"error": "圖片尚未完成或已清除"})
            if len(parts) == 3:
                job = self.server.service.get(identifier)
                return self.json(200, job) if job else self.json(404, {"error": "工作已清除，請重新翻譯"})
        if path == "/download/extension.zip":
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in sorted(EXTENSION.rglob("*")):
                    if file.is_file() and file.suffix in {".json", ".js", ".html", ".css", ".md"}:
                        archive.write(file, "comic-translator/"+file.relative_to(EXTENSION).as_posix())
            return self.send(200, output.getvalue(), "application/zip", {"Content-Disposition": 'attachment; filename="comic-translator-chrome.zip"'})
        # Only project-generated layout fixtures are exposed as built-in demos.
        demo = {"/demo/synthetic-01.png": ROOT/"samples/smoke/01.png",
                "/demo/synthetic-02.png": ROOT/"samples/smoke/02.png"}
        if path in {"/model-picker.js", "/work-picker.js", "/library-platforms.js", "/book-context.js", "/gemini-status.js", "/gemini-connection.js"}:
            return self.send(200, (EXTENSION/path.lstrip("/")).read_bytes(), "text/javascript; charset=utf-8")
        if path in demo and demo[path].is_file():
            return self.send(200, demo[path].read_bytes(), "image/png")
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/index.html": ("index.html", "text/html; charset=utf-8"),
                 "/reader.js": ("reader.js", "text/javascript; charset=utf-8"),
                 "/reader.css": ("reader.css", "text/css; charset=utf-8")}
        files.update({"/library": ("library.html", "text/html; charset=utf-8"),
                      "/recent-story.js": ("recent-story.js", "text/javascript; charset=utf-8"),
                      "/library.js": ("library.js", "text/javascript; charset=utf-8"),
                      "/library.css": ("library.css", "text/css; charset=utf-8")})
        # Opt-in DOM harness for extension rendering tests without Chrome APIs.
        if getattr(self.server, "test_harness", False):
            if path == "/test/bookwalker-probe":
                return self.send(200, ('<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>BookWalker 畫面核對</title><link rel="stylesheet" href="/reader.css"><h1>BookWalker 畫面核對（僅本次服務記憶體）</h1><p>閱讀器目前畫面</p><img src="/test/bookwalker-probe/current.png"><p>下載的頁面圖片</p><img src="/test/bookwalker-probe/source.png"><p>目前最佳的拼接候選</p><img src="/test/bookwalker-probe/candidate.png"></html>').encode(), "text/html; charset=utf-8")
            if path.startswith("/test/bookwalker-probe/"):
                name=path.rsplit("/",1)[-1].removesuffix(".png")
                value=getattr(self.server,"bookwalker_probe",{}).get(name)
                return self.send(200,value,"image/png") if value else self.json(404,{"error":"尚無測試畫面"})
            if path == "/test/popup":
                html = (EXTENSION/"popup.html").read_text(encoding="utf-8").replace('<script src="popup.js"', '<script src="/test/popup-harness.js" defer></script><script src="popup.js"')
                return self.send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            harness = {"/test/overlay": (ROOT/"tests/overlay-harness.html", "text/html; charset=utf-8"),
                       "/test/popup-harness.js": (ROOT/"tests/popup-harness.js", "text/javascript; charset=utf-8"),
                       "/test/popup.js": (EXTENSION/"popup.js", "text/javascript; charset=utf-8"),
                       "/test/model-picker.js": (EXTENSION/"model-picker.js", "text/javascript; charset=utf-8"),
                       "/test/work-picker.js": (EXTENSION/"work-picker.js", "text/javascript; charset=utf-8"),
                       "/test/library-platforms.js": (EXTENSION/"library-platforms.js", "text/javascript; charset=utf-8"),
                       "/test/book-context.js": (EXTENSION/"book-context.js", "text/javascript; charset=utf-8"),
                       "/test/gemini-status.js": (EXTENSION/"gemini-status.js", "text/javascript; charset=utf-8"),
                       "/test/gemini-connection.js": (EXTENSION/"gemini-connection.js", "text/javascript; charset=utf-8"),
                       "/test/popup.css": (EXTENSION/"popup.css", "text/css; charset=utf-8"),
                       "/test/overlay-harness.js": (ROOT/"tests/overlay-harness.js", "text/javascript; charset=utf-8"),
                       "/test/page-state.js": (EXTENSION/"page-state.js", "text/javascript; charset=utf-8"),
                       "/test/translated.png": (ROOT/"samples/smoke/02.png", "image/png"),
                       "/test/content.js": (EXTENSION/"content.js", "text/javascript; charset=utf-8")}
            if path in harness:
                target, content_type = harness[path]
                return self.send(200, target.read_bytes(), content_type)
        if path in files:
            filename, content_type = files[path]
            target = WEB/filename
            if target.is_file():
                return self.send(200, target.read_bytes(), content_type)
        self.json(404, {"error": "找不到這個頁面"})

    def do_POST(self):
        if not self.allowed(require_origin=True):
            self.discard_small_body()
            return self.json(403, {"error": "不允許的來源"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.discard_small_body()
            return self.json(415, {"error": "請使用 JSON"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 17*1024*1024:
                raise ValueError("請求過大或沒有內容")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("請求格式無效")
            path = urlsplit(self.path).path
            if path == "/api/settings":
                return self.json(200, self.server.service.configure(payload))
            if path == "/api/gpu/release":
                return self.json(202, self.server.service.release_gpu())
            if path == "/api/gpu/resume":
                return self.json(200, self.server.service.resume_gpu(payload))
            if path == "/test/bookwalker-probe" and getattr(self.server,"test_harness",False):
                self.server.bookwalker_probe = {name:png_bytes(decode_image(payload.get(name))) for name in ("current","candidate","source")}
                return self.json(200,{"received":True})
            if path == "/api/reader-diagnostics":
                if len(json.dumps(payload)) > 24000:
                    raise ValueError("閱讀器診斷格式無效")
                append_report(self.server,payload)
                return self.json(200, {"received": True})
            if path == "/api/gemini/check":
                return self.json(200, self.server.service.check_gemini(payload))
            if path == "/api/gemini/cache/clear":
                return self.json(200,self.server.service.clear_cloud_cache())
            if path == "/api/jobs":
                return self.json(202, self.server.service.submit(payload))
            if path == "/api/prefetch":
                return self.json(201, self.server.service.create_prefetch(payload))
            if path == "/api/works":
                return self.json(202, self.server.service.create_work(payload))
            if path == "/api/library/books":
                return self.json(200, self.server.service.library.register(payload))
            if path == "/api/library/create":
                work = self.server.service.memory.create(payload.get("title"), payload.get("author", ""), payload.get("volume", ""))
                return self.json(201, self.server.service.library.detail(work["id"]))
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "library"]:
                if parts[3] == "recent-story":
                    return self.json(202, self.server.service.story_updates.start(parts[2], payload))
                return self.json(200, self.server.service.manage_work(parts[2], parts[3], payload))
            if len(parts) == 4 and parts[:2] == ["api", "prefetch"]:
                if parts[3] == "pages":
                    return self.json(202, self.server.service.submit_prefetch(parts[2],payload))
                if parts[3] == "activate":
                    return self.json(200, self.server.service.activate_prefetch(parts[2],payload))
                if parts[3] == "cancel":
                    return self.json(200, self.server.service.cancel_prefetch(parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "works"]:
                identifier, action = parts[2:]
                if action == "research":
                    return self.json(202, self.server.service.research_work(identifier, payload))
                if action == "segment":
                    return self.json(200, self.server.service.memory.segment(identifier, payload.get("label", "")))
                if action == "delete":
                    self.server.service.memory.delete(identifier)
                    return self.json(200, {"deleted": True})
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                return self.json(200, self.server.service.cancel(parts[2]) or {"state": "missing"})
            self.json(404, {"error": "找不到這個功能"})
        except GeminiError as exc:
            self.json(502, {"error": str(exc)})
        except Conflict as exc:
            self.json(409, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self.json(400, {"error": str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "請求格式無效"})
        except Exception:
            self.json(500, {"error": "本機服務處理失敗，請重新啟動後重試"})

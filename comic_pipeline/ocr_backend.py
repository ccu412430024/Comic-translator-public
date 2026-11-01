"""Persistent, isolated OCR workers; image arrays and results stay in pipes."""
import atexit
import os
import queue
import subprocess
import threading
import time

from .onnx_worker import ROOT, read_packet, write_packet
from .reader_options import ProcessingError

OCR_MODELS = {"nar": "NAR", "base": "Manga-OCR", "paddle": "Paddle"}
DEFAULT_OCR = "base"
RUNTIMES = {"nar": ROOT/".venv-ocr-experiments/Scripts/python.exe",
            "paddle": ROOT/".venv-ocr-vl/Scripts/python.exe"}


def capabilities():
    assets = ROOT/".runtime/ocr-candidates"
    checks = {"nar": [RUNTIMES["nar"], assets/"nar/onnx/mangaocr_nar_small_fp32.onnx",
                      assets/"nar/nar_vocabulary_6817.json", assets/"nar/manga_ocr_nar_small/preprocess.py"],
              "paddle": [RUNTIMES["paddle"], assets/"paddlevl-source/model.safetensors",
                         assets/"paddlevl-source/tokenizer_config.json"]}
    result = {DEFAULT_OCR: dict(available=True, reason="")}
    for model, files in checks.items():
        ready = all(p.is_file() for p in files)
        result[model] = dict(available=ready, reason="" if ready else f"請先準備 {OCR_MODELS[model]} OCR 模型與執行環境")
    return result


class OCRBackend:
    """One selected GPU OCR remains loaded; switching releases the previous one."""
    def __init__(self, timeout=600):
        self.timeout = timeout
        self.process = None
        self.model = None
        self.info = {}
        self.lock = threading.RLock()
        atexit.register(self.close)

    def _response(self):
        try:
            metadata, arrays = self.responses.get(timeout=self.timeout)
        except queue.Empty as error:
            self.close()
            raise ProcessingError("OCR 等待逾時，請縮小畫面或釋放 GPU 後重試。") from error
        if metadata.get("error"):
            self.close()
            raise ProcessingError("OCR 模型處理失敗，請釋放 GPU 後重試，或改選 Manga-OCR。")
        return metadata

    def _start(self, model):
        self.close()
        if not capabilities()[model]["available"]:
            raise ProcessingError(capabilities()[model]["reason"])
        environment = {k:v for k,v in os.environ.items() if not any(s in k.upper() for s in
            ("API_KEY", "API_TOKEN", "HF_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"))}
        environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONIOENCODING="utf-8",
            HF_MODULES_CACHE=str(ROOT/".runtime/ocr-candidates/module-cache"), HF_HOME=str(ROOT/".runtime/ocr-candidates/hf-cache"))
        self.responses = queue.Queue()
        process = subprocess.Popen([str(RUNTIMES[model]), "-u", str(ROOT/"scripts/reader_ocr_worker.py"), model],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=ROOT, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.process, self.model = process, model
        responses = self.responses
        def receive():
            try:
                while True:
                    responses.put(read_packet(process.stdout))
            except (EOFError, OSError, ValueError):
                responses.put((dict(error="WorkerExited"), []))
        threading.Thread(target=receive, daemon=True, name="reader-ocr-pipe").start()
        try:
            self.info = self._response()
            if not self.info.get("ready") or self.info.get("model") != model:
                raise ProcessingError("OCR 模型未能載入，請檢查本機環境。")
        except Exception:
            self.close()
            raise

    def recognize(self, model, crops):
        if model not in RUNTIMES:
            raise ValueError("Unsupported isolated OCR model")
        if not crops:
            return []
        with self.lock:
            if self.model != model or self.process is None or self.process.poll() is not None:
                self._start(model)
            texts = []
            # Bounded packets avoid duplicating a whole high-resolution page.
            for offset in range(0, len(crops), 8):
                chunk = crops[offset:offset+8]
                try:
                    write_packet(self.process.stdin, dict(command="recognize"), chunk)
                    response = self._response()
                except (OSError, EOFError) as error:
                    self.close()
                    raise ProcessingError("OCR 程序已結束，請重新開始翻譯。") from error
                values = response.get("texts")
                if not isinstance(values, list) or len(values) != len(chunk) or not all(isinstance(v,str) for v in values):
                    self.close()
                    raise ProcessingError("OCR 回傳不完整，請重新開始翻譯。")
                texts.extend(values)
            return texts

    def close(self):
        with self.lock:
            process, self.process = self.process, None
            self.model, self.info = None, {}
            if process is None:
                return
            if process.poll() is None:
                try:
                    write_packet(process.stdin, dict(command="close"))
                    process.wait(timeout=5)
                except (OSError, EOFError, subprocess.TimeoutExpired):
                    # /T includes the Windows venv redirector's own child.
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def release_gpu(self):
        with self.lock:
            selected = self.model
            self.close()
            return ["ocr-"+selected] if selected else []

"""Lazy DirectML with a persistent isolated worker and CPU retry on failure."""
import atexit
import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import threading

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
GPU_PYTHON = ROOT/".venv-lama-gpu/Scripts/python.exe"


class DirectMLWorker:
    provider = "DmlExecutionProvider"

    def __init__(self):
        environment = dict(os.environ)
        environment.pop("GEMINI_API_KEY", None)
        self.responses = queue.Queue()
        self.process = subprocess.Popen([str(GPU_PYTHON), "-u", str(ROOT/"scripts/lama_gpu_worker.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=ROOT,
            env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def read():
            try:
                for line in self.process.stdout:
                    self.responses.put(json.loads(line))
            except (OSError, ValueError):
                pass
            finally:
                self.responses.put({"error": "WorkerExited"})
        threading.Thread(target=read, name="lama-gpu-pipe", daemon=True).start()
        atexit.register(self.close)
        try:
            ready = self.response(90)
            if not ready.get("ready"):
                raise RuntimeError("DirectML startup failed")
            self.adapter = ready["adapter"]
        except Exception:
            self.close()
            raise

    def response(self, timeout=120):
        try:
            value = self.responses.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("DirectML worker timeout") from error
        if value.get("error"):
            raise RuntimeError("DirectML worker failed")
        return value

    def __call__(self, image, mask, config):
        request = dict(shape=list(mask.shape), image=base64.b64encode(image.tobytes()).decode(),
                       mask=base64.b64encode(mask.tobytes()).decode())
        self.process.stdin.write(json.dumps(request, separators=(",", ":")).encode()+b"\n")
        self.process.stdin.flush()
        value = self.response()
        result = np.frombuffer(base64.b64decode(value["image"], validate=True), np.uint8).reshape(image.shape)
        return result.copy()

    def close(self):
        process = getattr(self, "process", None)
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
        atexit.unregister(self.close)


class LamaBackend:
    def __init__(self, cpu_factory, gpu_factory=DirectMLWorker, device=None, gpu_available=None):
        self.cpu_factory, self.gpu_factory = cpu_factory, gpu_factory
        self.device = device or os.getenv("COMIC_LAMA_DEVICE", "auto")
        self.gpu_available = GPU_PYTHON.is_file() if gpu_available is None else gpu_available
        self.model = None
        self.provider = "CPUExecutionProvider"
        self.fallback = ""

    def __call__(self, image, mask, config):
        if self.model is None and self.device != "cpu" and self.gpu_available:
            try:
                self.model = self.gpu_factory()
                self.provider = "DmlExecutionProvider"
            except Exception as error:
                self.fallback = type(error).__name__
        if self.model is None:
            self.model = self.cpu_factory()
        try:
            return self.model(image, mask, config)
        except Exception as error:
            if self.provider != "DmlExecutionProvider":
                raise
            self.fallback = type(error).__name__
            self.model.close()
            self.model = self.cpu_factory()
            self.provider = "CPUExecutionProvider"
            return self.model(image, mask, config)

    def close(self):
        if self.model is not None and hasattr(self.model, "close"):
            self.model.close()

    def release_gpu(self):
        if self.model is None or self.provider != "DmlExecutionProvider":
            return False
        self.model.close()
        self.model = None
        self.provider = "CPUExecutionProvider"
        return True

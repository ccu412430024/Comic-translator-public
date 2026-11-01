"""Per-model measured GPU policy, with lazy isolated sessions and CPU retry."""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import threading
import weakref

from .onnx_worker import GPU_PYTHON, MODEL_PATHS, OnnxWorkerSession, ROOT

POLICY = ROOT/".runtime/model-gpu-policy.json"
_PHASE_LOCK = threading.RLock()
_SESSIONS = weakref.WeakSet()


@lru_cache(maxsize=8)
def digest(path, size, modified):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def measured_device(model):
    override = os.getenv("COMIC_"+model.upper()+"_DEVICE", "auto")
    if override not in {"cpu", "dml"}:
        override = os.getenv("COMIC_INFERENCE_DEVICE", "auto")
    if override in {"cpu", "dml"}:
        return override
    try:
        value = json.loads(POLICY.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("models"), dict):
            return "cpu"
        path = MODEL_PATHS[model]
        stat = path.stat()
        item = value["models"][model]
        if not isinstance(item, dict):
            return "cpu"
        if value.get("version") == 1 and item.get("device") == "dml" and item.get("sha256") == digest(str(path), stat.st_size, stat.st_mtime_ns):
            return "dml"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return "cpu"


def gpu_residency():
    # Coexistence measured faster on P3000. Keep the older bounded policy as
    # an explicit opt-in for environments with tighter GPU memory budgets.
    return "phase" if os.getenv("COMIC_GPU_RESIDENCY") == "phase" else "resident"


class ModelBackendSession:
    def __init__(self, model, cpu_factory, *, device=None, gpu_factory=None, gpu_available=None):
        self.model, self.cpu_factory = model, cpu_factory
        self.device = device if device is not None else measured_device(model)
        self.gpu_factory = gpu_factory or (lambda: OnnxWorkerSession(model))
        self.gpu_available = GPU_PYTHON.is_file() if gpu_available is None else gpu_available
        self.session = None
        self.provider = "CPUExecutionProvider"
        self.fallback = ""
        self.evictions = 0
        self.lock = threading.RLock()
        _SESSIONS.add(self)

    def _phase(self):
        if gpu_residency() == "resident":
            return
        # Optional bounded policy: keep CTD, release the idle PP/AOT session.
        incompatible = {"paddle": "aot", "aot": "paddle"}.get(self.model)
        if self.device != "dml" or self.session is not None and self.provider != "DmlExecutionProvider":
            return
        for other in list(_SESSIONS):
            if other.model == incompatible:
                other.release_gpu()

    def _ensure(self):
        if self.session is None and self.device == "dml" and self.gpu_available:
            try:
                self.session = self.gpu_factory()
                if "DmlExecutionProvider" not in self.session.get_providers():
                    raise RuntimeError("GPU session fell back to CPU")
                self.provider = "DmlExecutionProvider"
            except Exception as error:
                self._cpu(error)
        if self.session is None:
            self.session = self.cpu_factory()
        return self.session

    def _cpu(self, error):
        self.fallback = type(error).__name__
        if self.session is not None and hasattr(self.session, "close"):
            self.session.close()
        self.session, self.provider = None, "CPUExecutionProvider"
        self.session = self.cpu_factory()

    def get_inputs(self):
        with _PHASE_LOCK, self.lock:
            self._phase()
            return self._ensure().get_inputs()

    def get_providers(self):
        with _PHASE_LOCK, self.lock:
            self._phase()
            self._ensure()
            return self.session.get_providers()

    def run(self, outputs, inputs):
        with _PHASE_LOCK, self.lock:
            self._phase()
            session = self._ensure()
            try:
                return session.run(outputs, inputs)
            except Exception as error:
                if self.provider != "DmlExecutionProvider":
                    raise
                self._cpu(error)
                return self.session.run(outputs, inputs)

    def close(self):
        with _PHASE_LOCK, self.lock:
            if self.session is not None and hasattr(self.session, "close"):
                self.session.close()
            self.session = None

    def release_gpu(self):
        with _PHASE_LOCK, self.lock:
            if self.session is not None and self.provider == "DmlExecutionProvider":
                if hasattr(self.session, "close"):
                    self.session.close()
                self.session = None
                self.provider = "CPUExecutionProvider"
                self.evictions += 1
                return True
            return False


def model_session(model, options):
    import onnxruntime as ort
    return ModelBackendSession(model, lambda: ort.InferenceSession(str(MODEL_PATHS[model]),
        sess_options=options, providers=["CPUExecutionProvider"]))

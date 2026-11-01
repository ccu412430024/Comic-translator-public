"""Private, bounded array pipes for isolated CPU/DirectML ONNX sessions."""
import atexit
import io
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import threading
import time
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
GPU_PYTHON = ROOT/".venv-lama-gpu/Scripts/python.exe"
MODEL_PATHS = {
    "ctd": ROOT/".runtime/upgrade-models/ctd.onnx",
    "paddle": ROOT/".runtime/detection-models/ppocrv6-medium.onnx",
    "aot": ROOT/".runtime/upgrade-models/aot.onnx",
    "rtdetr": ROOT/".runtime/ComicTranslate/models/detection/detector-v4-s_int8.onnx",
}
MAX_PACKET = 128*1024*1024


def write_packet(stream, metadata, arrays=()):
    buffer = io.BytesIO()
    np.savez(buffer, metadata=np.frombuffer(json.dumps(metadata).encode(), np.uint8),
             **{f"a{i}": np.asarray(value) for i,value in enumerate(arrays)})
    data = buffer.getvalue()
    if len(data) > MAX_PACKET:
        raise ValueError("Model packet too large")
    stream.write(struct.pack("!I", len(data)))
    stream.write(data)
    stream.flush()


def read_exact(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        data = stream.read(count-len(chunks))
        if not data:
            raise EOFError("Model worker closed")
        chunks.extend(data)
    return bytes(chunks)


def read_packet(stream):
    length, = struct.unpack("!I", read_exact(stream, 4))
    if not 0 < length <= MAX_PACKET:
        raise ValueError("Model packet too large")
    with np.load(io.BytesIO(read_exact(stream, length)), allow_pickle=False) as data:
        metadata = json.loads(data["metadata"].tobytes())
        arrays = [data[f"a{i}"].copy() for i in range(len(data.files)-1)]
    return metadata, arrays


class OnnxWorkerSession:
    """Minimal ORT session interface. No images, credentials or tensors on disk."""
    def __init__(self, model, device="dml", profile="", python=GPU_PYTHON, timeout=180):
        if model not in MODEL_PATHS or device not in {"cpu", "dml"}:
            raise ValueError("Unsupported model worker")
        environment = dict(os.environ)
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "COMIC_API_KEY"):
            environment.pop(name, None)
        self.lock, self.responses = threading.Lock(), queue.Queue()
        self.last_run = {}
        command = [str(python), "-u", str(ROOT/"scripts/model_gpu_worker.py"), "--model", model, "--device", device]
        if profile:
            command += ["--profile", str(profile)]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=ROOT, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def read():
            try:
                while True:
                    self.responses.put(read_packet(self.process.stdout))
            except (EOFError, OSError, ValueError):
                self.responses.put((dict(error="WorkerExited"), []))
        threading.Thread(target=read, daemon=True, name="model-array-pipe").start()
        atexit.register(self.close)
        try:
            self.info, _ = self.response(timeout)
            if not self.info.get("ready"):
                raise RuntimeError("Model worker failed to initialize")
        except Exception:
            self.close()
            raise

    def response(self, timeout=180):
        try:
            value = self.responses.get(timeout=timeout)
        except queue.Empty as error:
            self.close()
            raise TimeoutError("Model worker timeout") from error
        if value[0].get("error"):
            detail = value[0]["error"]
            if detail == "WorkerExited":
                try:
                    detail += " (exit "+str(self.process.wait(timeout=3))+")"
                except subprocess.TimeoutExpired:
                    pass
            raise RuntimeError("Model worker: "+detail)
        return value

    def get_inputs(self):
        return [SimpleNamespace(**item) for item in self.info["inputs"]]

    def get_providers(self):
        return self.info["providers"]

    def run(self, output_names, input_feed):
        with self.lock:
            started = time.perf_counter()
            names = list(input_feed)
            write_packet(self.process.stdin, dict(command="run", outputs=output_names, inputs=names), list(input_feed.values()))
            metadata, arrays = self.response()
            self.last_run = dict(metadata, ipc_seconds=time.perf_counter()-started)
            return arrays

    def end_profiling(self):
        with self.lock:
            write_packet(self.process.stdin, dict(command="profile"))
            metadata, _ = self.response()
            return metadata

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

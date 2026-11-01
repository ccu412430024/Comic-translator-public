"""Isolated inference process. Array packets only; no network or page files."""
import argparse
from collections import Counter
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
import time

import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from comic_pipeline.gpu_devices import adapters, tested_adapter
from comic_pipeline.onnx_worker import MODEL_PATHS, read_packet, write_packet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=MODEL_PATHS, required=True)
    parser.add_argument("--device", choices=["cpu", "dml"], required=True)
    parser.add_argument("--profile", default="")
    parser.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args()
    selected = tested_adapter(adapters()) if args.device == "dml" else None
    if args.device == "dml" and (selected is None or "DmlExecutionProvider" not in ort.get_available_providers()):
        raise RuntimeError("Tested DirectML adapter unavailable")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.enable_mem_pattern = args.device != "dml"
    options.log_severity_level = 3
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    if args.profile:
        options.enable_profiling = True
        options.profile_file_prefix = args.profile
    providers = [("DmlExecutionProvider", {"device_id": selected["index"]}), "CPUExecutionProvider"] if selected else ["CPUExecutionProvider"]
    started = time.perf_counter()
    # ORT's Python EP retry message uses stdout. Keep the array pipe binary.
    with redirect_stdout(sys.stderr):
        session = ort.InferenceSession(str(MODEL_PATHS[args.model]), sess_options=options, providers=providers)
    if selected and "DmlExecutionProvider" not in session.get_providers():
        raise RuntimeError("DirectML initialization fell back to CPU")
    if args.diagnostic:
        print(json.dumps(dict(ready=True, providers=session.get_providers(), seconds=time.perf_counter()-started)))
        return
    write_packet(sys.stdout.buffer, dict(ready=True, model=args.model, runtime=ort.__version__,
        adapter=selected, session_seconds=time.perf_counter()-started, providers=session.get_providers(),
        inputs=[dict(name=i.name, shape=i.shape, type=i.type) for i in session.get_inputs()]))
    while True:
        try:
            request, arrays = read_packet(sys.stdin.buffer)
        except EOFError:
            break
        try:
            if request["command"] == "profile":
                path = session.end_profiling()
                events = json.loads(Path(path).read_text(encoding="utf-8")) if path else []
                counts = Counter(e.get("args", {}).get("provider") for e in events if e.get("args", {}).get("provider"))
                write_packet(sys.stdout.buffer, dict(node_events=dict(counts), profile=Path(path).name if path else ""))
                continue
            inputs = dict(zip(request["inputs"], arrays))
            started = time.perf_counter()
            outputs = session.run(request["outputs"], inputs)
            elapsed = time.perf_counter()-started
            if not all(np.isfinite(a).all() for a in outputs):
                raise RuntimeError("Nonfinite output")
            write_packet(sys.stdout.buffer, dict(model_seconds=elapsed), outputs)
        except Exception as error:
            write_packet(sys.stdout.buffer, dict(error=type(error).__name__))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        if "--diagnostic" in sys.argv:
            print(json.dumps(dict(error=type(error).__name__, detail=str(error)[:2500])))
        else:
            write_packet(sys.stdout.buffer, dict(error=type(error).__name__))
        sys.exit(1)

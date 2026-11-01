"""Offscreen upstream Qt renderer; page input and output travel only in RAM."""
import io
import json
import os
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from comic_pipeline.upstream import configure
configure()
from comic_pipeline.comic_original import render_qt


def main():
    with np.load(io.BytesIO(sys.stdin.buffer.read()), allow_pickle=False) as request:
        pixels = request["image"].copy()
        blocks = json.loads(request["blocks"].tobytes())
    # Keep incidental upstream prints out of the binary output stream.
    from contextlib import redirect_stdout
    with redirect_stdout(sys.stderr):
        result, metadata = render_qt(pixels, blocks)
    output = io.BytesIO()
    np.savez(output, image=result, metadata=np.frombuffer(json.dumps(metadata).encode(), np.uint8))
    sys.stdout.buffer.write(output.getvalue())


if __name__ == "__main__":
    main()

"""Start the loopback reader. No images or API keys are saved by this server."""
import argparse
import getpass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from comic_pipeline.reader_http import ReaderHTTPServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--key-prompt", action="store_true")
    parser.add_argument("--test-harness", action="store_true", help="Enable the local extension DOM test page")
    args = parser.parse_args()
    server = ReaderHTTPServer(("127.0.0.1", args.port))
    server.test_harness = args.test_harness
    if args.key_prompt:
        server.service.configure({"key": getpass.getpass("Gemini API key (memory only): ")})
    print(f"Comic reader: http://127.0.0.1:{args.port}/", flush=True)
    print("User-enabled rolling lookahead: 1-5 screens at a time; credentials and page images stay in memory.", flush=True)
    print("Selected work profiles, source text and reading summaries persist locally; images and keys do not.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

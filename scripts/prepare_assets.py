"""Fetch pinned upstream model assets and optional public OCR examples."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", action="store_true")
    parser.add_argument("--models-only", action="store_true")
    parser.add_argument("--include-base", action="store_true")
    args = parser.parse_args()
    from comic_pipeline.upstream import prepare_models
    prepare_models(include_base=args.include_base)
    if args.samples and not args.models_only:
        folder = ROOT / "samples" / "public"
        folder.mkdir(parents=True, exist_ok=True)
        source = "https://raw.githubusercontent.com/kha-white/manga-ocr/master/assets/examples/"
        entries = []
        for index in range(12):
            name = f"{index:02}.jpg"
            target = folder / name
            if not target.exists():
                urllib.request.urlretrieve(source + name, target)
            entries.append({"file": name, "source": source + name,
                            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
        (folder / "sources.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")
        print(f"Public OCR examples: {folder}", flush=True)


if __name__ == "__main__":
    main()

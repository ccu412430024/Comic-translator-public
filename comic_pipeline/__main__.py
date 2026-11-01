import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="日文漫畫 → 繁體中文：可重跑的偵測、OCR、翻譯、修復、嵌字管線")
    parser.add_argument("--input", type=Path, required=True, help="Image file or directory (recursive)")
    parser.add_argument("--output", type=Path, default=Path("outputs/project"))
    parser.add_argument("--book", default="my-book")
    parser.add_argument("--chapter", default=None)
    parser.add_argument("--translator", choices=["google-web", "compatible", "gemini", "file"], default="google-web")
    parser.add_argument("--translations", type=Path, help="Per-page translations for the file adapter")
    parser.add_argument("--edits", type=Path, help="JSON source/translation corrections indexed by page and block ID")
    parser.add_argument("--reference", type=Path, help="Reference translations using the same relative filenames")
    parser.add_argument("--font", type=Path)
    parser.add_argument("--render-style", choices=["outline"], default="outline",
                        help="contrast outlines preserve the cleaned artwork")
    parser.add_argument("--threshold", type=float, default=0.3)
    parser.add_argument("--detection-mode", choices=["standard", "tiled", "paddle"], default="standard")
    parser.add_argument("--tile-size", type=int, default=640)
    parser.add_argument("--tile-overlap", type=float, default=.25)
    parser.add_argument("--ocr-model", choices=["base", "mobile"], default="base")
    parser.add_argument("--input-mode", choices=["page", "text-crop"], default="page",
                        help="text-crop explicitly bypasses detection for already-cropped OCR samples")
    parser.add_argument("--mask-dilation", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--from-stage", choices=["detect", "ocr", "translate", "clean", "refine", "render"])
    args = parser.parse_args()
    if not 0 < args.threshold < 1 or args.mask_dilation < 1 or (args.limit is not None and args.limit < 1):
        parser.error("threshold must be between 0 and 1; dilation/limit must be positive")
    if args.tile_size < 64 or not 0 <= args.tile_overlap < .75:
        parser.error("tile-size must be >=64; tile-overlap must be in [0, .75)")
    try:
        from .pipeline import run
        return run(args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

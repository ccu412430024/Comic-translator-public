"""Check a generated public tree without models, credentials, or network access."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ".public-release.json"
IMAGE_PATHS = {"samples/smoke/01.png", "samples/smoke/02.png"}
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".json", ".html", ".css", ".md", ".ps1", ".cs", ".txt", ".toml", ".yml"}
FORBIDDEN_PARTS = {".git", ".runtime", "outputs", "reports", "research", "vendor", "user-provided", "public", "jmangabench-challenge", "ocr-challenge"}


def checked_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ValueError("Invalid release path")
    if any(part.lower() in FORBIDDEN_PARTS or part.lower().startswith(".venv") for part in path.parts):
        raise ValueError("Private/research path in public release: " + value)
    if value in IMAGE_PATHS or value in {".gitignore", ".gitattributes", ".githooks/pre-commit", ".githooks/pre-push", ".githooks/run-security"}:
        return path
    if path.suffix not in TEXT_SUFFIXES:
        raise ValueError("Unapproved public file type: " + value)
    return path


def working_paths(root):
    command = ["git", "-c", "safe.directory=" + root.as_posix(), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    result = subprocess.run(command, cwd=root, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("Cannot enumerate public Git files")
    names = {p.decode("utf-8") for p in result.stdout.split(b"\0") if p}
    return {name for name in names if (root / name).exists() or (root / name).is_symlink()}


def validate(root=ROOT):
    root = root.resolve()
    state = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if state.get("format") != "comic-public-snapshot-v1" or state.get("license") != "unspecified":
        raise ValueError("Unexpected release manifest")
    entries = state["files"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("Empty release manifest")
    files = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("Invalid release entry")
        name, sha = entry["path"], entry["sha256"]
        if not isinstance(name, str) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) or name in files:
            raise ValueError("Invalid release integrity record")
        files[name] = sha
    if len({name.casefold() for name in files}) != len(files):
        raise ValueError("Case-colliding public paths")
    expected = set(files) | {MANIFEST}
    actual = working_paths(root)
    if actual != expected:
        raise ValueError("Public file inventory differs from its manifest: " + ", ".join(sorted(actual ^ expected)))
    for name, expected_hash in files.items():
        relative = checked_path(name)
        path = root.joinpath(*relative.parts)
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Unsafe or missing public file: " + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError("Unreviewed public file change: " + name)
    return dict(files=len(files), synthetic_images=len(IMAGE_PATHS), license=state["license"], status="verified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        print(json.dumps(validate(args.repo)))
    except Exception as error:
        print("Public release check failed: " + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

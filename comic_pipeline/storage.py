from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class StageCache:
    def __init__(self, folder, force_from=None):
        self.folder = folder
        self.force_from = force_from
        self.events = []

    def run(self, stage, key, action, artifacts=()):
        stages = ["detect", "ocr", "translate", "clean", "refine", "render"]
        forced = self.force_from is not None and stages.index(stage) >= stages.index(self.force_from)
        path = self.folder / f"{stage}.json"
        cached = None
        if path.exists() and not forced:
            cached = json.loads(path.read_text(encoding="utf-8"))
        if cached and cached.get("key") == key and all((self.folder/a).is_file() for a in artifacts):
            self.events.append({"stage": stage, "cached": True})
            return cached["data"]
        import time
        started = time.perf_counter()
        data = action()
        seconds = round(time.perf_counter()-started, 3)
        write_json(path, {"key": key, "data": data, "seconds": seconds})
        self.events.append({"stage": stage, "cached": False, "seconds": seconds})
        return data


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS books(id TEXT PRIMARY KEY, source_lang TEXT, target_lang TEXT);
            CREATE TABLE IF NOT EXISTS pages(book_id TEXT, id TEXT, chapter TEXT, page_order INTEGER,
                filename TEXT, image_sha256 TEXT, status TEXT, updated_at TEXT,
                PRIMARY KEY(book_id,id), FOREIGN KEY(book_id) REFERENCES books(id));
            CREATE TABLE IF NOT EXISTS blocks(book_id TEXT, page_id TEXT, id TEXT, reading_order INTEGER,
                bbox_json TEXT, source_text TEXT, translation TEXT, metadata_json TEXT,
                PRIMARY KEY(book_id,page_id,id), FOREIGN KEY(book_id,page_id) REFERENCES pages(book_id,id));
        """)

    def save(self, book, source, target, page):
        with self.db:
            self.db.execute("""INSERT INTO books VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET
                source_lang=excluded.source_lang,target_lang=excluded.target_lang""", (book, source, target))
            # UPSERT instead of REPLACE avoids deleting a parent row with foreign keys.
            self.db.execute("""INSERT INTO pages VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(book_id,id) DO UPDATE SET
                chapter=excluded.chapter,page_order=excluded.page_order,filename=excluded.filename,
                image_sha256=excluded.image_sha256,status=excluded.status,updated_at=excluded.updated_at""",
                (book, page["id"], page["chapter"], page["order"], page["filename"], page["image_sha256"],
                 page["status"], datetime.now(timezone.utc).isoformat()))
            self.db.execute("DELETE FROM blocks WHERE book_id=? AND page_id=?", (book, page["id"]))
            for block in page.get("blocks", []):
                self.db.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?)", (book, page["id"], block["id"],
                    block["reading_order"], json.dumps(block["bbox"]), block.get("source_text", ""),
                    block.get("translation", ""), json.dumps(block, ensure_ascii=False)))

    def close(self):
        self.db.close()

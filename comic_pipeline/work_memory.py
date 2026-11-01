"""Local, transactional work profiles and source-anchored reading memory."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import unicodedata

from .memory_budget import MEMORY_TOKEN_BUDGET, bounded_context
from .page_identity import page_key as stable_page_key, source_page, split_page
from .reader_chronology import ChapterOrder, remember_order

ROOT = Path(__file__).resolve().parents[1]


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized(value):
    return "".join(unicodedata.normalize("NFKC", value).split())


EVIDENCE = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "STRING"}, "quote": {"type": "STRING"}}, "required": ["id", "quote"]}}
MEMORY_FACT = {"type": "OBJECT", "properties": {"text": {"type": "STRING"}, "evidence": EVIDENCE,
    "certainty": {"type": "STRING", "enum": ["explicit", "uncertain"]}}, "required": ["text", "evidence", "certainty"]}
MEMORY_SCHEMA = {"type": "OBJECT", "properties": {
    "scene": MEMORY_FACT, "facts": {"type": "ARRAY", "items": MEMORY_FACT},
    "terms": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "ja": {"type": "STRING"}, "zh_tw": {"type": "STRING"}, "evidence": EVIDENCE}, "required": ["ja", "zh_tw", "evidence"]}},
    "open_threads": {"type": "ARRAY", "items": MEMORY_FACT}}, "required": ["scene", "facts", "terms", "open_threads"]}


def grounded_delta(value, blocks):
    """Reject untraceable evidence; this checks provenance, not semantic entailment."""
    value = value if isinstance(value, dict) else {}
    originals = {b["id"]: normalized(b.get("source_text", "")) for b in blocks}
    def items(item, key, limit):
        value = item.get(key, [])
        return value[:limit] if isinstance(value, list) else []
    def evidence(item):
        result = []
        for e in items(item, "evidence", 10):
            if not isinstance(e, dict) or not isinstance(e.get("quote"), str):
                continue
            quote = normalized(e["quote"])
            if len(quote) >= 2 and quote in originals.get(e.get("id"), ""):
                result.append(dict(id=e["id"], quote=e["quote"][:1000]))
        return result
    def fact(item):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not (refs := evidence(item)):
            return None
        return dict(text=item["text"][:1500], certainty="explicit" if item.get("certainty") == "explicit" else "uncertain", evidence=refs)
    result = dict(scene=fact(value.get("scene")), facts=[], terms=[], open_threads=[])
    for key in ("facts", "open_threads"):
        result[key] = [f for item in items(value, key, 12) if (f := fact(item))]
    for item in items(value, "terms", 20):
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ("ja", "zh_tw")):
            continue
        refs = evidence(item)
        if refs and any(normalized(item["ja"]) in normalized(e["quote"]) for e in refs):
            result["terms"].append(dict(ja=item["ja"][:150], zh_tw=item["zh_tw"][:150], evidence=refs))
    return result


class WorkMemory:
    def __init__(self, path=None):
        path = path if path is not None else ROOT/".runtime/work-memory.sqlite3"
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        if str(path) != ":memory:" and self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='works'").fetchone():
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(works)")}
            backup = Path(path).with_name(Path(path).stem+"-before-library.sqlite3")
            if "data_version" not in columns and not backup.exists():
                destination = sqlite3.connect(str(backup))
                try:
                    self.db.backup(destination)
                finally:
                    destination.close()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS works(id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL,
            volume TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
            epoch TEXT NOT NULL, research_token TEXT NOT NULL DEFAULT '', current_segment TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS profiles(work_id TEXT REFERENCES works(id) ON DELETE CASCADE,
            revision INTEGER, profile TEXT NOT NULL, research TEXT NOT NULL, usage TEXT NOT NULL, model TEXT NOT NULL,
            created REAL NOT NULL, PRIMARY KEY(work_id,revision));
        CREATE TABLE IF NOT EXISTS segments(id TEXT PRIMARY KEY, work_id TEXT REFERENCES works(id) ON DELETE CASCADE,
            label TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS pages(work_id TEXT REFERENCES works(id) ON DELETE CASCADE,
            segment_id TEXT REFERENCES segments(id) ON DELETE CASCADE, page_key TEXT NOT NULL, seq INTEGER NOT NULL,
            context TEXT NOT NULL, blocks TEXT NOT NULL, delta TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(work_id,segment_id,page_key));
        CREATE TABLE IF NOT EXISTS translations(work_id TEXT REFERENCES works(id) ON DELETE CASCADE,
            segment_id TEXT REFERENCES segments(id) ON DELETE CASCADE, page_key TEXT, mode TEXT, model TEXT,
            revision INTEGER, blocks TEXT NOT NULL, PRIMARY KEY(work_id,segment_id,page_key,mode,model,revision));
        CREATE TABLE IF NOT EXISTS reader_books(ref TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
            title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS page_renders(work_id TEXT NOT NULL, segment_id TEXT NOT NULL,
            page_key TEXT NOT NULL, render_key TEXT NOT NULL,
            PRIMARY KEY(work_id,segment_id,page_key,render_key),
            FOREIGN KEY(work_id,segment_id,page_key) REFERENCES pages(work_id,segment_id,page_key) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS reader_chapters(work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
            chapter_ref TEXT NOT NULL, series_ref TEXT NOT NULL, ordinal INTEGER, updated REAL NOT NULL,
            PRIMARY KEY(work_id,chapter_ref));
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS pages_reading_order ON pages(work_id,segment_id,seq)")
        if "data_version" not in {row[1] for row in self.db.execute("PRAGMA table_info(works)")}:
            self.db.execute("ALTER TABLE works ADD COLUMN data_version INTEGER NOT NULL DEFAULT 0")
        chapter_columns={row[1] for row in self.db.execute("PRAGMA table_info(reader_chapters)")}
        for column in ("numbering_title","sub_title"):
            if column not in chapter_columns:self.db.execute("ALTER TABLE reader_chapters ADD COLUMN "+column+" TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def recover_interrupted_research(self):
        with self.transaction() as db:
            db.execute("UPDATE works SET status='interrupted',error='上次建檔因服務重啟而中斷，可重新搜尋。',research_token='' WHERE status='building'")

    def fork(self):
        """An in-memory speculative branch, never persisted as reading history."""
        clone = WorkMemory(":memory:")
        with self.lock:
            self.db.backup(clone.db)
        return clone

    def state_token(self, identifier, segment):
        with self.lock:
            work = self.get(identifier)
            rows = [tuple(r) for r in self.db.execute(
                "SELECT page_key,seq,blocks,delta FROM pages WHERE work_id=? AND segment_id=? ORDER BY seq", (identifier,segment))]
            return hashlib.sha256(packed([work["epoch"],work["revision"],rows]).encode()).hexdigest()

    @contextmanager
    def transaction(self):
        with self.lock:
            nested = self.db.in_transaction
            savepoint = "nested_"+secrets.token_hex(6)
            self.db.execute("SAVEPOINT "+savepoint if nested else "BEGIN")
            try:
                yield self.db
                if nested:
                    self.db.execute("RELEASE SAVEPOINT "+savepoint)
                else:
                    self.db.commit()
            except BaseException:
                if nested:
                    self.db.execute("ROLLBACK TO SAVEPOINT "+savepoint)
                    self.db.execute("RELEASE SAVEPOINT "+savepoint)
                else:
                    self.db.rollback()
                raise

    def create(self, title, author="", volume=""):
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise ValueError("請輸入完整作品名稱（最多 300 字）")
        if any(not isinstance(s, str) or len(s) > 200 for s in (author, volume)):
            raise ValueError("作者或卷數格式無效")
        identifier, segment, epoch = secrets.token_hex(12), secrets.token_hex(12), secrets.token_hex(12)
        with self.transaction() as db:
            db.execute("INSERT INTO works(id,title,author,volume,status,epoch,current_segment,created) VALUES(?,?,?,?,?,?,?,?)",
                       (identifier, title.strip(), author.strip(), volume.strip(), "empty", epoch, segment, time.time()))
            db.execute("INSERT INTO segments VALUES(?,?,?,?)", (segment, identifier, volume.strip() or "接續閱讀", time.time()))
        return self.get(identifier)

    def list(self):
        with self.lock:
            return [self._summary(row) for row in self.db.execute("SELECT * FROM works ORDER BY created DESC")]

    def _summary(self, row):
        result = dict(row)
        result.pop("research_token", None)
        result["pages"] = self.db.execute("SELECT COUNT(*) FROM pages WHERE work_id=?", (row["id"],)).fetchone()[0]
        return result

    def get(self, identifier):
        with self.lock:
            row = self.db.execute("SELECT * FROM works WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("找不到這部作品，請重新選擇")
            result = self._summary(row)
            profile = self.db.execute("SELECT * FROM profiles WHERE work_id=? AND revision=?", (identifier, row["revision"])).fetchone()
            result.update(profile=json.loads(profile["profile"]) if profile else None,
                research=json.loads(profile["research"]) if profile else None,
                usage=json.loads(profile["usage"]) if profile else [], research_model=profile["model"] if profile else "")
            result["segments"] = [dict(r) for r in self.db.execute("SELECT id,label,created FROM segments WHERE work_id=? ORDER BY created", (identifier,))]
            return result

    def start_research(self, identifier):
        with self.transaction() as db:
            work = self.get(identifier)
            if work["status"] == "building":
                return None
            token = secrets.token_hex(12)
            db.execute("UPDATE works SET status='building',error='',research_token=?,data_version=data_version+1 WHERE id=?", (token, identifier))
            return token

    def finish_research(self, identifier, token, profile, research, usage, model):
        with self.transaction() as db:
            row = db.execute("SELECT revision FROM works WHERE id=? AND research_token=?", (identifier, token)).fetchone()
            if row is None:
                return False
            previous = self.get(identifier)["profile"] or {}
            if "recent_story" in previous:
                profile = dict(profile, recent_story=deepcopy(previous["recent_story"]))
            revision = row[0]+1
            db.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?,?)", (identifier, revision, packed(profile), packed(research), packed(usage), model, time.time()))
            db.execute("UPDATE works SET revision=?,status='ready',error='',research_token='',epoch=?,data_version=data_version+1 WHERE id=?", (revision, secrets.token_hex(12), identifier))
            return True

    def research_error(self, identifier, token, message):
        with self.transaction() as db:
            db.execute("UPDATE works SET status='error',error=?,research_token='',data_version=data_version+1 WHERE id=? AND research_token=?", (message, identifier, token))

    def segment(self, identifier, label):
        if not isinstance(label, str) or len(label) > 200:
            raise ValueError("閱讀段落名稱格式無效")
        with self.transaction() as db:
            self.get(identifier)
            segment = secrets.token_hex(12)
            db.execute("INSERT INTO segments VALUES(?,?,?,?)", (segment, identifier, label.strip() or "新閱讀段落", time.time()))
            db.execute("UPDATE works SET current_segment=?,data_version=data_version+1 WHERE id=?", (segment, identifier))
        return self.get(identifier)

    def delete(self, identifier):
        with self.transaction() as db:
            db.execute("DELETE FROM works WHERE id=?", (identifier,))

    def selection(self, identifier, segment=""):
        with self.lock:
            work = self.get(identifier)
            segment = segment or work["current_segment"]
            if self.db.execute("SELECT 1 FROM segments WHERE id=? AND work_id=?", (segment, identifier)).fetchone() is None:
                raise ValueError("閱讀段落不屬於這部作品")
            return {k: work[k] for k in ("id", "title", "epoch", "revision")}, segment

    def saved_translation(self, selection, segment, page, mode, model):
        with self.lock:
            row = self.db.execute("SELECT blocks FROM translations WHERE work_id=? AND segment_id=? AND page_key=? AND mode=? AND model=? AND revision=?",
                                 (selection["id"], segment, page, mode, model, selection["revision"])).fetchone()
            return json.loads(row[0]) if row else None

    def context(self, selection, segment, page, blocks, identity=None, *, token_limit=MEMORY_TOKEN_BUDGET, cache_fixed=False):
        with self.lock:
            work = self.get(selection["id"])
            if work["epoch"] != selection["epoch"]:
                raise ValueError("作品記憶已變更，請重新翻譯")
            chronology=None
            if identity and identity["book_ref"].startswith("pixiv:episode:"):
                with self.transaction() as db:remember_order(db,work["id"],identity)
                chronology=ChapterOrder(self.db.execute("SELECT * FROM reader_chapters WHERE work_id=?",(work["id"],)).fetchall(),identity["book_ref"])
            keys = [stable_page_key(identity,p["index"]) for p in identity["pages"]] if identity else [page]
            placeholders = ",".join("?" for _ in keys)
            old = self.db.execute("SELECT context,seq FROM pages WHERE work_id=? AND segment_id=? AND page_key IN ("+placeholders+") ORDER BY seq LIMIT 1", (work["id"], segment, *keys)).fetchone()
            if old is None and identity:
                # Exact-pixel legacy records remain usable before lazy adoption.
                old = self.db.execute("SELECT context,seq FROM pages WHERE work_id=? AND segment_id=? AND page_key=?", (work["id"],segment,page)).fetchone()
            if old and not chronology:
                dynamic = json.loads(old[0])
            else:
                rows = self.db.execute("SELECT page_key,seq,blocks,delta FROM pages WHERE work_id=? AND segment_id=? ORDER BY seq", (work["id"], segment)).fetchall()
                if identity:
                    # A newly encountered earlier physical page must not learn
                    # later pages just because the user previously jumped ahead.
                    first = min(p["index"] for p in identity["pages"])
                    if chronology:
                        # Rebuild pre-page snapshots under the new chronology
                        # too: old snapshots may already contain later chapters.
                        rows=[r for r in rows if chronology.before(source_page(r["page_key"]),first) and (old is None or r["seq"]<old["seq"])]
                        rows=chronology.sort_rows(rows,source_page)
                    else:
                        rows = [r for r in rows if not ((s:=source_page(r["page_key"])) and s["book_ref"]==identity["book_ref"] and s["index"]>=first)]
                recent, history, terms, questions = [], [], {}, []
                query = normalized("".join(b.get("source_text", "") for b in blocks))
                grams = {query[i:i+2] for i in range(max(0, len(query)-1))}
                earlier = []
                for row in rows:
                    source_blocks, delta = json.loads(row["blocks"]), json.loads(row["delta"])
                    if row in rows[-5:]:
                        recent.append(dict(page=row["seq"],page_key=row["page_key"], dialogue=[{k:b.get(k, "") for k in ("id", "source_text", "translation")} for b in source_blocks]))
                    for item in ([delta.get("scene")] + delta.get("facts", [])):
                        if item and item.get("certainty") == "explicit":
                            text = normalized(item["text"]+"".join(e["quote"] for e in item["evidence"]))
                            rank = sum(g in text for g in grams)
                            earlier.append((rank, row["seq"], dict(page=row["seq"], **item)))
                    for term in delta.get("terms", []):
                        terms.setdefault(term["ja"], dict(page=row["seq"], **term))
                    for item in delta.get("open_threads", []):
                        questions.append(dict(page=row["seq"], **item))
                history = [x[2] for x in sorted(sorted(earlier, key=lambda x:(x[0],x[1]), reverse=True)[:35], key=lambda x:x[1])]
                dynamic = dict(recent_pages=recent, grounded_history=history,
                               observed_terms=list(terms.values())[-100:], unresolved_questions=questions[-10:],
                               continuity="Only pages processed in this reading segment; unseen gaps are unknown. Questions are not established facts.")
            profile_row = self.db.execute("SELECT profile FROM profiles WHERE work_id=? AND revision=?", (work["id"], selection["revision"])).fetchone()
            profile = json.loads(profile_row[0]) if profile_row else {}
            from .recent_story import visible_story
            from .story_outline import segment_story
            if "recent_story" in profile:
                chosen_story = segment_story(profile["recent_story"], segment)
                if chosen_story is None:profile.pop("recent_story", None)
                else:profile["recent_story"] = deepcopy(chosen_story)
            current_seq = old["seq"] if old else None
            if old and len(keys)>1:
                current_seq = self.db.execute("SELECT MAX(seq) FROM pages WHERE work_id=? AND segment_id=? AND page_key IN ("+placeholders+")",
                    (work["id"], segment, *keys)).fetchone()[0] or current_seq
            story=profile.get("recent_story")
            boundaries=None
            if chronology and isinstance(story,dict) and story.get("source"):
                # Legacy outlines kept only the latest 200 provenance pages.
                # Recover their full boundary from committed memory without
                # rewriting or regenerating the user's stored outline.
                boundaries=[source_page(r[0]) for r in self.db.execute("SELECT page_key FROM pages WHERE work_id=? AND segment_id=? AND seq<=?",(work["id"],segment,story["source"].get("last_seq",0)))]
            if not visible_story(story, segment, current_seq, identity, chronology=chronology,boundaries=boundaries):
                profile.pop("recent_story", None)
            return bounded_context(dict(work=dict(id=work["id"], title=work["title"], profile_revision=selection["revision"]),
                        fixed=profile, dynamic=deepcopy(dynamic)), "".join(b.get("source_text", "") for b in blocks),stable_fixed=True,token_limit=token_limit,cache_fixed=cache_fixed)

    def needs_page_adoption(self, selection, segment, identity):
        if not identity:return False
        with self.lock:
            return any(self.db.execute("SELECT 1 FROM pages WHERE work_id=? AND segment_id=? AND page_key=?",
                (selection["id"],segment,stable_page_key(identity,p["index"]))).fetchone() is None for p in identity["pages"])

    def commit_page(self, value):
        selection, segment, page = value["selection"], value["segment"], value["page"]
        with self.transaction() as db:
            row = db.execute("SELECT epoch FROM works WHERE id=?", (selection["id"],)).fetchone()
            if row is None or row[0] != selection["epoch"]:
                return False
            if db.execute("SELECT 1 FROM segments WHERE id=? AND work_id=?", (segment, selection["id"])).fetchone() is None:
                return False
            identity=value.get("page_identity")
            remember_order(db,selection["id"],identity)
            legacy_key=value.get("legacy_page",page)
            legacy=db.execute("SELECT seq,context,delta,created FROM pages WHERE work_id=? AND segment_id=? AND page_key=?",(selection["id"],segment,legacy_key)).fetchone() if identity else None
            observation=value["delta"]
            if legacy and value.get("adopt_cached"):
                observation=json.loads(legacy["delta"])
            parts=split_page(identity,value["blocks"],observation,value["image_size"]) if identity else [dict(page=page,blocks=value["blocks"],delta=observation)]
            for part in parts:
                # Cache adoption must never overwrite observations already
                # recorded for the same physical page from another layout.
                exists=db.execute("SELECT 1 FROM pages WHERE work_id=? AND segment_id=? AND page_key=?",(selection["id"],segment,part["page"])).fetchone()
                if not (exists and value.get("adopt_cached")):
                    seq = legacy["seq"] if legacy else db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM pages WHERE work_id=? AND segment_id=?", (selection["id"], segment)).fetchone()[0]
                    db.execute("INSERT OR IGNORE INTO pages VALUES(?,?,?,?,?,?,?,?)", (selection["id"], segment, part["page"], seq,
                        legacy["context"] if legacy else packed(value["context"]["dynamic"]),packed(part["blocks"]),packed(part["delta"]),legacy["created"] if legacy else time.time()))
                    # Preserve the original pre-page context and sequence on revisits.
                    db.execute("UPDATE pages SET blocks=?,delta=? WHERE work_id=? AND segment_id=? AND page_key=?", (
                        packed(part["blocks"]),packed(part["delta"]),selection["id"],segment,part["page"]))
                if identity:
                    db.execute("INSERT OR IGNORE INTO page_renders VALUES(?,?,?,?)",(selection["id"],segment,part["page"],page))
            if legacy:
                db.execute("DELETE FROM pages WHERE work_id=? AND segment_id=? AND page_key=?",(selection["id"],segment,legacy_key))
            # Translations stay keyed by render pixels. Coordinates from a
            # different resolution/spread must never be used for rendering.
            db.execute("INSERT OR REPLACE INTO translations VALUES(?,?,?,?,?,?,?)", (selection["id"], segment, page,
                value["mode"], value["model"], selection["revision"], packed(value["blocks"])))
            db.execute("UPDATE works SET data_version=data_version+1 WHERE id=?", (selection["id"],))
            return True

    def export(self, identifier):
        with self.lock:
            work = self.get(identifier)
            pages = [dict(r) for r in self.db.execute("SELECT segment_id,page_key,seq,blocks,delta,context,created FROM pages WHERE work_id=? ORDER BY created", (identifier,))]
            for page in pages:
                for key in ("blocks", "delta", "context"):
                    page[key] = json.loads(page[key])
            profiles=[dict(r) for r in self.db.execute("SELECT revision,profile,research,usage,model,created FROM profiles WHERE work_id=? ORDER BY revision",(identifier,))]
            for profile in profiles:
                for key in ("profile","research","usage"):
                    profile[key]=json.loads(profile[key])
            chapters=[dict(r) for r in self.db.execute("SELECT chapter_ref,series_ref,ordinal,numbering_title,sub_title FROM reader_chapters WHERE work_id=? ORDER BY series_ref,ordinal",(identifier,))]
            return dict(format="comic-work-memory-v1", work=work, profiles=profiles, pages=pages,reader_chapters=chapters)

    def close(self):
        with self.lock:
            self.db.close()

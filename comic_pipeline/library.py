"""SQLite catalogue and explicit, version-checked edits of local work data."""
import json
import re
import secrets
import time

from .work_memory import grounded_delta, normalized, packed
from .page_identity import source_page
from .memory_budget import MEMORY_TOKEN_BUDGET, MEMORY_TOKEN_CHOICES, SPLIT_CACHE_POLICY
from .reader_platforms import catalogue_ref, platform_id, PLATFORMS


class Conflict(ValueError):
    pass


def text(value, limit, label, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{label}格式無效（最多 {limit} 字）")
    return value.strip()


def object_value(value, limit=250000):
    if not isinstance(value, dict) or len(packed(value)) > limit:
        raise ValueError("資料須為 JSON 物件，且不可超過 250,000 字")
    return value


def manual_profile(value):
    object_value(value)
    for key in ("user_instructions", "title_ja", "title_zh_tw", "identity_notes"):
        if key in value:
            text(value[key], 15000, key)
    for key in ("translation_style", "authors", "uncertainties"):
        if key in value and (not isinstance(value[key], list) or not all(isinstance(x, str) for x in value[key])):
            raise ValueError(f"{key} 須為文字陣列")
    synopsis = value.get("synopsis", {})
    if synopsis is not None and not isinstance(synopsis, str) and (not isinstance(synopsis, dict) or not isinstance(synopsis.get("text", ""), str)):
        raise ValueError("synopsis 須包含文字內容")
    if "recent_story" in value:
        story = value["recent_story"]
        if not isinstance(story, (str, dict)):
            raise ValueError("故事大綱須為文字或包含 text 的物件。")
        text(story.get("text", "") if isinstance(story, dict) else story, 12000, "故事大綱")
        if isinstance(story, dict) and story.get("source") is not None:
            source = story["source"]
            if not isinstance(source, dict) or not isinstance(source.get("segment_id"), str) or not isinstance(source.get("pages"), list) or not 1 <= len(source["pages"]) <= 200:
                raise ValueError("近期故事的閱讀範圍格式無效。")
            for key in ("first_seq", "last_seq"):
                if type(source.get(key)) is not int or source[key] < 1:
                    raise ValueError("近期故事的頁序無效。")
            for page in source["pages"]:
                if not isinstance(page, dict) or not isinstance(page.get("page_key"), str) or type(page.get("seq")) is not int or page["seq"] < 1:
                    raise ValueError("近期故事的來源頁無效。")
            bounds=source.get("physical_boundaries", {})
            if not isinstance(bounds, dict) or len(bounds)>4096 or any(not isinstance(ref,str) or len(ref)>300 or type(index) is not int or index<0 for ref,index in bounds.items()):
                raise ValueError("故事大綱的原始頁範圍無效。")
        if isinstance(story, dict) and "other_segments" in story:
            archives=story["other_segments"]
            if not isinstance(archives,dict):raise ValueError("故事大綱的段落資料無效。")
            for segment, saved in archives.items():
                if not isinstance(segment,str) or not isinstance(saved,dict) or "other_segments" in saved:
                    raise ValueError("故事大綱的段落資料無效。")
                manual_profile(dict(recent_story=saved))
                if (saved.get("source") or {}).get("segment_id",saved.get("segment_id"))!=segment:
                    raise ValueError("故事大綱的段落資料無效。")
    for key in ("glossary", "characters"):
        if key not in value:
            continue
        if not isinstance(value[key], list) or len(value[key]) > 500:
            raise ValueError("人名與用語須為陣列，最多 500 筆")
        for term in value[key]:
            if not isinstance(term, dict):
                raise ValueError("人名與用語格式無效")
            for field, limit in (("ja", 150), ("zh_tw", 150), ("notes", 1600)):
                text(term.get(field, ""), limit, field, field != "notes")
    return value


class WorkLibrary:
    def __init__(self, memory):
        self.memory = memory

    def _platforms(self, db, identifier):
        refs = [r[0] for r in db.execute("SELECT ref FROM reader_books WHERE work_id=?", (identifier,))]
        # Older memories may have physical page IDs without a catalogue binding.
        for row in db.execute("SELECT page_key FROM pages WHERE work_id=?", (identifier,)):
            page = source_page(row[0])
            if page:
                refs.append(page["book_ref"])
        found = {platform_id(ref) for ref in refs}
        return [p for p in PLATFORMS if p in found]

    def allows(self, identifier, ref):
        with self.memory.lock:
            origins = self._platforms(self.memory.db, identifier)
            return not origins or origins == [platform_id(ref)]

    def assert_platform(self, identifier, ref):
        if identifier and platform_id(ref) and not self.allows(identifier, ref):
            raise ValueError("作品平台不符，請選擇目前平台的作品或建立新分類。")

    def assert_selection(self, work_id, catalogue_id="", ref=""):
        with self.memory.lock:
            for identifier in (work_id, catalogue_id):
                text(identifier, 64, "作品識別碼")
                self.assert_platform(identifier, ref)
            if work_id and catalogue_id:
                memory_origins = self._platforms(self.memory.db, work_id)
                catalogue_origins = self._platforms(self.memory.db, catalogue_id)
                if memory_origins and catalogue_origins and memory_origins != catalogue_origins:
                    raise ValueError("作品記憶與書籍的平台不同，請重新選擇作品。")

    def _books(self, db, identifier, detail=False):
        columns = "ref,title,created,updated" if detail else "ref,title"
        return [dict(row, platform=platform_id(row["ref"])) for row in db.execute(
            f"SELECT {columns} FROM reader_books WHERE work_id=? ORDER BY updated DESC", (identifier,))]

    def register(self, value):
        ref = text(value.get("ref", ""), 650, "書籍識別碼", True)
        if not catalogue_ref(ref):
            raise ValueError("書籍識別碼格式無效")
        title = text(value.get("title", ""), 300, "閱讀器書名")
        author = text(value.get("author", ""), 200, "作者")
        unnamed = f"未命名閱讀器作品（{ref[-8:]}）"
        with self.memory.transaction() as db:
            old = db.execute("SELECT * FROM reader_books WHERE ref=?", (ref,)).fetchone()
            identifier = value.get("work_id", "")
            text(identifier, 64, "作品識別碼")
            if old and (not identifier or value.get("prefer_existing")):
                identifier = old["work_id"]
            elif identifier and value.get("prefer_existing") and not self.allows(identifier, ref):
                # Ignore a stale Chrome binding from another platform on startup.
                identifier = ""
            if not identifier:
                matches = [w for w in self.memory.list() if title and normalized(w["title"]) == normalized(title)
                           and (not author or normalized(w["author"]) == normalized(author)) and self.allows(w["id"], ref)]
                identifier = matches[0]["id"] if len(matches) == 1 else self.memory.create(
                    title or unnamed, author)["id"]
            work = self.memory.get(identifier)
            self.assert_platform(identifier, ref)
            if title and work["title"] in {unnamed, f"未命名 BookWalker 書籍（{ref[-8:]}）"}:
                db.execute("UPDATE works SET title=?,data_version=data_version+1 WHERE id=?", (title, identifier))
                work["title"] = title
            now = time.time()
            display = title or (old["title"] if old else work["title"])
            db.execute("INSERT INTO reader_books VALUES(?,?,?,?,?) ON CONFLICT(ref) DO UPDATE SET "
                       "work_id=excluded.work_id,title=excluded.title,updated=excluded.updated",
                       (ref, identifier, display, now, now))
            if not old or old["work_id"] != identifier or old["title"] != display:
                db.execute("UPDATE works SET data_version=data_version+1 WHERE id=?", (identifier,))
            return dict(catalogue_id=identifier, catalogue_epoch=work["epoch"], title=work["title"], platform=platform_id(ref))

    def _stats(self, db, identifier):
        return dict(pages=db.execute("SELECT COUNT(*) FROM pages WHERE work_id=?", (identifier,)).fetchone()[0],
                    translations=db.execute("SELECT COUNT(*) FROM translations WHERE work_id=?", (identifier,)).fetchone()[0],
                    profiles=db.execute("SELECT COUNT(*) FROM profiles WHERE work_id=?", (identifier,)).fetchone()[0])

    def list(self):
        with self.memory.lock:
            db = self.memory.db
            result = []
            for work in self.memory.list():
                work.update(stats=self._stats(db, work["id"]), books=self._books(db, work["id"]),
                            platforms=self._platforms(db, work["id"]))
                result.append(work)
            return result

    def detail(self, identifier):
        with self.memory.lock:
            work = self.memory.get(identifier)
            work.update(stats=self._stats(self.memory.db, identifier), books=self._books(self.memory.db, identifier, detail=True),
                platforms=self._platforms(self.memory.db, identifier),
                profile_versions=[dict(r) for r in self.memory.db.execute(
                    "SELECT revision,model,created FROM profiles WHERE work_id=? ORDER BY revision DESC", (identifier,))])
            for segment in work["segments"]:
                segment["pages"] = self.memory.db.execute("SELECT COUNT(*) FROM pages WHERE work_id=? AND segment_id=?", (identifier, segment["id"])).fetchone()[0]
            return work

    def _page_source(self, identifier, key):
        source=source_page(key)
        if source and source["book_ref"].startswith("pixiv:episode:"):
            chapter=self.memory.db.execute("SELECT numbering_title,sub_title FROM reader_chapters WHERE work_id=? AND chapter_ref=?",(identifier,source["book_ref"])).fetchone()
            if chapter and any(chapter):
                source.update(chapter_title=" ".join(value for value in chapter if value),
                    numbering_title=chapter["numbering_title"],sub_title=chapter["sub_title"])
        return source

    def pages(self, identifier, segment="", offset=0, limit=25):
        with self.memory.lock:
            self.memory.get(identifier)
            offset, limit = int(offset), int(limit)
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError("分頁範圍無效")
            where, params = "work_id=?", [identifier]
            if segment:
                self.memory.selection(identifier, segment)
                where += " AND segment_id=?"
                params.append(segment)
            total = self.memory.db.execute("SELECT COUNT(*) FROM pages WHERE "+where, params).fetchone()[0]
            rows = self.memory.db.execute("SELECT segment_id,page_key,seq,created,blocks,delta FROM pages WHERE "+where+" ORDER BY created DESC,seq DESC,segment_id DESC,page_key DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
            pages = []
            for row in rows:
                page = dict(row)
                blocks, delta = json.loads(page.pop("blocks")), json.loads(page.pop("delta"))
                page.update(blocks=len(blocks), preview=" / ".join(b.get("translation", "") for b in blocks)[:200], scene=(delta.get("scene") or {}).get("text", ""),source_page=self._page_source(identifier,page["page_key"]))
                pages.append(page)
            return dict(pages=pages, total=total, offset=offset, limit=limit)

    def page(self, identifier, segment, key):
        with self.memory.lock:
            self.memory.selection(identifier, segment)
            row = self.memory.db.execute("SELECT * FROM pages WHERE work_id=? AND segment_id=? AND page_key=?", (identifier, segment, key)).fetchone()
            if row is None:
                raise ValueError("找不到這一頁的記憶")
            page = dict(row)
            for field in ("blocks", "delta", "context"):
                page[field] = json.loads(page[field])
            page["source_page"] = self._page_source(identifier,key)
            page["translations"] = [dict(r) for r in self.memory.db.execute("SELECT mode,model,revision,blocks FROM translations WHERE work_id=? AND segment_id=? AND (page_key=? OR page_key IN (SELECT render_key FROM page_renders WHERE work_id=? AND segment_id=? AND page_key=?))", (identifier, segment, key,identifier,segment,key))]
            for row in page["translations"]:
                row["blocks"] = json.loads(row["blocks"])
                mode, *suffixes = row["mode"].split(":")
                options=dict(s.split("=",1) for s in suffixes if "=" in s)
                tokens=options.get("memory", "")
                row.update(mode=mode, ocr_model=options.get("ocr", "base"),
                           memory_tokens=int(tokens) if tokens in {str(n) for n in MEMORY_TOKEN_CHOICES} else MEMORY_TOKEN_BUDGET,
                           memory_policy="explicit_split" if options.get("cache")==SPLIT_CACHE_POLICY else "combined")
            return page

    def _check(self, identifier, value):
        work = self.memory.get(identifier)
        if type(value.get("version")) is not int or value["version"] != work["data_version"]:
            raise Conflict("資料已在其他分頁或閱讀過程中更新，請重新載入後再儲存。")
        return work

    def _invalidate(self, db, identifier):
        db.execute("UPDATE works SET epoch=?,research_token='',status=CASE WHEN revision>0 THEN 'ready' ELSE 'empty' END,error='',data_version=data_version+1 WHERE id=?", (secrets.token_hex(12), identifier))

    def update(self, identifier, value):
        with self.memory.transaction() as db:
            work = self._check(identifier, value)
            title = text(value.get("title", work["title"]), 300, "書名", True)
            author = text(value.get("author", work["author"]), 200, "作者")
            volume = text(value.get("volume", work["volume"]), 200, "卷數")
            if "profile" in value:
                profile = manual_profile(value["profile"])
                story = profile.get("recent_story")
                if isinstance(story, dict) and story.get("source"):
                    self.memory.selection(identifier, story["source"]["segment_id"])
                if isinstance(story, dict):
                    if story.get("segment_id"):self.memory.selection(identifier, story["segment_id"])
                    for segment in story.get("other_segments", {}):self.memory.selection(identifier, segment)
                revision = work["revision"]+1
                db.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?,?)", (identifier, revision, packed(profile), packed(work["research"] or {}), "[]", "manual", time.time()))
                db.execute("UPDATE works SET revision=? WHERE id=?", (revision, identifier))
            db.execute("UPDATE works SET title=?,author=?,volume=? WHERE id=?", (title, author, volume, identifier))
            self._invalidate(db, identifier)
            return self.detail(identifier)

    def restore_profile(self, identifier, value):
        with self.memory.transaction() as db:
            work = self._check(identifier, value)
            row = db.execute("SELECT * FROM profiles WHERE work_id=? AND revision=?", (identifier, value.get("revision"))).fetchone()
            if row is None:
                raise ValueError("找不到固定資料版本")
            revision = work["revision"]+1
            db.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?,?)", (identifier, revision, row["profile"], row["research"], "[]", "manual-restore", time.time()))
            db.execute("UPDATE works SET revision=? WHERE id=?", (revision, identifier))
            self._invalidate(db, identifier)
            return self.detail(identifier)

    def edit_page(self, identifier, value, delete=False):
        with self.memory.transaction() as db:
            self._check(identifier, value)
            segment, key = value.get("segment_id", ""), value.get("page_key", "")
            page = self.page(identifier, segment, key)
            if not delete:
                supplied = value.get("blocks", [])
                if not isinstance(supplied, list) or len(supplied) != len(page["blocks"]) or [b.get("id") for b in supplied if isinstance(b, dict)] != [b["id"] for b in page["blocks"]]:
                    raise ValueError("文字區塊的數量與識別碼不可變更")
                for old, new in zip(page["blocks"], supplied):
                    old["source_text"] = text(new.get("source_text"), 10000, "原文")
                    old["translation"] = text(new.get("translation"), 10000, "譯文")
                raw = object_value(value.get("delta", {}))
                clean = grounded_delta(raw, page["blocks"])
                # A correction must keep every supplied fact anchored to this page.
                canonical = dict(scene=raw.get("scene"), facts=raw.get("facts", []), terms=raw.get("terms", []), open_threads=raw.get("open_threads", []))
                if packed(clean) != packed(canonical):
                    raise ValueError("記憶摘要的證據無效：每筆須引用本頁存在的文字區塊與至少兩字原文。")
                page["delta"] = clean
            rows = [dict(r) for r in db.execute("SELECT * FROM pages WHERE work_id=? AND segment_id=? ORDER BY seq", (identifier, segment))]
            self._invalidate(db, identifier)
            db.execute("DELETE FROM pages WHERE work_id=? AND segment_id=?", (identifier, segment))
            db.execute("DELETE FROM translations WHERE work_id=? AND segment_id=?", (identifier, segment))
            selection, _ = self.memory.selection(identifier, segment)
            # Replay only the earlier prefix, so edits never insert future facts
            # into a revisited page's original pre-page context.
            for row in rows:
                if row["page_key"] == key and delete:
                    continue
                blocks = page["blocks"] if row["page_key"] == key else json.loads(row["blocks"])
                delta = page["delta"] if row["page_key"] == key else json.loads(row["delta"])
                dynamic = self.memory.context(selection, segment, row["page_key"], blocks)["dynamic"]
                db.execute("INSERT INTO pages VALUES(?,?,?,?,?,?,?,?)", (identifier, segment, row["page_key"], row["seq"], packed(dynamic), packed(blocks), packed(delta), row["created"]))
            return self.detail(identifier)

    def clear(self, identifier, value, translations=False):
        with self.memory.transaction() as db:
            self._check(identifier, value)
            segment = value.get("segment_id", "")
            if segment:
                self.memory.selection(identifier, segment)
            where = "work_id=?"+(" AND segment_id=?" if segment else "")
            params = (identifier, segment) if segment else (identifier,)
            db.execute("DELETE FROM translations WHERE "+where, params)
            if not translations:
                db.execute("DELETE FROM pages WHERE "+where, params)
            self._invalidate(db, identifier)
            return self.detail(identifier)

    def segment(self, identifier, value):
        with self.memory.transaction() as db:
            self._check(identifier, value)
            if not value.get("segment_id"):
                self.memory.segment(identifier, text(value.get("label", ""), 200, "段落名稱"))
            else:
                _, segment = self.memory.selection(identifier, value["segment_id"])
                db.execute("UPDATE segments SET label=? WHERE id=?", (text(value.get("label"), 200, "段落名稱", True), segment))
            self._invalidate(db, identifier)
            return self.detail(identifier)

    def delete(self, identifier, value):
        with self.memory.transaction():
            self._check(identifier, value)
            self.memory.delete(identifier)
            return dict(deleted=True)

    def export(self, identifier):
        with self.memory.lock:
            result = self.memory.export(identifier)
            result.update(format="comic-library-work-v1", work=self.detail(identifier), translations=[dict(r) for r in self.memory.db.execute("SELECT * FROM translations WHERE work_id=?", (identifier,))])
            for row in result["translations"]:
                row["blocks"] = json.loads(row["blocks"])
            return result

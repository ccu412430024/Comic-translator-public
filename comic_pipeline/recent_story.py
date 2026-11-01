"""User-triggered cumulative story outlines from committed reading memory."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import secrets
import time

from .gemini import GeminiError, valid_model
from .library import Conflict
from .memory_budget import bounded_context
from .story_outline import (PAGE_CHOICES, LENGTH_CHOICES, MAX_STORY_CHARS, MAX_SOURCE_BYTES,
    OUTLINE_KIND, memory_digest, outline_prompt, page_evidence, segment_story, snapshot,
    store_story, story_text, validate_sections)
from .page_identity import source_page

ACTIVE = {"queued", "running"}


def visible_story(story, segment, current_seq=None, identity=None, *, chronology=None, boundaries=None):
    """Never turn a cumulative outline into later knowledge on earlier pages."""
    if not isinstance(story, dict) or not story.get("source"):
        return True
    source = story["source"]
    if source.get("segment_id") != segment:
        return False
    if current_seq is not None and source.get("last_seq", 0) > current_seq:
        return False
    if identity:
        last = max(page["index"] for page in identity["pages"])
        if chronology:
            originals=list(boundaries or [])+[dict(book_ref=ref,index=index) for ref,index in source.get("physical_boundaries",{}).items()]
            originals += [source_page(p.get("page_key","")) for p in source.get("pages",[])]
            for original in originals:
                comparison=chronology.compare(original["book_ref"]) if original else None
                if comparison is None or comparison>0 or comparison==0 and original["index"]>last:
                    return False
        if source.get("physical_boundaries", {}).get(identity["book_ref"], -1) > last:
            return False
        for page in source.get("pages", []):
            original = source_page(page.get("page_key", ""))
            if original and original["book_ref"] == identity["book_ref"] and original["index"] > last:
                return False
    return True


def fixed_prefix(profile):
    from .translation import stable_translation_prefix
    context = bounded_context(dict(fixed=profile, dynamic={}), stable_fixed=True, cache_fixed=True)
    return stable_translation_prefix(context)


class RecentStoryUpdates:
    def __init__(self, service, client_factory):
        self.service, self.client_factory = service, client_factory
        self.tasks = OrderedDict()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comic-story")

    def status(self, identifier, task_id=""):
        with self.service.lock:
            self.service.memory.get(identifier)
            candidates = [task for task in self.tasks.values() if task["work_id"] == identifier and (not task_id or task["id"] == task_id)]
            return deepcopy(candidates[-1]) if candidates else dict(state="idle")

    def start(self, identifier, value):
        service = self.service
        with service.lock, service.memory.lock:
            work = service.memory.get(identifier)
            if not (type(value.get("version")) is int and value.get("epoch") == work["epoch"]
                    and type(value.get("profile_revision")) is int and value["profile_revision"] == work["revision"]):
                work = service.library._check(identifier, value)
            current = self.status(identifier)
            if current["state"] in ACTIVE:
                return current
            if sum(task["state"] in ACTIVE for task in self.tasks.values()) >= 3:
                raise ValueError("故事大綱整理佇列已滿，請稍後再試。")
            model = value.get("model", service._model)
            if not valid_model(model):raise ValueError("請選擇有效的整理模型。")
            captured = snapshot(service.memory, identifier, value)
            if captured["unchanged"]:
                return dict(state="unchanged", message="沒有新增可整理的已讀記憶，大綱保持不變；未呼叫模型。",
                    revision=work["revision"], segment_id=captured["segment_id"], metrics=captured["previous"].get("metrics"))
            settings = dict(service._connection_snapshot(), key=service._key, model=model)
            if settings["gemini_backend"] == "vertex":settings["key"] = ""
            elif not settings["key"]:raise ValueError("請先在翻譯設定套用 Gemini API 金鑰。")
            task = dict(id=secrets.token_hex(12), work_id=identifier, state="queued", message="正在等待更新故事大綱…", model=model,
                page_count=len(captured["pages"]), batch_count=len(captured["batches"]), completed_batches=0,
                segment_id=captured["segment_id"], segment_label=captured["segment_label"], update_reason=captured["reason"], created=time.time())
            self.tasks[task["id"]] = task
            while len(self.tasks) > 32:
                key = next(key for key,item in self.tasks.items() if item["state"] not in ACTIVE)
                self.tasks.pop(key)
            self.pool.submit(self._run, task, deepcopy(work), captured, settings)
            return deepcopy(task)

    def _assert_current(self, work):
        current = self.service.memory.get(work["id"])
        if current["epoch"] != work["epoch"] or current["revision"] != work["revision"]:
            raise Conflict("整理期間固定資料或閱讀記憶已被修改，結果未覆蓋原資料，請重新更新。")
        return current

    def _run(self, task, work, captured, settings):
        service = self.service
        def report(**fields):
            with service.lock:task.update(fields)
        try:
            report(state="running", message=f"正在更新故事大綱，共 {len(captured['pages'])} 頁、{len(captured['batches'])} 批…")
            client = self.client_factory(settings)
            previous = captured["previous"]
            # Count this segment's actual fixed prefix, not another segment's outline.
            old_profile = deepcopy(work["profile"] or {})
            if story_text(previous):old_profile["recent_story"] = previous
            else:old_profile.pop("recent_story", None)
            try:prefix_before = client.count_tokens(fixed_prefix(old_profile))
            except GeminiError:prefix_before = None
            usage=[];story={};processed=0
            prompt_work = dict(title=work["title"], segment_label=captured["segment_label"],
                name_mappings=(work["profile"] or {}).get("glossary", []))
            for index, pages in enumerate(captured["batches"]):
                with service.lock, service.memory.lock:self._assert_current(work)
                report(message=f"正在整合故事主線：第 {index+1}/{len(captured['batches'])} 批（本批 {len(pages)} 頁）…")
                prompt, schema = outline_prompt(prompt_work, previous, pages, captured["length"])
                result, metadata = client.generate_json(prompt, schema=schema, max_output_tokens=24576)
                text, sections = validate_sections(result, pages, previous, captured["length"])
                usage.append(metadata)
                story = dict(kind=OUTLINE_KIND, text=text, sections=sections, source=deepcopy(captured["source"]),
                    model=settings["model"], backend=settings["gemini_backend"], created=time.time(),
                    target_characters=captured["length"])
                previous = story;processed += len(pages)
                report(completed_batches=index+1, processed_pages=processed)
            profile=deepcopy(work["profile"] or {})
            store_story(profile, story)
            report(message="正在計算故事大綱固定前綴的 token 數…")
            try:prefix_after = client.count_tokens(fixed_prefix(profile))
            except GeminiError:prefix_after = None
            story["metrics"] = dict(prefix_tokens_before=prefix_before, prefix_tokens=prefix_after, token_model=settings["model"],
                attempts=len(usage), batches=len(usage), characters=len(story["text"]), new_pages=len(captured["pages"]),
                total_pages=story["source"]["page_count"], checkpoint_before=captured["checkpoint"],
                checkpoint_after=captured["last_seq"], update_reason=captured["reason"],
                narrative_characters=len(story["text"]), dialogue_supplemented=False)
            from .work_memory import packed
            with service.lock, service.memory.transaction():
                current = self._assert_current(work)
                rows=service.memory.db.execute("SELECT page_key,seq,blocks,delta FROM pages WHERE work_id=? AND segment_id=? AND seq<=? ORDER BY seq",
                    (work["id"], captured["segment_id"], captured["last_seq"])).fetchall()
                if memory_digest(rows) != captured["memory_digest"]:
                    raise Conflict("整理期間來源頁內容已變更，大綱與納入進度皆未更新，請重新整理。")
                updated=service.library.update(work["id"], dict(version=current["data_version"], profile=profile))
                service.memory.db.execute("UPDATE profiles SET model=?,usage=? WHERE work_id=? AND revision=?",
                    ("story-outline:"+settings["model"], packed(usage), work["id"], updated["revision"]))
                service._discard_work_runtime(work["id"])
                task.update(state="done", message="累積故事大綱已更新，原有故事概要保持不變。請重新啟動閱讀頁翻譯。",
                    revision=updated["revision"], metrics=story["metrics"], finished=time.time())
        except (GeminiError, Conflict, ValueError) as error:
            report(state="error", message=str(error), finished=time.time())
        except Exception as error:
            report(state="error", message=f"故事大綱整理失敗（{type(error).__name__}），原有大綱與進度已保留。", finished=time.time())

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)

"""Small, speculative reading branches. Images and branches live only in RAM."""
from dataclasses import dataclass, field
import secrets
import threading
import time

from .memory_budget import MEMORY_TOKEN_BUDGET, memory_token_limit
from .reader_platforms import reader_ref

MAX_PREFETCH=5


@dataclass
class PrefetchSession:
    id: str
    book_ref: str
    options: dict
    memory: object
    expected: str
    entries: list = field(default_factory=list)
    active: bool = True
    activated: int = -1
    created: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    parallelism: int = 1
    lock: object = field(default_factory=threading.RLock,repr=False)
    gate: object = field(init=False,repr=False)

    def __post_init__(self):
        self.gate=threading.BoundedSemaphore(self.parallelism)

    @classmethod
    def create(cls, store, value):
        book = value.get("book_ref", "")
        if not reader_ref(book):
            raise ValueError("預翻譯需要可辨識的閱讀器書籍或章節 ID")
        parallelism=value.get("prefetch_parallelism",1)
        if type(parallelism) is not int or not 1<=parallelism<=MAX_PREFETCH:
            raise ValueError("同時預翻數量須為 1–5 個畫面")
        options = {key:value[key] for key in ("mode","ocr_model","translator","model","cleaning","layout","work_id","segment_id","catalogue_id","catalogue_epoch","gpu_epoch","gemini_cache","gemini_tier","prefetch_parallelism","gemini_backend","vertex_project","vertex_location") if key in value}
        options["memory_tokens"] = memory_token_limit(value.get("memory_tokens", MEMORY_TOKEN_BUDGET))
        if options.get("work_id"):
            _,segment = store.selection(options["work_id"],options.get("segment_id",""))
            options["segment_id"] = segment
            # Capture token and database snapshot under the same lock.
            with store.lock:
                expected = store.state_token(options["work_id"],segment)
                clone = store.fork()
        else:
            expected,clone = "",None
        return cls(secrets.token_hex(12),book,options,clone,expected,parallelism=parallelism)

    def context_for(self, selection, segment, pixel_key, blocks, identity, page_id):
        """Use committed memory plus available EARLIER source-only dialogue.

        Never read a later page or pretend that pending translations are known.
        The local stages are serialized by ReaderProcessor.local_lock.
        """
        with self.lock:
            if not self.active or self.memory is None:raise ValueError("預翻譯已停止")
            limit=self.options.get("memory_tokens", MEMORY_TOKEN_BUDGET)
            cache_fixed=self.options.get("gemini_cache")=="explicit"
            context=self.memory.context(selection,segment,pixel_key,blocks,identity,token_limit=limit,cache_fixed=cache_fixed)
            entry=next(e for e in self.entries if e["page_id"]==page_id)
            entry["source_blocks"]=[{k:str(b.get(k,""))[:400] for k in ("id","source_text")} for b in blocks[:80]]
            if self.parallelism>1:
                for prior in self.entries:
                    if prior is entry:break
                    if prior.get("staged") or not prior.get("source_blocks"):continue
                    context["dynamic"]["recent_pages"].append(dict(page=int(prior["page_id"]),
                        page_key="pending-source:"+prior["page_id"],dialogue=[dict(b,translation="") for b in prior["source_blocks"]]))
                context["dynamic"]["continuity"]="Only completed earlier pages are known. Pending earlier pages may contain OCR source dialogue only, with empty translations; no inferred events or facts."
                from .memory_budget import bounded_context
                context=bounded_context(context,"".join(b.get("source_text","") for b in blocks),stable_fixed=True,token_limit=limit,cache_fixed=cache_fixed)
            return context

    def complete(self, page_id, commit):
        """Stage only a contiguous completed prefix, regardless of finish order."""
        with self.lock:
            if not self.active:raise ValueError("預翻譯已停止")
            entry=next(e for e in self.entries if e["page_id"]==page_id)
            entry.update(done=True,commit=commit)
            staged=[]
            for prior in self.entries:
                if not prior.get("done"):break
                if prior.get("staged"):continue
                value=prior.get("commit")
                if value and self.memory:
                    if not self.memory.commit_page(value):
                        self.active=False
                        raise ValueError("作品記憶已變更，請重新開始預翻譯")
                    staged.append(value)
                prior["staged"]=True
            return staged

    def activate(self, store, page_id):
        with self.lock:
            return self._activate(store,page_id)

    def _activate(self, store, page_id):
        if not self.active:
            raise ValueError("預翻譯已停止，請重新開始")
        index = next((i for i,e in enumerate(self.entries) if e["page_id"]==page_id),None)
        if index is None or any(not e.get("done") for e in self.entries[:index+1]):
            raise ValueError("這個畫面的預翻譯尚未完成")
        self.touched = time.monotonic()
        if self.memory:
            with store.transaction():
                work,segment = self.options["work_id"],self.options["segment_id"]
                if store.state_token(work,segment) != self.expected:
                    self.active = False
                    raise ValueError("作品資料或閱讀記憶已變更，請重新開始預翻譯")
                if index <= self.activated:
                    return self.entries[index]["job"]
                # A jump activates only the translated prefix preceding that page.
                # Later speculative pages never enter the live memory.
                for entry in self.entries[self.activated+1:index+1]:
                    if entry.get("commit") and not store.commit_page(entry["commit"]):
                        raise ValueError("作品記憶已變更，請重新開始預翻譯")
                self.expected = store.state_token(work,segment)
        job = self.entries[index]["job"]
        self.activated = max(index,self.activated)
        self.touched = time.monotonic()
        # Retain a short revisit history plus the at-most-five unread screens.
        # The speculative database keeps its sequential context independently.
        discard = max(0,self.activated-3)
        if discard:
            del self.entries[:discard]
            self.activated -= discard
        return job

    def close(self):
        with self.lock:
            self.active = False
            if self.memory:
                self.memory.close()
                self.memory = None

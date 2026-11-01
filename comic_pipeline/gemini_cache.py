"""Bounded, opt-in cloud prefix caches. Credentials/handles stay in RAM only."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import hashlib
import math
import re
import threading
import time

from .gemini import GeminiError

CACHE_TTL_SECONDS=600
MAX_CLOUD_CACHES=4


@dataclass
class Entry:
    client: object
    scope: str
    expires: float
    tokens: int | None = None
    name: str = ""
    state: str = "unavailable"
    minimum: int | None = None
    renewal_state: str = ""
    cleanup_expires: float = 0


class GeminiCacheManager:
    def __init__(self, clock=time.time):
        self.clock=clock
        self.lock=threading.RLock()
        self.cleanup_lock=threading.Lock()
        self.entries=OrderedDict()
        self.pending={}

    def _prune(self):
        now=self.clock()
        for key,entry in list(self.entries.items()):
            if entry.expires<=now:
                self.entries.pop(key)
                if entry.name and entry.cleanup_expires>now:self.pending[entry.name]=entry
        for name,entry in list(self.pending.items()):
            if max(entry.expires,entry.cleanup_expires)<=now:self.pending.pop(name)

    def _delete(self, entry):
        if not entry.name or max(entry.expires,entry.cleanup_expires)<=self.clock():return True
        try:entry.client.delete_cached_content(entry.name)
        except GeminiError as error:
            if error.status_code!=404:return False
        return True

    def _retire(self, key):
        entry=self.entries.pop(key)
        if entry.name and not self._delete(entry):self.pending[entry.name]=entry

    def obtain(self, client, prefix, scope="generic"):
        fingerprint=hashlib.sha256((client._key+'\0'+client.model+'\0'+scope+'\0'+prefix).encode()).hexdigest()
        with self.lock:
            self._prune()
            entry=self.entries.get(fingerprint)
            if entry:
                self.entries.move_to_end(fingerprint)
                if not entry.name:
                    return self._lease(fingerprint,entry,entry.state)
                try:
                    requested_at=self.clock()
                    # An update can reach Google even if its reply is lost.
                    # Retain its handle for cleanup, without claiming renewal.
                    entry.cleanup_expires=max(entry.expires,requested_at+CACHE_TTL_SECONDS)
                    resource=client.update_cached_content(entry.name,CACHE_TTL_SECONDS)
                    value=resource.get("expireTime")
                    expires=datetime.fromisoformat(value.replace("Z","+00:00")).timestamp() if isinstance(value,str) else None
                    if expires is None or not math.isfinite(expires) or expires<=self.clock():
                        raise GeminiError("Gemini 快取續期未回報有效到期時間")
                    entry.expires=min(expires,requested_at+CACHE_TTL_SECONDS)
                    entry.cleanup_expires=entry.expires
                    entry.client=client;entry.renewal_state="renewed"
                except (ValueError,OverflowError):
                    entry.renewal_state="failed"
                except GeminiError as error:
                    if error.status_code==404 or error.code=="cache_reference_invalid":
                        # The remote copy no longer exists; recreate it once.
                        self.entries.pop(fingerprint,None)
                        entry=None
                    else:
                        # A failed update doesn't extend the known expiry.
                        entry.renewal_state="failed"
                if entry:
                    return self._lease(fingerprint,entry,"reused")
            # A changed revision/prefix for the same work retires the old copy.
            for key,old in list(self.entries.items()):
                if old.name and old.scope.split(":")[0]==scope.split(":")[0] and old.client.model==client.model and old.client._key==client._key:
                    self._retire(key)
            entry=Entry(client,scope,self.clock()+300)
            major=re.match(r"gemini-(\d+)",client.model)
            if major and int(major[1])>=4:
                entry.state="unsupported"
            else:
                try:
                    entry.tokens=client.count_tokens(prefix)
                    active=[key for key,item in self.entries.items() if item.name]
                    if len(active)+len(self.pending)>=MAX_CLOUD_CACHES and active:self._retire(active[0])
                    if sum(bool(e.name) for e in self.entries.values())+len(self.pending)>=MAX_CLOUD_CACHES:
                        entry.state="cleanup_pending"
                    else:
                        resource=client.create_cached_content(prefix,CACHE_TTL_SECONDS)
                        name=resource.get("name","")
                        if not client.valid_cache_name(name):
                            raise GeminiError("Gemini 快取回傳格式無效")
                        entry.name=name;entry.state="created";entry.renewal_state="created";entry.expires=self.clock()+CACHE_TTL_SECONDS
                        if isinstance(resource.get("expireTime"),str):
                            try:entry.expires=min(entry.expires,datetime.fromisoformat(resource["expireTime"].replace("Z","+00:00")).timestamp())
                            except (ValueError,OverflowError):pass
                except GeminiError as error:
                    entry.state={"cache_too_small":"too_small","cache_unsupported":"unsupported"}.get(error.code,"unavailable")
                    entry.minimum=error.minimum_tokens
                    entry.expires=self.clock()+(30 if error.status_code in {429,500,502,503,504} else 300)
            self.entries[fingerprint]=entry
            # Negative decisions are also bounded; never discard a live handle.
            while len(self.entries)>32:
                negative=next((k for k,e in self.entries.items() if not e.name),None)
                if negative is None:break
                self.entries.pop(negative)
            return self._lease(fingerprint,entry,entry.state)

    def _lease(self, key, entry, state):
        return dict(key=key,name=entry.name,state=state,prefix_tokens=entry.tokens,minimum_tokens=entry.minimum,
                    renewal_state=entry.renewal_state,
                    ttl_remaining_seconds=max(0,int(entry.expires-self.clock())) if entry.name else 0)

    def invalidate(self, key):
        with self.lock:
            if key in self.entries:self._retire(key)

    def status(self):
        with self.lock:
            self._prune()
            return dict(active=sum(bool(e.name) for e in self.entries.values()),pending_deletions=len(self.pending),
                        ttl_seconds=CACHE_TTL_SECONDS,ttl_policy="sliding",maximum=MAX_CLOUD_CACHES,
                        renewal_failures=sum(e.renewal_state=="failed" for e in self.entries.values()))

    def clear(self):
        with self.cleanup_lock:
            with self.lock:
                self._prune()
                resources={**self.pending,**{e.name:e for e in self.entries.values() if e.name}}
                self.entries.clear()
                # Count deletion-in-flight resources toward the billing cap.
                # Concurrent translation can create a cache only if room remains.
                self.pending.update(resources)
            with ThreadPoolExecutor(max_workers=MAX_CLOUD_CACHES) as pool:
                results=list(pool.map(self._delete,resources.values()))
            with self.lock:
                for (name,entry),ok in zip(resources.items(),results):
                    if ok and self.pending.get(name) is entry:self.pending.pop(name)
                self._prune()
                pending=len(self.pending)
            return dict(deleted=sum(results),pending_deletions=pending,
                message="目前已追蹤的雲端快取已清除；啟用時，後續翻譯會重新建立。" if not pending else "部分雲端快取未能刪除，可重試；它們仍會按原定時間到期。")

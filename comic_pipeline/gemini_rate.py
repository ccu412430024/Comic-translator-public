"""Bounded shared cooldowns for page translation and work research."""
from collections import OrderedDict
from dataclasses import dataclass
import math
import threading
import time


MAX_429_RETRIES = 4
MAX_GENERATION_ATTEMPTS = 8
MAX_RETRY_WAIT_SECONDS = 90


class RetryWaitExceeded(Exception):
    pass


@dataclass
class Window:
    until: float = 0
    next_send: float = 0
    spread_until: float = 0


class SharedCooldown:
    def __init__(self, clock=time.monotonic, spacing=.5):
        self.clock=clock;self.spacing=spacing
        self.lock=threading.Lock();self.windows=OrderedDict()

    def _key(self, key, now):
        if key in self.windows:return key
        overflow=self.windows.get('__overflow__')
        return '__overflow__' if len(self.windows)>=255 or overflow and overflow.spread_until>now else key

    def penalize(self, key, delay):
        with self.lock:
            now=self.clock()
            for old,item in list(self.windows.items()):
                if item.spread_until<now:self.windows.pop(old)
            key=self._key(key,now)
            window=self.windows.setdefault(key,Window())
            window.until=max(window.until,now+delay)
            window.spread_until=max(window.spread_until,window.until+60)
            self.windows.move_to_end(key)

    def wait(self, key, cancel, notify, budget):
        started=self.clock();last_notice=None
        while True:
            if cancel.is_set():return self.clock()-started
            with self.lock:
                now=self.clock()
                window=self.windows.get(self._key(key,now))
                remaining=max(0,window.until-now,window.next_send-now) if window else 0
                if remaining<=0:
                    if window and window.spread_until>now:window.next_send=now+self.spacing
                    return now-started
            if now-started+remaining>budget:raise RetryWaitExceeded()
            notice=math.ceil(remaining)
            if notice!=last_notice:
                notify(notice);last_notice=notice
            cancel.wait(min(.25,remaining))


SHARED_COOLDOWN=SharedCooldown()

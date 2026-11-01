"""Gemini SDK client with one cancellable page-level retry controller."""
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import math
import json
import os
import re
import random
import socket
import ssl
import threading
import time

import requests
from .genai_transport import request as sdk_request
from .gemini_rate import (SHARED_COOLDOWN,MAX_429_RETRIES,MAX_GENERATION_ATTEMPTS,
                          MAX_RETRY_WAIT_SECONDS,RetryWaitExceeded)


DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_RESEARCH_MODEL = DEFAULT_MODEL
# Public text+image model IDs verified against Google's model documentation.
# Presets do not imply that every API project has access to each model.
MODEL_PRESETS = [
    {"id": DEFAULT_MODEL, "name": "Gemini 3.5 Flash-Lite（預設）"},
    {"id": "gemini-3.6-flash", "name": "Gemini 3.6 Flash"},
    {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
    {"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro（預覽）"},
    {"id": "gemini-4-argon", "name": "Gemini 4 Argon"},
    {"id": "gemini-3.1-flash-lite", "name": "Gemini 3.1 Flash-Lite"},
]
API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
MAX_503_RETRIES = 3


def valid_model(model):
    return isinstance(model, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", model) is not None


class GeminiError(RuntimeError):
    def __init__(self, message, *, status_code=None, quota=None, code="", minimum_tokens=None,retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.quota = quota or []
        self.code=code
        self.minimum_tokens=minimum_tokens
        self.retry_after=retry_after


class GeminiCancelled(GeminiError):
    pass


def network_error_message(error, read_timeout=180):
    """Classify transport failures without formatting exceptions or URLs."""
    pending, seen, errors = [error], set(), []
    while pending and len(errors) < 32:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        errors.append(item)
        pending.extend(value for value in [getattr(item, "__cause__", None),
            getattr(item, "__context__", None), getattr(item, "reason", None), *getattr(item, "args", ())]
            if isinstance(value, BaseException))
    if any(getattr(item, "winerror", None) == 10013 or isinstance(item, PermissionError) for item in errors):
        return "Gemini 對外連線遭拒（Windows 10013／權限不足）。請從一般 PowerShell 啟動本機服務。"
    if isinstance(error, requests.exceptions.ProxyError):
        return "Gemini 代理伺服器連線失敗，請檢查本機代理設定。"
    if any(isinstance(item,(requests.exceptions.SSLError,ssl.SSLError)) for item in errors):
        return "Gemini TLS 憑證驗證失敗，請檢查系統時間、憑證及代理設定。"
    if isinstance(error, requests.exceptions.ConnectTimeout):
        return "Gemini 建立連線逾時，請檢查網路後重試。"
    if isinstance(error, requests.exceptions.ReadTimeout):
        return f"Gemini 等待回應逾時（{read_timeout} 秒），請稍後重試。"
    if any(isinstance(item, socket.gaierror) or type(item).__name__ == "NameResolutionError" for item in errors):
        return "無法解析 Google Gemini 的網域名稱，請檢查 DNS 與網路連線。"
    if isinstance(error, requests.exceptions.ConnectionError):
        return "無法連接 Google Gemini，請檢查網路、代理設定後重試。"
    if isinstance(error, requests.exceptions.Timeout):
        return "Gemini 網路請求逾時，請稍後重試。"
    return "Gemini 網路請求失敗，請使用連線測試確認原因。"


class GeminiClient:
    def __init__(self, key=None, model=DEFAULT_MODEL, cache_manager=None, cache_mode="off", service_tier="standard", *,
                 cancel_event=None,on_retry=None,on_status=None,rate_controller=None):
        self._key = (key or os.getenv("GEMINI_API_KEY", "")).strip()
        if not self._key:
            raise ValueError("Set GEMINI_API_KEY locally before using Gemini")
        if not valid_model(model):
            raise ValueError("Invalid Gemini model ID")
        self.model = model
        if cache_mode not in {"off","explicit"} or service_tier not in {"standard","priority"}:
            raise ValueError("無效的 Gemini 加速選項")
        self.cache_manager=cache_manager
        self.cache_mode=cache_mode
        self.service_tier=service_tier
        self._response_context=threading.local()
        self._last_service_tier="unknown"
        self.cancel_event=cancel_event if cancel_event is not None else threading.Event()
        self.on_retry=on_retry or (lambda event:None)
        self.on_status=on_status or (lambda event:None)
        self.rate_controller=rate_controller or SHARED_COOLDOWN

    def _begin_generation(self):
        self._response_context.retry_count=0
        self._response_context.retry_wait_seconds=0.0
        self._response_context.retry_429=0
        self._response_context.retry_503=0
        self._response_context.last_retry_status=503
        self._response_context.api_calls=0
        self._check_cancelled()

    def _check_cancelled(self):
        if self.cancel_event.is_set():raise GeminiCancelled("Gemini 翻譯已停止")

    def _retry_metadata(self):
        return dict(retry_count=getattr(self._response_context,"retry_count",0),
                    retry_wait_seconds=round(getattr(self._response_context,"retry_wait_seconds",0.0),3),
                    retry_429=getattr(self._response_context,"retry_429",0),
                    retry_503=getattr(self._response_context,"retry_503",0))

    def _rate_key(self):
        namespace="vertex:"+self.project+":"+self.location if getattr(self,"backend",None)=="vertex" else "developer:"+hashlib.sha256(self._key.encode()).hexdigest()
        return namespace+":"+self.model

    def _retry_event(self, status, *, state="requesting", remaining=0):
        attempt=getattr(self._response_context,"retry_429" if status==429 else "retry_503",0)
        return dict(status=status,attempt=attempt,maximum=MAX_429_RETRIES if status==429 else MAX_503_RETRIES,
                    state=state,remaining_seconds=remaining)

    def _wait_for_capacity(self):
        try:
            waited=self.rate_controller.wait(self._rate_key(),self.cancel_event,
                lambda remaining:self.on_retry(self._retry_event(429,state="waiting",remaining=remaining)),
                MAX_RETRY_WAIT_SECONDS-self._response_context.retry_wait_seconds)
        except RetryWaitExceeded:
            raise GeminiError("Gemini 429 冷卻等待已達本頁上限，請稍後重新翻譯。",status_code=429,code="retry_wait_exhausted") from None
        else:self._response_context.retry_wait_seconds+=waited
        self._check_cancelled()

    @property
    def _last_service_tier(self):
        return getattr(self._response_context,"service_tier","unknown")

    @_last_service_tier.setter
    def _last_service_tier(self,value):
        self._response_context.service_tier=value

    def _request(self, method, path, body=None, timeout=None):
        # Retry failed generation only. Cache creation is a separate billable
        # mutation and must never be repeated automatically on a 503.
        generation=method=="POST" and path.endswith(":generateContent")
        while True:
            if generation:
                self._check_cancelled()
                if self._response_context.api_calls>=MAX_GENERATION_ATTEMPTS:
                    raise GeminiError("Gemini 本頁已達 8 次請求上限，請稍後重試。",code="retry_exhausted")
            try:
                result=self._request_once(method,path,body,timeout)
                if generation:
                    self._check_cancelled()
                    self.on_status(dict(phase="response",retry_count=self._retry_metadata()["retry_count"]))
                return result
            except GeminiError as error:
                status=error.status_code
                if not generation or status not in {429,503} or error.code in {"quota_exhausted","retry_wait_exhausted"}:raise
                count=getattr(self._response_context,"retry_429" if status==429 else "retry_503",0)
                maximum=MAX_429_RETRIES if status==429 else MAX_503_RETRIES
                delay=0
                if status==429:
                    delay=max(min(16,2**(count+1))+random.uniform(0,1),error.retry_after or 0)
                    self.rate_controller.penalize(self._rate_key(),delay)
                if count>=maximum:
                    raise GeminiError(f"Gemini HTTP {status}：已自動重試 {maximum} 次，服務仍受限，請稍後再試。",
                        status_code=status,code="retry_exhausted",quota=error.quota) from None
                self._check_cancelled()
                self._response_context.retry_count+=1
                setattr(self._response_context,"retry_429" if status==429 else "retry_503",count+1)
                self._response_context.last_retry_status=status
                self._check_cancelled()
                self.on_retry(self._retry_event(status,state="waiting" if status==429 else "requesting",remaining=int(delay+.999)))
                self._check_cancelled()

    def _request_once(self, method, path, body=None, timeout=None):
        timeout = timeout or ((10, 20) if method in {"GET","DELETE"} else (15, 180))
        self._last_service_tier="unknown"
        url,headers,wire_body=self._transport(method,path,body)
        if method=="POST" and path.endswith(":generateContent"):
            self._check_cancelled()
            self._wait_for_capacity()
            self.on_status(dict(phase="request",**self._retry_metadata(),retry_status=self._response_context.last_retry_status))
            self._check_cancelled()
            self._response_context.api_calls+=1
        try:
            response = sdk_request(method, url,headers=headers,
                json=wire_body, timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            raise GeminiError(network_error_message(exc, timeout[1])) from None
        tier=response.headers.get("x-gemini-service-tier")
        self._last_service_tier=tier if isinstance(tier,str) and tier in {"standard","priority","flex"} else "unknown"
        if response.status_code==204:return {}
        try:
            data = response.json()
        except ValueError:
            raise GeminiError(f"Gemini returned non-JSON HTTP {response.status_code}",status_code=response.status_code) from None
        if response.status_code >= 300:
            error = data.get("error", {})
            # Inspect only for controlled classifications; never retain or
            # expose Google's arbitrary error message (it may echo inputs).
            detail=str(error.get("message", "")).lower()
            code="";minimum=None
            if path=="/cachedContents":
                if re.search(r"too (?:small|short)|min(?:imum)?[_ ](?:total[_ ])?token",detail):
                    code="cache_too_small"
                    match=re.search(r"min(?:imum)?[_ ](?:total[_ ])?token(?:[_ ]count)?\s*[=:]?\s*(\d{1,9})",detail)
                    if match:minimum=int(match[1])
                elif "support" in detail and ("cach" in detail or "model" in detail):code="cache_unsupported"
            elif body and body.get("cachedContent") and response.status_code in {400,403,404} and ("cached" in detail or "cache" in detail):
                code="cache_reference_invalid"
            if not code and body and body.get("service_tier")=="priority" and response.status_code in {400,403} and any(term in detail for term in ("priority","service_tier","service tier","tier 2","tier_2")):
                code="priority_unavailable"
            # Never persist arbitrary HTTP errors, request headers, URLs or response bodies.
            status = re.sub(r"[^A-Z_0-9]", "", str(error.get("status", "")))[:80]
            reasons = [re.sub(r"[^A-Z_0-9]", "", str(d.get("reason", "")))[:80]
                       for d in error.get("details", []) if isinstance(d, dict) and d.get("reason")]
            quota = []
            for detail in error.get("details", []):
                if not isinstance(detail, dict):
                    continue
                for violation in detail.get("violations", []) if isinstance(detail.get("violations"), list) else []:
                    if isinstance(violation, dict):
                        quota.append({k: re.sub(r"[^a-zA-Z0-9_./:-]", "", str(violation[k]))[:180]
                                      for k in ("quotaMetric", "quotaId", "quotaValue") if k in violation})
            message = f"Gemini HTTP {response.status_code}: {status}; reasons={','.join(reasons)}"
            retry_after=None
            if response.status_code == 429:
                message += "。請求受速率、配額或模型容量限制。"
                if quota:
                    message += " 限制："+", ".join(q.get("quotaId", q.get("quotaMetric", "")) for q in quota)
                if any(q.get("quotaValue")=="0" or re.search(r"per[_./-]?day|daily",q.get("quotaId","")+q.get("quotaMetric",""),re.I) for q in quota):
                    code="quota_exhausted"
                    message+="。明確的每日／零配額限制，不自動重試。"
                value=response.headers.get("retry-after")
                if isinstance(value,str):
                    try:retry_after=max(0,float(value))
                    except ValueError:
                        try:retry_after=max(0,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
                        except (ValueError,TypeError,OverflowError):pass
                for item in error.get("details",[]):
                    if isinstance(item,dict) and str(item.get("@type","")).endswith("google.rpc.RetryInfo"):
                        value=item.get("retryDelay")
                        if isinstance(value,str) and re.fullmatch(r"\d+(?:\.\d+)?s",value):retry_after=max(retry_after or 0,float(value[:-1]))
                if retry_after is not None and not math.isfinite(retry_after):retry_after=None
            raise GeminiError(message, status_code=response.status_code, quota=quota,code=code,minimum_tokens=minimum,retry_after=retry_after)
        return data

    def _transport(self, method, path, body):
        return API_ROOT+path,{"x-goog-api-key":self._key,"Content-Type":"application/json"},body

    def valid_cache_name(self, name):
        return isinstance(name,str) and re.fullmatch(r"cachedContents/[A-Za-z0-9_-]{1,160}",name) is not None

    def count_tokens(self, prefix):
        result=self._request("POST","/models/"+self.model+":countTokens",{"contents":[{"role":"user","parts":[{"text":prefix}]}]},timeout=(10,30))
        count=result.get("totalTokens")
        if type(count) is not int or count<0:raise GeminiError("Gemini token 計數回傳格式無效")
        return count

    def create_cached_content(self, prefix, ttl):
        return self._request("POST","/cachedContents",{"model":"models/"+self.model,
            "contents":[{"role":"user","parts":[{"text":prefix}]}],"ttl":str(ttl)+"s","displayName":"Comic translator fixed prefix"},timeout=(10,30))

    def delete_cached_content(self, name):
        if not self.valid_cache_name(name):raise ValueError("無效的 Gemini 快取識別碼")
        return self._request("DELETE","/"+name)

    def update_cached_content(self, name, ttl):
        if not self.valid_cache_name(name):raise ValueError("無效的 Gemini 快取識別碼")
        if type(ttl) is not int or ttl <= 0:raise ValueError("無效的 Gemini 快取存活時間")
        return self._request("PATCH","/"+name,{"ttl":str(ttl)+"s"},timeout=(5,10))

    def check_model(self):
        data = self._request("GET", "/models/" + self.model)
        return {key: data.get(key) for key in
                ("name", "displayName", "supportedGenerationMethods", "inputTokenLimit", "outputTokenLimit")}

    def generate_json(self, prompt, images=(), schema=None, max_output_tokens=8192, *, stable_prefix="", cache_scope="generic", uncached_prompt=None):
        started=time.perf_counter()
        self._begin_generation()
        if stable_prefix and not prompt.startswith(stable_prefix):raise ValueError("Gemini 固定前綴與翻譯提示詞不一致")
        if uncached_prompt is not None and (not stable_prefix or not isinstance(uncached_prompt,str) or not uncached_prompt.strip()):
            raise ValueError("Gemini 未快取提示詞格式無效")
        lease=dict(name="",state="off",prefix_tokens=None,minimum_tokens=None,ttl_remaining_seconds=0)
        if self.cache_mode=="explicit" and stable_prefix and self.cache_manager:
            self.on_status(dict(phase="cache",retry_count=0))
            lease=self.cache_manager.obtain(self,stable_prefix,cache_scope)
        elif self.cache_mode=="explicit":lease["state"]="no_prefix"
        config = {"temperature": 0, "maxOutputTokens": max_output_tokens, "responseMimeType": "application/json"}
        if schema:
            config["responseSchema"] = schema
        tier=self.service_tier;fallback=""
        while True:
            text_prompt=prompt[len(stable_prefix):] if lease["name"] else uncached_prompt if uncached_prompt is not None else prompt
            parts=[{"text":text_prompt}]
            parts.extend({"inlineData":{"mimeType":"image/png","data":base64.b64encode(value).decode("ascii")}} for value in images)
            body={"contents":[{"role":"user","parts":parts}],"generationConfig":config}
            if lease["name"]:body["cachedContent"]=lease["name"]
            if tier=="priority":body["service_tier"]="priority"
            try:
                data=self._request("POST","/models/"+self.model+":generateContent",body)
                break
            except GeminiError as error:
                if lease["name"] and error.code=="cache_reference_invalid":
                    self.cache_manager.invalidate(lease["key"]);lease=dict(lease,name="",state="expired_fallback")
                elif tier=="priority" and error.code=="priority_unavailable":
                    tier="standard";fallback="priority_unavailable"
                else:raise
        candidates = data.get("candidates", [])
        if not candidates or candidates[0].get("finishReason") != "STOP":
            raise GeminiError("Gemini did not finish a complete JSON response")
        text = "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])
                       if not part.get("thought"))
        try:
            value = json.loads(text)
        except ValueError:
            raise GeminiError("Gemini returned invalid JSON") from None
        usage = {k: v for k, v in data.get("usageMetadata", {}).items() if isinstance(v, (int, float))}
        cached=usage.get("cachedContentTokenCount")
        return value, {"requested_model": self.model, "model_version": data.get("modelVersion"), "usage": usage,
            "acceleration":dict(cache_mode=self.cache_mode,cache_state=lease["state"],prefix_tokens=lease["prefix_tokens"],
                minimum_tokens=lease["minimum_tokens"],cached_tokens=cached,ttl_remaining_seconds=lease["ttl_remaining_seconds"],
                context_policy=("cached_fixed" if lease["name"] else "combined") if uncached_prompt is not None else "",
                cache_renewal=lease.get("renewal_state",""),
                requested_tier=self.service_tier,submitted_tier=tier,actual_tier=self._last_service_tier,
                priority_fallback=fallback or ("server_downgrade" if self.service_tier=="priority" and self._last_service_tier=="standard" else ""),
                transport="google-genai",api_calls=self._response_context.api_calls,seconds=round(time.perf_counter()-started,4),**self._retry_metadata())}

    def search(self, prompt):
        """Grounded research only; page translation never enables web tools."""
        self._begin_generation()
        data = self._request("POST", "/models/"+self.model+":generateContent", {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 16384}})
        candidates = data.get("candidates", [])
        if not candidates or candidates[0].get("finishReason") != "STOP":
            raise GeminiError("作品搜尋未完整完成，請重試或更換建檔模型。")
        candidate = candidates[0]
        text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []) if not p.get("thought"))
        grounding = candidate.get("groundingMetadata", {})
        sources = []
        for chunk in grounding.get("groundingChunks", []):
            web = chunk.get("web", {})
            if web.get("uri"):
                sources.append(dict(id=f"s{len(sources)+1}", title=web.get("title", ""), url=web["uri"]))
        if not text.strip() or not sources:
            raise GeminiError("Gemini 未取得可追溯的網路來源，請補充作者、完整書名或更換建檔模型後重試。")
        usage = {k: v for k, v in data.get("usageMetadata", {}).items() if isinstance(v, (int, float))}
        return dict(text=text, sources=sources, supports=grounding.get("groundingSupports", []),
                    queries=grounding.get("webSearchQueries", []),
                    search_suggestions=grounding.get("searchEntryPoint", {}).get("renderedContent", "")), {
                        "requested_model": self.model, "model_version": data.get("modelVersion"), "usage": usage,
                        "retry":dict(self._retry_metadata(),transport="google-genai",api_calls=self._response_context.api_calls)}

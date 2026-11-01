"""Vertex AI Gemini REST transport and local Application Default Credentials."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import threading

from .gemini import DEFAULT_MODEL,GeminiClient,GeminiError,valid_model

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_LOCATION="global"
SCOPE="https://www.googleapis.com/auth/cloud-platform"


def valid_project(value):
    return isinstance(value,str) and (re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]",value) is not None or re.fullmatch(r"[0-9]{6,20}",value) is not None)


def valid_location(value):
    return isinstance(value,str) and (value=="global" or re.fullmatch(r"[a-z]{2,20}-[a-z]{2,20}[0-9]",value) is not None)


def connection_key(settings):
    if settings.get("gemini_backend","developer")=="vertex":
        return "vertex:"+settings.get("vertex_project","")+":"+settings.get("vertex_location",DEFAULT_LOCATION)
    return "developer"


def storage_model(settings):
    return (connection_key(settings)+":" if settings.get("gemini_backend")=="vertex" else "")+settings["model"]


def credential_path():
    explicit=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if explicit:return Path(explicit)
    config=os.environ.get("CLOUDSDK_CONFIG")
    if config:return Path(config)/"application_default_credentials.json"
    if os.name=="nt":return Path(os.environ.get("APPDATA",str(Path.home()/"AppData/Roaming")))/"gcloud/application_default_credentials.json"
    return Path.home()/".config/gcloud/application_default_credentials.json"


class VertexSession:
    def __init__(self, credentials):
        self.credentials=credentials
        self.identity=secrets.token_hex(16)
        self.lock=threading.Lock()

    def headers(self, project):
        try:
            from google.auth.transport.requests import Request
            class BoundedRequest(Request):
                def __call__(self,*args,**kwargs):
                    kwargs["timeout"]=15
                    return super().__call__(*args,**kwargs)
            with self.lock:
                if not self.credentials.valid:
                    self.credentials.refresh(BoundedRequest())
                token=self.credentials.token
                if not isinstance(token,str) or not token:raise ValueError()
            return {"Authorization":"Bearer "+token,"x-goog-user-project":project,"Content-Type":"application/json"}
        except Exception:
            raise GeminiError("Vertex AI 本機登入憑證無法更新，請重新執行 gcloud auth application-default login。",code="vertex_auth_refresh") from None


class VertexCredentials:
    def __init__(self):
        self.lock=threading.Lock();self.marker=None;self.session=None

    def status(self):
        try:installed=importlib.util.find_spec("google.auth") is not None
        except (ImportError,ValueError):installed=False
        try:configured=credential_path().is_file()
        except OSError:configured=False
        message="已找到本機 ADC 憑證，可測試 Vertex AI 連線。" if installed and configured else "尚未完成本機登入，請依設定頁指引執行 gcloud auth application-default login。" if installed else "本機缺少 google-auth，請更新專案依賴。"
        return dict(installed=installed,configured=configured,ready=installed and configured,message=message)

    def get(self):
        with self.lock:
            state=self.status()
            if not state["ready"]:raise GeminiError(state["message"],code="vertex_auth_missing")
            try:
                path=credential_path();stat=path.stat();marker=(str(path),stat.st_mtime_ns,stat.st_size)
                if self.session is None or marker!=self.marker:
                    from google.auth import load_credentials_from_file
                    credentials,_=load_credentials_from_file(str(path),scopes=[SCOPE])
                    self.session=VertexSession(credentials);self.marker=marker
                return self.session
            except Exception:
                raise GeminiError("Vertex AI 本機 ADC 憑證格式無效，請重新執行 gcloud auth application-default login。",code="vertex_auth_invalid") from None


class ConnectionStore:
    """Only non-secret connection preferences are persisted."""
    def __init__(self,path=None):self.path=Path(path) if path else None

    def load(self):
        try:
            value=json.loads(self.path.read_text(encoding="utf-8")) if self.path else {}
            return self.validate(value)
        except (OSError,ValueError,TypeError):return dict(gemini_backend="developer",vertex_project="",vertex_location=DEFAULT_LOCATION)

    @staticmethod
    def validate(value):
        if not isinstance(value,dict):raise ValueError("Gemini 連線設定格式無效")
        backend=value.get("gemini_backend","developer");project=value.get("vertex_project","");location=value.get("vertex_location",DEFAULT_LOCATION)
        if not isinstance(backend,str) or backend not in {"developer","vertex"}:raise ValueError("請選擇有效的 Gemini 連線方式")
        if not isinstance(project,str) or project and not valid_project(project):raise ValueError("GCP 專案 ID 格式無效")
        if not valid_location(location):raise ValueError("Vertex AI 區域格式無效")
        return dict(gemini_backend=backend,vertex_project=project,vertex_location=location)

    def save(self,value):
        value=self.validate(value)
        if self.path:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
            temporary.replace(self.path)


class VertexClient(GeminiClient):
    def __init__(self, project, location=DEFAULT_LOCATION, model=DEFAULT_MODEL, auth=None, **options):
        if not valid_project(project):raise ValueError("請先填入有效的 GCP 專案 ID")
        if not valid_location(location):raise ValueError("Vertex AI 區域格式無效")
        self.project,self.location=project,location
        self.backend="vertex"
        self.auth=(auth or VertexCredentials()).get()
        super().__init__(key="vertex:"+project+":"+location+":"+self.auth.identity,model=model,**options)
        self.origin="https://"+("" if location=="global" else location+"-")+"aiplatform.googleapis.com"
        self.parent="projects/"+project+"/locations/"+location

    def _transport(self,method,path,body):
        wire=deepcopy(body)
        if path.startswith("/models/"):
            suffix=path[len("/models/"):]
            name=suffix.split(":",1)[0]
            if not valid_model(name) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}(?::(?:generateContent|countTokens))?",suffix):raise ValueError("Vertex AI 模型名稱無效")
            target="/"+self.parent+"/publishers/google/models/"+suffix
        elif path=="/cachedContents":target="/"+self.parent+path
        elif path.startswith("/projects/") and self.valid_cache_name(path[1:]):target=path
        else:raise ValueError("無效的 Vertex AI API 路徑")
        if path.endswith(":generateContent"):
            self.on_status(dict(phase="auth",retry_count=self._retry_metadata()["retry_count"]))
        headers=self.auth.headers(self.project)
        if isinstance(wire,dict) and path.endswith(":generateContent"):
            tier=wire.pop("service_tier","standard")
            headers["X-Vertex-AI-LLM-Request-Type"]="shared"
            if tier=="priority":headers["X-Vertex-AI-LLM-Shared-Request-Type"]="priority"
            for tool in wire.get("tools",[]):
                if "google_search" in tool:tool["googleSearch"]=tool.pop("google_search")
        return self.origin+"/v1"+target,headers,wire

    def _request_once(self,*args,**kwargs):
        try:data=super()._request_once(*args,**kwargs)
        except GeminiError as error:
            if error.code:raise
            reasons={401:"本機登入已失效，請重新執行 gcloud auth application-default login。",
                403:"請確認專案已啟用 Vertex AI API、連結帳單，且登入帳戶有 Vertex AI User 與 Service Usage Consumer 權限。",
                404:"此模型在所選專案／區域不可用，請確認模型 ID，或改用 global 區域。",
                429:"請求受速率、專案配額或共享模型容量限制。"}
            if error.status_code in reasons:
                raise GeminiError("Vertex AI HTTP "+str(error.status_code)+"："+reasons[error.status_code],status_code=error.status_code,quota=error.quota,retry_after=error.retry_after) from None
            raise
        traffic=data.get("usageMetadata",{}).get("trafficType")
        self._last_service_tier={"ON_DEMAND":"standard","ON_DEMAND_PRIORITY":"priority"}.get(traffic,"unknown")
        return data

    def generate_json(self,*args,**kwargs):
        result,metadata=super().generate_json(*args,**kwargs)
        metadata.update(backend="vertex",project=self.project,location=self.location)
        metadata["acceleration"].update(backend="vertex",project=self.project,location=self.location)
        return result,metadata

    def search(self,*args,**kwargs):
        result,metadata=super().search(*args,**kwargs)
        metadata.update(backend="vertex",project=self.project,location=self.location)
        return result,metadata

    def valid_cache_name(self,name):
        return isinstance(name,str) and re.fullmatch(r"projects/(?:"+re.escape(self.project)+r"|[0-9]{1,20})/locations/"+re.escape(self.location)+r"/cachedContents/[A-Za-z0-9_-]{1,160}",name) is not None

    def create_cached_content(self,prefix,ttl):
        return self._request("POST","/cachedContents",dict(model=self.parent+"/publishers/google/models/"+self.model,
            contents=[dict(role="user",parts=[dict(text=prefix)])],ttl=str(ttl)+"s",displayName="Comic translator fixed prefix"),timeout=(10,30))

    def check_model(self):
        self._request("POST","/models/"+self.model+":countTokens",dict(contents=[dict(role="user",parts=[dict(text="connection check")])]),timeout=(10,20))
        return dict(name=self.parent+"/publishers/google/models/"+self.model,supportedGenerationMethods=["generateContent"],backend="vertex")

"""Bounded metadata-only diagnostic records for automatically observed readers."""
import time
from .reader_platforms import PLATFORMS


def diagnostic(value):
    if value.get("adapter") == "bookwalker-dom-v1":
        return value  # Preserve the existing diagnostic UI and its response format.
    if value.get("adapter") != "reader-v1" or value.get("platform") not in PLATFORMS:
        raise ValueError("閱讀器診斷格式無效")
    def safe(v):
        if isinstance(v, str):
            if any(prefix in v.lower() for prefix in ("https://", "http://", "data:image", "blob:")):
                return "[resource omitted]"
            return v[:500]
        if v is None or type(v) in (int, float, bool):return v
        if isinstance(v, list):return [safe(x) for x in v[:12]]
        if isinstance(v, dict):
            return {k:safe(x) for k,x in list(v.items())[:30] if k not in {"url","urls","resource_urls","image","sample","fingerprint","key","salt","api_key","access_token","hash","source_text","translation"}}
        return None
    fields=("adapter","platform","phase","rolling","run_id","current_position","window_positions","refilling","notice","calibration","native","error","frames","job_id")
    return {key:safe(value[key]) for key in fields if key in value}


def append_report(server, value):
    report=diagnostic(value)
    server.reader_diagnostics=report
    record=dict(received_at=time.time(),report=report)
    with server.reader_diagnostics_lock:
        server.reader_diagnostics_history=(server.reader_diagnostics_history+[record])[-32:]
    return report

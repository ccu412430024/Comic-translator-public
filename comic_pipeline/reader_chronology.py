"""Chapter chronology, independent of reading/commit sequence and page geometry."""
import hashlib
import json
import re
import time

from .reader_platforms import PIXIV_EPISODE, PIXIV_WORK

OFFICIAL = "pixiv-official-list-v1"
FALLBACK = "pixiv-id-fallback-v1"
POLICY = "pixiv-chapters-v1"


def validate_order(value, ref):
    if value is None:
        return None
    if not re.fullmatch(PIXIV_EPISODE, ref) or not isinstance(value, dict):
        raise ValueError("章節排序資料格式無效")
    series, source, chapters = value.get("work_ref"), value.get("source"), value.get("chapters")
    if not isinstance(series, str) or not re.fullmatch(PIXIV_WORK, series) or source not in (OFFICIAL, FALLBACK):
        raise ValueError("章節排序來源無效")
    if not isinstance(chapters, list) or len(chapters)>4096 or any(not isinstance(c,str) or not re.fullmatch(r"[1-9][0-9]{0,11}",c) for c in chapters):
        raise ValueError("官方章節列表格式無效")
    if len(set(chapters))!=len(chapters) or (source==OFFICIAL and not chapters) or (source==FALLBACK and chapters):
        raise ValueError("官方章節列表格式無效")
    result=dict(work_ref=series,source=source,chapters=list(chapters))
    if "titles" in value:
        titles=value["titles"]
        if not isinstance(titles,dict) or any(key not in chapters for key in titles):
            raise ValueError("章節名稱資料格式無效")
        result["titles"]={}
        for key,item in titles.items():
            if not isinstance(item,dict):raise ValueError("章節名稱資料格式無效")
            names={}
            for field,limit in (("numbering_title",120),("sub_title",300)):
                name=item.get(field,"")
                if not isinstance(name,str) or len(name)>limit:raise ValueError("章節名稱資料格式無效")
                names[field]=name.strip()
            result["titles"][key]=names
    return result


def geometry(identity):
    """A refreshed list must not change physical page/render identity."""
    return {k:identity[k] for k in ("book_ref", "pages")}


def cache_policy(identity):
    if not identity or not identity["book_ref"].startswith("pixiv:episode:"):
        return ""
    order=identity.get("chapter_order") or {}
    chapters=order.get("chapters", [])
    current=identity["book_ref"].split(":")[-1]
    # Publishing later chapters leaves existing render translations reusable;
    # changing the order before this chapter invalidates its old translation.
    prefix=chapters[:chapters.index(current)+1] if current in chapters else []
    value=[order.get("work_ref"),order.get("source"),prefix]
    return POLICY+":"+hashlib.sha256(json.dumps(value,separators=(",",":")).encode()).hexdigest()[:16]


def remember_order(db, work_id, identity):
    order=identity.get("chapter_order") if identity else None
    if not order:return
    now=time.time();series=order["work_ref"]
    if order["source"]==OFFICIAL:
        prior=[r[0].split(":")[-1] for r in db.execute("SELECT chapter_ref FROM reader_chapters WHERE work_id=? AND series_ref=? AND ordinal IS NOT NULL ORDER BY ordinal",(work_id,series))]
        if prior!=order["chapters"]:
            db.execute("UPDATE reader_chapters SET ordinal=NULL WHERE work_id=? AND series_ref=?",(work_id,series))
            for position,chapter in enumerate(order["chapters"]):
                db.execute("INSERT INTO reader_chapters(work_id,chapter_ref,series_ref,ordinal,updated) VALUES(?,?,?,?,?) ON CONFLICT(work_id,chapter_ref) DO UPDATE SET series_ref=excluded.series_ref,ordinal=excluded.ordinal,updated=excluded.updated",
                    (work_id,"pixiv:episode:"+chapter,series,position,now))
    db.execute("INSERT INTO reader_chapters(work_id,chapter_ref,series_ref,ordinal,updated) VALUES(?,?,?,NULL,?) ON CONFLICT(work_id,chapter_ref) DO UPDATE SET ordinal=NULL,series_ref=excluded.series_ref,updated=excluded.updated WHERE series_ref<>excluded.series_ref",
        (work_id,identity["book_ref"],series,now))
    for chapter,names in order.get("titles",{}).items():
        if not any(names.values()):continue
        db.execute("UPDATE reader_chapters SET numbering_title=?,sub_title=? WHERE work_id=? AND series_ref=? AND chapter_ref=? AND (numbering_title<>? OR sub_title<>?)",
            (names["numbering_title"],names["sub_title"],work_id,series,"pixiv:episode:"+chapter,names["numbering_title"],names["sub_title"]))


class ChapterOrder:
    def __init__(self, rows, current):
        self.chapters={r["chapter_ref"]:(r["series_ref"],r["ordinal"]) for r in rows}
        self.current=current

    def position(self, ref):
        series,ordinal=self.chapters.get(ref,("",None));identifier=int(ref.split(":")[-1])
        if ordinal is not None:return (ordinal,0,identifier)
        ranked=sorted((int(key.split(":")[-1]),rank) for key,(group,rank) in self.chapters.items() if group==series and rank is not None)
        if not ranked:return (identifier,0,identifier)
        # Insert an unlisted ID immediately before the nearest higher known
        # ID, while preserving the official relative order of every listed ID.
        following=next((rank for key,rank in ranked if key>identifier),max(rank for _,rank in ranked)+1)
        return (following,-1,identifier)

    def compare(self, ref):
        if ref==self.current:return 0
        a,b=self.chapters.get(ref),self.chapters.get(self.current)
        if not a or not b or a[0]!=b[0]:return None
        left,right=self.position(ref),self.position(self.current)
        return (left>right)-(left<right)

    def before(self, source, index):
        if not source:return False
        comparison=self.compare(source["book_ref"])
        return comparison is not None and (comparison<0 or comparison==0 and source["index"]<index)

    def sort_rows(self, rows, source_page):
        def key(row):
            page=source_page(row["page_key"])
            return self.position(page["book_ref"])+(page["index"],)
        return sorted(rows,key=key)

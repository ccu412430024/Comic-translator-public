"""Stable physical reader page identity, separate from render fingerprints."""
from copy import deepcopy
import math
from .reader_platforms import READER_REF as BOOK
from .reader_chronology import validate_order


def validate_identity(value):
    if value is None:
        return None
    if not isinstance(value,dict) or not isinstance(value.get("book_ref"),str) or not BOOK.fullmatch(value["book_ref"]):
        raise ValueError("閱讀器頁面識別格式無效")
    pages=value.get("pages")
    if not isinstance(pages,list) or not 1<=len(pages)<=2:
        raise ValueError("閱讀器原始頁序無效")
    result=[]
    for page in pages:
        if not isinstance(page,dict) or type(page.get("index")) is not int or not 0<=page["index"]<=100000:
            raise ValueError("閱讀器原始頁序無效")
        box=page.get("bbox")
        if not isinstance(box,list) or len(box)!=4 or any(type(n) not in (int,float) or not math.isfinite(n) or not 0<=n<=1 for n in box) or box[0]>=box[2] or box[1]>=box[3]:
            raise ValueError("閱讀器原始頁面範圍無效")
        result.append(dict(index=page["index"],bbox=box))
    if len({p["index"] for p in result})!=len(result):
        raise ValueError("閱讀器原始頁序重複")
    if len(result)==2:
        a,b=(p["bbox"] for p in result)
        overlap=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
        if overlap>0.001:raise ValueError("閱讀器原始頁面範圍重疊")
    identity=dict(book_ref=value["book_ref"],pages=sorted(result,key=lambda p:p["index"]))
    order=validate_order(value.get("chapter_order"),identity["book_ref"])
    if order:identity["chapter_order"]=order
    return identity


def page_key(identity, index):
    return identity["book_ref"]+":page:"+str(index)


def source_page(key):
    book,marker,index=key.rpartition(":page:")
    if marker and BOOK.fullmatch(book) and index.isascii() and index.isdigit():
        return dict(book_ref=book,index=int(index))
    return None


def split_page(identity, blocks, delta, size):
    width,height=size
    groups={p["index"]:[] for p in identity["pages"]}
    for block in blocks:
        box=block["bbox"]
        x1,y1,x2,y2=box[0]/width,box[1]/height,box[2]/width,box[3]/height
        def rank(page):
            l,t,r,b=page["bbox"]
            overlap=max(0,min(x2,r)-max(x1,l))*max(0,min(y2,b)-max(y1,t))
            distance=abs((x1+x2-l-r)/2)+abs((y1+y2-t-b)/2)
            return overlap,-distance
        target=max(identity["pages"],key=rank)
        groups[target["index"]].append(deepcopy(block))
    result=[]
    delta=delta or {}
    for page in identity["pages"]:
        part=groups[page["index"]]
        ids={b["id"] for b in part}
        def belongs(item):
            # Cross-page observations are omitted rather than retaining only
            # half their evidence and changing the meaning of the claim.
            return isinstance(item,dict) and bool(item.get("evidence")) and all(e.get("id") in ids for e in item["evidence"])
        observations=dict(scene=delta.get("scene") if belongs(delta.get("scene")) else None,
            **{key:[deepcopy(item) for item in delta.get(key,[]) if belongs(item)] for key in ("facts","terms","open_threads")})
        result.append(dict(page=page_key(identity,page["index"]),blocks=part,delta=observations))
    return result

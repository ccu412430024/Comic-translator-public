"""Public web discovery for Gemini accounts without built-in search access.

No page scripts run. Retrieved extracts are temporary model inputs, not saved
articles. DNS and every redirect are checked before fetching a public source.
"""
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from html import unescape
from html.parser import HTMLParser
import ipaddress
import re
import socket
from urllib.parse import quote, unquote, urlsplit, urljoin
import xml.etree.ElementTree as ET

import requests
from .research_identity import ResearchError, search_title

HEADERS={"User-Agent":"ComicTranslator/0.6 (local personal work research)"}


def public_url(url):
    try:
        parsed=urlsplit(url)
        if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.port not in {None,80,443}:
            return False
        addresses=socket.getaddrinfo(parsed.hostname,parsed.port or (443 if parsed.scheme=="https" else 80),type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except (ValueError,OSError):
        return False


def fetch(url, *, params=None, json_response=False):
    for _ in range(4):
        if not public_url(url):
            raise ValueError("Non-public source URL")
        with requests.get(url,params=params,headers=HEADERS,timeout=(5,12),allow_redirects=False,stream=True) as response:
            if response.status_code in {301,302,303,307,308}:
                url=urljoin(response.url,response.headers.get("Location",""));params=None;continue
            response.raise_for_status()
            data=bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data)>2*1024*1024:
                    raise ValueError("Source exceeds research size limit")
            if json_response:
                import json
                return json.loads(data)
            encoding=response.encoding
            if not encoding or encoding.lower()=="iso-8859-1":encoding="utf-8"
            return data.decode(encoding,errors="replace")
    raise ValueError("Too many source redirects")


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__();self.skip=0;self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in {"script","style","noscript","svg"}:self.skip+=1
    def handle_endtag(self,tag):
        if tag in {"script","style","noscript","svg"}:self.skip=max(0,self.skip-1)
        if tag in {"p","div","li","h1","h2","h3","br"}:self.parts.append("\n")
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)


def plain(html):
    parser=PlainText();parser.feed(html)
    return re.sub(r"[ \t\r\f\v]+"," ",unescape("".join(parser.parts)))[:20000]


def wiki_page(language, *, pageid=None,title=None):
    params=dict(action="query",prop="extracts|langlinks|extlinks",explaintext=1,lllimit=30,ellimit=60,format="json")
    params.update(pageids=pageid) if pageid else params.update(titles=title)
    data=fetch(f"https://{language}.wikipedia.org/w/api.php",params=params,json_response=True)
    pages=data.get("query",{}).get("pages",{})
    row=next((v for v in pages.values() if "missing" not in v),None)
    if not row:return None
    return dict(title=row["title"],url=f"https://{language}.wikipedia.org/wiki/"+quote(row["title"].replace(" ","_")),
                text=row.get("extract","")[:24000],links=[x["*"] for x in row.get("extlinks",[]) if x.get("*")],
                languages={x["lang"]:x["*"] for x in row.get("langlinks",[])})


def discover(work, queries):
    title=search_title(work["title"])
    sources,external,errors=[],[],[]
    def bing(query):
        try:
            xml=fetch("https://www.bing.com/search",params={"q":query,"format":"rss"})
            root=ET.fromstring(xml)
            return [dict(title=x.findtext("title") or "",url=x.findtext("link") or "",text=x.findtext("description") or "") for x in root.findall("./channel/item")[:5]]
        except (OSError,ValueError,requests.RequestException,ET.ParseError):return []
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(bing,queries[:3]))
    key=re.sub(r"\W","",title).lower()
    for result in results:
        for item in result:
            haystack=re.sub(r"\W","",item["title"]+unquote(item["url"])+item["text"]).lower()
            if key and key in haystack:external.append(item)
    # Wikipedia is both a title lookup and a way to discover primary/community
    # references when a generic search endpoint returns no relevant results.
    try:
        search=fetch("https://ja.wikipedia.org/w/api.php",params=dict(action="query",list="search",srsearch=title,srlimit=4,format="json"),json_response=True)
        candidates=search.get("query",{}).get("search",[])
        candidates.sort(key=lambda x:SequenceMatcher(None,title,x["title"]).ratio(),reverse=True)
        if candidates and SequenceMatcher(None,title,candidates[0]["title"]).ratio()>=.4:
            first=wiki_page("ja",pageid=candidates[0]["pageid"])
            if first:
                sources.append(first);external.extend(dict(url=url,title="關聯來源",text="") for url in first["links"])
                if first["languages"].get("zh"):
                    other=wiki_page("zh",title=first["languages"]["zh"])
                    if other:sources.append(other);external.extend(dict(url=url,title="關聯來源",text="") for url in other["links"])
    except (OSError,ValueError,requests.RequestException):errors.append("百科介面暫時無法連線")
    unique={item["url"]:item for item in external if item.get("url","").startswith("https://")}
    def score(item):
        url=unquote(item["url"])
        return (int(title in url),int(any(s in url for s in ("ichijinsha","bookwalker","publisher","/character","wikipedia"))))
    hosts=set();selected=[]
    for item in sorted(unique.values(),key=score,reverse=True):
        host=urlsplit(item["url"]).hostname
        if host not in hosts and host and not any(s in host for s in ("youtube.com","youtu.be","google.com","archive.org")):
            hosts.add(host);selected.append(item)
        if len(selected)>=6:break
    def retrieve(item):
        try:
            html=fetch(item["url"])
            title_tag=re.search(r"<title[^>]*>(.*?)</title>",html,re.I|re.S)
            return dict(title=plain(title_tag[1]).strip() if title_tag else item["title"],url=item["url"],text=plain(html))
        except (OSError,ValueError,requests.RequestException):return None
    with ThreadPoolExecutor(max_workers=3) as pool:
        sources.extend(item for item in pool.map(retrieve,selected) if item and len(item["text"].strip())>80)
    if not sources:raise ResearchError("no_public_sources","公開搜尋未取得符合書名且可讀取的來源；可稍後重試、補充作者，或改用 Gemini 內建搜尋。")
    return sources,errors

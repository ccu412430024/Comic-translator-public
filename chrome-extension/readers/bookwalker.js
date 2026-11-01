(()=>{
  "use strict";
  const uuid="[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}";
  function reference(url){try{const id=new URL(url).searchParams.get("cid");return new RegExp("^"+uuid+"$","i").test(id||"")?"bookwalker:"+id.toLowerCase():"";}catch{return "";}}
  function supported(url){try{const u=new URL(url);return u.protocol==="https:"&&u.hostname==="viewer.bookwalker.jp"&&/^\/03\/\d+\/viewer\.html$/.test(u.pathname)&&!!reference(url);}catch{return false;}}
  function context(doc,url,title,author=""){
    let parsed;try{parsed=new URL(url);}catch{return null;}
    if(!/(^|\.)bookwalker\.(jp|com\.tw)$/i.test(parsed.hostname))return null;
    const rendered=doc?.querySelector('#pagetitle .titleText')?.textContent?.trim()||doc?.querySelector('#pagetitle')?.getAttribute('title')?.trim();
    const clean=String(rendered||title||"").replace(/\s*[|｜│]\s*(?:BOOK.?WALKER|ブックウォーカー|電子書籍).*$/i,"")
      .replace(/\s*[-–—]\s*(?:BOOK.?WALKER|マンガ[（(]漫画[）)]).*$/i,"").trim();
    const name=/^(?:BOOK.?WALKER.*|電子書籍リーダー|読書|Reader)$/i.test(clean)?"":clean;
    let id="";
    for(const key of ["cid","bookId","book_id","contentId","content_id"]){const candidate=parsed.searchParams.get(key);if(candidate&&new RegExp("^(?:de)?"+uuid+"$","i").test(candidate)){id=candidate.replace(/^de/i,"").toLowerCase();break;}}
    if(!id)id=parsed.pathname.match(new RegExp("(?:^|/)(?:de)?("+uuid+")(?:/|$)","i"))?.[1]?.toLowerCase()||"";
    return {title:name.slice(0,300),author:String(author||"").slice(0,200),ref:id?"bookwalker:"+id:name?"title:"+name.normalize("NFKC").toLowerCase():""};
  }
  ComicReaders.register({id:"bookwalker",label:"BookWalker",probePath:"/test/bookwalker-probe",supported,reference,context,
    readerFiles:["bookwalker-reader.js"],readerGlobal:"ComicBookWalker",nativeFiles:["bookwalker-native.js"],nativeGlobal:"ComicBookWalkerNative"});
})();

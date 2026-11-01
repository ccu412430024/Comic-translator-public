(()=>{
  "use strict";
  function parsed(url){try{const u=new URL(url);return u.protocol==="https:"&&u.hostname==="comic.pixiv.net"?u:null;}catch{return null;}}
  function reference(url){const id=parsed(url)?.pathname.match(/^\/viewer\/stories\/([1-9]\d{0,11})\/?$/)?.[1];return id?"pixiv:episode:"+id:"";}
  function context(doc,url,title,author=""){
    const u=parsed(url);if(!u)return null;
    let props={};try{props=JSON.parse(doc?.querySelector('#__NEXT_DATA__')?.textContent||"{}").props?.pageProps||{};}catch{}
    const episodeId=reference(url).split(':').at(-1),validProps=!reference(url)||String(props.id)===episodeId;
    const image=doc?.querySelector('meta[property="og:image"]')?.content||"";
    const id=u.pathname.match(/^\/works\/([1-9]\d{0,11})\/?$/)?.[1]||(validProps?String(props.workId||""):"")||image.match(/\/ogimage\/works\/([1-9]\d{0,11})(?:[/?]|$)/)?.[1]||"";
    let name=String(title||(validProps?props.htmlMetaData?.title:"")||"").replace(/\s*[-–—|｜]\s*pixivコミック\s*$/i,"").trim();
    if(reference(url)&&/[|｜]/.test(name))name=name.split(/[|｜]/).at(-1).trim();
    if(/^pixivコミック$/i.test(name))name="";
    return {title:name.slice(0,300),author:String(author||"").slice(0,200),ref:/^[1-9]\d{0,11}$/.test(id)?"pixiv:work:"+id:"",
      reader_ref:reference(url),platform:"pixiv"};
  }
  ComicReaders.register({id:"pixiv",label:"pixivコミック",endLabel:"已到本章末",supported:url=>!!reference(url),reference,context,
    readerFiles:["readers/pixiv-image.js","readers/pixiv-reader.js"],readerGlobal:"ComicPixivReader",
    nativeFiles:["readers/pixiv-image.js","readers/pixiv-order.js","readers/pixiv-native.js"],nativeGlobal:"ComicPixivNative"});
})();

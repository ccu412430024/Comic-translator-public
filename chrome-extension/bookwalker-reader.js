/* Public rendered DOM only; no cookies, network interception or reader internals. */
(()=>{
  "use strict";
  function supported(url){try{const u=new URL(url);return u.protocol==="https:"&&u.hostname==="viewer.bookwalker.jp"&&/^\/03\/\d+\/viewer\.html$/.test(u.pathname);}catch{return false;}}
  function position(node){const b=node.getBoundingClientRect();return {x:Math.round(b.x),y:Math.round(b.y),width:Math.round(b.width),height:Math.round(b.height)};}
  function describe(node){
    const style=getComputedStyle(node),rect=position(node);
    const item={tag:node.tagName,parent:node.parentElement?.id||"",id:node.id||"",width:node.width||0,height:node.height||0,rect,
      display:style.display,visibility:style.visibility,opacity:style.opacity,transform:style.transform,
      page_attributes:Object.fromEntries(Array.from(node.attributes||[]).filter(a=>/^(?:data-)?(?:page|index|position)(?:[-_](?:index|number|id))?$/.test(a.name)&&/^\d{1,6}$/.test(a.value)).map(a=>[a.name,a.value]))};
    if(node.tagName==="IMG"){
      item.natural_width=node.naturalWidth;item.natural_height=node.naturalHeight;
      try{const u=new URL(node.currentSrc||node.src);item.source=u.protocol==="https:"?{kind:"https",host:u.hostname,path:u.pathname.slice(0,200)}:{kind:u.protocol};}catch{item.source={kind:"none"};}
    }
    try{const probe=document.createElement("canvas");probe.width=probe.height=32;const pen=probe.getContext("2d");pen.drawImage(node,0,0,32,32);const data=pen.getImageData(0,0,32,32).data;
      let min=255,max=0,hash=2166136261;for(let i=0;i<data.length;i+=4){const v=Math.round((data[i]+data[i+1]+data[i+2])/3);min=Math.min(min,v);max=Math.max(max,v);hash=Math.imul(hash^v,16777619);}
      item.readable=true;item.range=max-min;item.fingerprint=(hash>>>0).toString(16);
    }catch(error){item.readable=false;item.read_error=error.name==="SecurityError"?"protected-canvas":"not-ready";}
    return item;
  }
  function counter(doc=document){
    const text=doc.querySelector("#pageSliderCounter")?.textContent||"";
    const match=text.match(/(\d+)\s*\/\s*(\d+)/);
    return match?{position:Number(match[1]),total:Number(match[2])}:null;
  }
  function current(doc=document){
    if(!supported(location.href))throw new Error("已離開支援的 BookWalker 閱讀頁。");
    const count=counter(doc);if(!count)throw new Error("尚未取得 BookWalker 頁碼，請等畫面載入完成。");
    const candidates=Array.from(doc.querySelectorAll("#viewport0 > canvas,#viewport1 > canvas,#canvasCluster canvas"))
      .filter(n=>{const s=getComputedStyle(n),b=n.getBoundingClientRect();return n.width>=128&&n.height>=128&&b.width>64&&b.height>64&&s.display!=="none"&&s.visibility!=="hidden"&&Number(s.opacity)!==0;})
      .sort((a,b)=>{const area=n=>{const r=n.getBoundingClientRect();return Math.max(0,Math.min(innerWidth,r.right)-Math.max(0,r.left))*Math.max(0,Math.min(innerHeight,r.bottom)-Math.max(0,r.top));};return area(b)-area(a);});
    const node=candidates[0];if(!node)throw new Error("未找到已繪製的漫畫畫面。");
    const info=describe(node);if(!info.readable)throw new Error("這個畫布禁止直接讀取，仍可使用原本的目前畫面翻譯。");
    if(info.range<8)throw new Error("漫畫畫面仍在載入。");
    return {node,...count,rect:info.rect,fingerprint:"png-v2:"+info.fingerprint,width:node.width,height:node.height};
  }
  async function frame(doc=document){
    const state=current(doc);let background="rgb(255,255,255)";
    for(let n=state.node;n;n=n.parentElement){const color=getComputedStyle(n).backgroundColor;if(color&&color!=="transparent"&&!/^rgba\([^)]*,\s*0\)$/.test(color)){background=color;break;}}
    const copy=document.createElement("canvas");copy.width=state.width;copy.height=state.height;
    const pen=copy.getContext("2d");pen.fillStyle=background;pen.fillRect(0,0,copy.width,copy.height);pen.drawImage(state.node,0,0);
    const image=copy.toDataURL("image/png");
    if(image.length>16*1024*1024)throw new Error("預讀畫面過大，請改用目前畫面翻譯。");
    // Use the same PNG -> ImageBitmap -> OffscreenCanvas path as background
    // predictions. DOM canvas scaling can use a different GPU filtering path.
    const thumbnail=new OffscreenCanvas(96,64),brush=thumbnail.getContext("2d");
    const decoded=await createImageBitmap(await (await fetch(image)).blob());
    try{brush.drawImage(decoded,0,0,96,64);}finally{decoded.close();}
    const digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(image));
    const book=globalThis.ComicBookContext?.read(doc,location.href)?.ref?.slice(11)||"";
    const resource_urls=performance.getEntriesByType("resource").map(e=>e.name).filter(raw=>{try{const u=new URL(raw);return u.hostname==="bw-bv-epubs.bookwalker.jp"&&u.pathname.startsWith("/d_product/"+book+"/")&&/\.xhtml\.region\/[0-9a-f]+\.json$/i.test(u.pathname);}catch{return false;}}).slice(-40);
    return {...state,node:undefined,image,background,resource_urls,sample:Array.from(brush.getImageData(0,0,96,64).data),hash:Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,"0")).join("")};
  }
  function resourceInfo(entry){
    const u=new URL(entry.name);
    return {host:u.hostname,path:u.pathname.replace(/[A-Za-z0-9_-]{64,}/g,":opaque").slice(0,250),
      query_keys:Array.from(new Set(u.searchParams.keys())).slice(0,20),
      numeric_query:Object.fromEntries(Array.from(u.searchParams).filter(([k,v])=>/^(?:page|pageIndex|index|offset|number|p)$/i.test(k)&&/^\d{1,6}$/.test(v))),
      type:entry.initiatorType,bytes:entry.decodedBodySize||0};
  }
  function shape(value,depth=0){
    if(value===null)return "null";
    if(typeof value==="string")return {type:"string",length:value.length,encoding:/^data:image\//.test(value)?"image-data-uri":/^(?:\/9j\/|iVBORw0|UklGR)/.test(value)?"image-base64":"text-or-encoded"};
    if(typeof value!=="object")return typeof value;
    if(depth>=3)return Array.isArray(value)?{type:"array",length:value.length}:{type:"object",keys:Object.keys(value).slice(0,12)};
    if(Array.isArray(value))return {type:"array",length:value.length,first:value.length?shape(value[0],depth+1):null};
    return Object.fromEntries(Object.entries(value).slice(0,12).map(([k,v])=>[k,shape(v,depth+1)]));
  }
  function embeddedImages(value,depth=0){
    if(depth>6)return [];
    if(typeof value==="string"){
      if(/^data:image\/(?:jpeg|png|webp);base64,/.test(value))return [value];
      if(/^(?:\/9j\/|iVBORw0|UklGR)/.test(value))return ["data:image/"+(/^\/9j\//.test(value)?"jpeg":/^UklGR/.test(value)?"webp":"png")+";base64,"+value];
      return [];
    }
    if(!value||typeof value!=="object")return [];
    return Object.values(value).slice(0,50).flatMap(v=>embeddedImages(v,depth+1)).slice(0,4);
  }
  async function diagnose(doc=document,url=location.href){
    if(!supported(url))return {supported:false,reason:"此版本先支援 viewer.bookwalker.jp/03 的漫畫閱讀器。"};
    const counter=doc.querySelector("#pageSliderCounter")?.textContent||"";
    const nodes=Array.from(doc.querySelectorAll("#renderer canvas,#renderer img")).slice(0,32);
    const allEntries=performance.getEntriesByType("resource");
    const relevant=allEntries.filter(e=>{try{return /(^|\.)bookwalker\.jp$/.test(new URL(e.name).hostname);}catch{return false;}});
    const unique=Array.from(new Map(relevant.map(e=>[e.name,e])).values());
    const sample=unique.length>40?[...unique.slice(0,20),...unique.slice(-20)]:unique;
    const resources=sample.map(e=>({...resourceInfo(e),query_keys:resourceInfo(e).query_keys.slice(0,10)}));
    const entries=unique.filter(e=>new URL(e.name).hostname==="bw-bv-epubs.bookwalker.jp");
    const assets=entries.filter(e=>/\.(?:json|jpe?g|png|webp|avif|bin|dat)(?:\?|$)/i.test(e.name)&&!e.name.includes(".xhtml.region/"));
    const candidates=assets.length>1?[assets[0],assets[assets.length-1]]:assets;
    const probes=await Promise.all(candidates.map(async entry=>{
      const result=resourceInfo(entry);
      try{const response=await fetch(entry.name,{signal:AbortSignal.timeout(7000)});result.status=response.status;result.mime=response.headers.get("Content-Type")?.split(";")[0]||"";
        if(response.ok){let blob=await response.blob();if(blob.size>12*1024*1024)throw new Error("large");
          if(result.mime.includes("json")||new URL(entry.name).pathname.endsWith(".json")){
            const value=JSON.parse(await blob.text());result.json_shape=shape(value);const images=embeddedImages(value);result.embedded_image_count=images.length;
            if(images.length)blob=await (await fetch(images[0])).blob();
          }
          const bitmap=await createImageBitmap(blob);result.image_decodable=true;result.width=bitmap.width;result.height=bitmap.height;bitmap.close();}
      }catch{result.image_decodable=false;}
      return result;
    }));
    const origins={};for(const e of allEntries){try{const host=new URL(e.name).hostname;if(host)origins[host]=(origins[host]||0)+1;}catch{}}
    return {supported:true,adapter:"bookwalker-dom-v1",reader_path:new URL(url).pathname,resources,probes,origins,resource_count:allEntries.length,
      viewport:{width:innerWidth,height:innerHeight},counter:counter.replace(/[^\d/\s\-–]/g,"").slice(0,80),
      book_ref:globalThis.ComicBookContext?.read(doc,url)?.ref||"",
      cluster:!!doc.querySelector("#canvasCluster"),nodes:nodes.map(describe),
      controls:["leftTapArea","rightTapArea","pageSliderCounter","viewport0","viewport1","viewportW"].filter(id=>doc.getElementById(id))};
  }
  globalThis.ComicBookWalker={supported,diagnose,current,counter,frame,resourceInfo,shape,embeddedImages};
})();

/* Optional, book-scoped network metadata. Signed URLs stay in volatile RAM. */
export function resourceMetadata(raw){
  const u=new URL(raw);
  return {host:u.hostname,path:u.pathname.replace(/[A-Za-z0-9_-]{64,}/g,":opaque").slice(0,250),query_keys:Array.from(new Set(u.searchParams.keys())).slice(0,12)};
}
function shape(value,depth=0){
  if(value===null)return "null";
  if(typeof value==="string")return {type:"string",length:value.length,encoding:/^data:image\//.test(value)?"image-data-uri":"text-or-encoded"};
  if(typeof value!=="object")return typeof value;
  if(depth>=3)return Array.isArray(value)?{type:"array",length:value.length}:{type:"object",keys:Object.keys(value).slice(0,12)};
  if(Array.isArray(value))return {type:"array",length:value.length,first:value.length?shape(value[0],depth+1):null};
  return Object.fromEntries(Object.entries(value).slice(0,12).map(([k,v])=>[k,shape(v,depth+1)]));
}
export class BookWalkerNetwork{
  constructor(chrome,fetcher=fetch){this.chrome=chrome;this.fetch=fetcher.bind(globalThis);this.watches=new Map();this.installed=false;this.installing=null;}
  async install(){
    if(this.installed)return true;
    if(this.installing)return this.installing;
    this.installing=this._install();
    try{return await this.installing;}finally{this.installing=null;}
  }
  async _install(){
    if(!await this.chrome.permissions.contains({permissions:["webRequest"],origins:["https://bw-bv-epubs.bookwalker.jp/*"]}))return false;
    const saved=await this.chrome.storage?.session?.get("comicBookWalkerNetwork");
    for(const item of saved?.comicBookWalkerNetwork||[]){if(Date.now()-item.started<1800000)this.watches.set(item.tabId,{book:item.book,started:item.started,records:new Map(item.records)});}
    this.chrome.webRequest.onCompleted.addListener(details=>this.record(details),{urls:["https://bw-bv-epubs.bookwalker.jp/*"]});this.installed=true;return true;
  }
  persist(){
    // storage.session is browser RAM; it survives MV3 worker suspension only.
    this.chrome.storage?.session?.set({comicBookWalkerNetwork:Array.from(this.watches,([tabId,w])=>({tabId,book:w.book,started:w.started,records:Array.from(w.records)}))})?.catch(()=>{});
  }
  async watch(tabId,book){
    if(!/^bookwalker:[0-9a-f-]{36}$/.test(book))throw new Error("請在可辨識的 BookWalker 書籍使用。");
    if(!await this.install())throw new Error("請先允許 BookWalker 預讀的可選權限。");
    this.watches.set(tabId,{book,records:new Map(),started:Date.now()});
    this.persist();
    return {watching:true};
  }
  record(details){
    if(details.initiator?.startsWith("chrome-extension:"))return;
    let url;try{url=new URL(details.url);}catch{return;}
    if(url.hostname!=="bw-bv-epubs.bookwalker.jp")return;
    for(const [tabId,watch] of this.watches){
      if(Date.now()-watch.started>1800000){this.watches.delete(tabId);continue;}
      if(details.tabId!==tabId&&details.tabId!==-1)continue;
      const id=watch.book.slice("bookwalker:".length);
      if(!url.pathname.startsWith("/d_product/"+id+"/"))continue;
      watch.records.set(details.url,{...resourceMetadata(details.url),type:details.type,method:details.method||"GET",status:details.statusCode});
      while(watch.records.size>80)watch.records.delete(watch.records.keys().next().value);
    }
    this.persist();
  }
  async diagnose(tabId,book){
    const installed=await this.install();
    const watch=this.watches.get(tabId);
    if(!watch||watch.book!==book)return {watching:false,permission:installed,reason:watch?"different-book":"watch-not-started"};
    const records=Array.from(watch.records),candidates=records.filter(([u])=>!u.includes(".xhtml.region/")&&!u.includes("configuration_pack.json")).slice(-2);
    const probes=await Promise.all(candidates.map(async([url,metadata])=>{
      const result={...metadata,observed_status:metadata.status};let phase="fetch";
      try{const response=await this.fetch(url,{credentials:"omit",signal:AbortSignal.timeout(10000)});result.fetch_status=response.status;result.status=response.status;result.mime=response.headers.get("Content-Type")?.split(";")[0]||"";
        if(response.ok){const blob=await response.blob();result.bytes=blob.size;if(blob.size>12*1024*1024)throw new Error("large");
          if(result.mime.includes("json"))result.json_shape=shape(JSON.parse(await blob.text()));
          phase="decode";
          const bitmap=await createImageBitmap(blob);result.image_decodable=true;result.width=bitmap.width;result.height=bitmap.height;bitmap.close();}
      }catch(error){result.image_decodable=false;result.error_kind=error.name;result.error_phase=phase;result.error_reason=/Illegal invocation/.test(error.message)?"invalid-fetch-receiver":"request-or-decode-failed";}
      return result;
    }));
    return {watching:true,count:records.length,resources:records.slice(-18).map(([,m])=>m),probes};
  }
}

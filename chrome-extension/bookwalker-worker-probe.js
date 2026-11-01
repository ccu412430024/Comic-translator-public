/* Temporary observation of the reader's own worker protocol; no network changes. */
(()=>{
  "use strict";
  if(location.hostname!=="viewer.bookwalker.jp"||!/^\/03\/\d+\/viewer\.html$/.test(location.pathname))return;
  globalThis.__comicBookWalkerWorkerProbe?.stop();
  const original=globalThis.Worker?.prototype.postMessage,workers=new Map(),messages=[];
  const canvasRecords=new Map(),objectIds=new WeakMap(),restores=[];let nextId=1,drawCalls=0;
  function objectInfo(value){
    if(!value||typeof value!=="object")return {type:typeof value};
    if(!objectIds.has(value))objectIds.set(value,nextId++);
    const info={id:objectIds.get(value),type:value.constructor?.name||"object",width:value.width,height:value.height};
    if(typeof value.isConnected==="boolean")info.connected=value.isConnected;
    if(value.currentSrc||value.src){try{const url=new URL(value.currentSrc||value.src,location.href);info.source=url.protocol==="blob:"?"blob":url.protocol==="data:"?"data":url.hostname==="bw-bv-epubs.bookwalker.jp"?url.pathname.replace(/[A-Za-z0-9_-]{64,}/g,":opaque").slice(0,250):url.protocol;}catch{}}
    return info;
  }
  for(const prototype of [globalThis.CanvasRenderingContext2D?.prototype,globalThis.OffscreenCanvasRenderingContext2D?.prototype]){
    if(!prototype?.drawImage)continue;
    const native=prototype.drawImage;
    const hook=function(source){
      const result=Reflect.apply(native,this,arguments);
      try{
        drawCalls++;const target=objectInfo(this.canvas),key=target.id;
        if(!canvasRecords.has(key)&&canvasRecords.size<12)canvasRecords.set(key,{target,draws:0,sources:[]});
        const record=canvasRecords.get(key);
        if(record){record.target=target;record.draws++;const item={source:objectInfo(source),rect:Array.from(arguments).slice(1)};
          if(record.sources.length<3)record.sources.push(item);else record.last=item;}
      }catch{}
      return result;
    };
    prototype.drawImage=hook;restores.push(()=>{if(prototype.drawImage===hook)prototype.drawImage=native;});
  }
  function describe(value,depth=0,key=""){
    if(value===null)return "null";
    if(value instanceof ArrayBuffer)return {type:"ArrayBuffer",bytes:value.byteLength};
    if(ArrayBuffer.isView(value))return {type:value.constructor.name,length:value.length,bytes:value.byteLength};
    if(value instanceof ImageBitmap||value instanceof ImageData||value instanceof OffscreenCanvas)return {type:value.constructor.name,width:value.width,height:value.height};
    if(value instanceof Blob)return {type:"Blob",bytes:value.size,mime:value.type};
    if(typeof value==="string")return {type:"string",length:value.length,...(/^(?:decode|load|render|image|result|success|error|init)$/i.test(value)?{label:value}:{})};
    if(typeof value==="number")return /^(?:page|index|width|height|w|h|id|type|op|code)$/i.test(key)?value:"number";
    if(typeof value==="boolean")return value;
    if(typeof value!=="object")return typeof value;
    if(depth>=3)return Array.isArray(value)?{type:"array",length:value.length}:{type:"object",keys:Object.keys(value).slice(0,12)};
    if(Array.isArray(value))return {type:"array",length:value.length,first:value.length?describe(value[0],depth+1):null};
    return Object.fromEntries(Object.entries(value).slice(0,12).map(([k,v])=>[k,describe(v,depth+1,k)]));
  }
  function note(direction,value){try{let shape=describe(value);if(JSON.stringify(shape).length>1600)shape={type:"large-message",keys:Object.keys(value).slice(0,20)};messages.push({direction,shape});if(messages.length>10)messages.shift();}catch{}}
  function wrapped(message){
    if(!workers.has(this)){
      const worker=this,onmessage=worker.onmessage,listener=event=>note("reply-listener",event.data);
      const handler=function(event){note("reply",event.data);if(onmessage)Reflect.apply(onmessage,this,[event]);};
      if(onmessage)worker.onmessage=handler;else worker.addEventListener("message",listener);
      workers.set(worker,{onmessage,handler,listener});
    }
    note("request",message);return Reflect.apply(original,this,arguments);
  }
  if(original)Worker.prototype.postMessage=wrapped;
  const state={snapshot:()=>({workers:workers.size,messages:messages.slice(),rendering:{calls:drawCalls,canvases:Array.from(canvasRecords.values())}}),stop:()=>{
    if(original&&Worker.prototype.postMessage===wrapped)Worker.prototype.postMessage=original;
    for(const restore of restores)restore();
    for(const [worker,item] of workers){if(worker.onmessage===item.handler)worker.onmessage=item.onmessage;worker.removeEventListener("message",item.listener);}
  }};
  globalThis.__comicBookWalkerWorkerProbe=state;
  setTimeout(state.stop,180000);
})();

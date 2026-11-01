import {cropGeometry,meanDifference} from "./capture-utils.js";
import {BookWalkerNetwork} from "./bookwalker-network.js";
import {ReadAheadQueue} from "./read-ahead.js";
import {rgbDifference,sample} from "./frame-utils.js";
import "./gemini-status.js";
import "./gemini-connection.js";
import "./local-service.js";
import "./readers/registry.js";
import "./readers/bookwalker.js";
import "./readers/pixiv.js";
const platformFiles=["readers/registry.js","readers/bookwalker.js","readers/pixiv.js"];
async function installReader(tabId,url,watch=false){
  const adapter=globalThis.ComicReaders.match(url);if(!adapter)throw new Error("此平台尚未支援自動預翻譯。");
  await chrome.scripting.executeScript({target:{tabId},files:[...platformFiles,"book-context.js",...adapter.readerFiles,"reader-runtime.js",...(watch?["reader-content.js"]:[])]});
  return adapter;
}
const localService=globalThis.ComicLocalService?.create({runtime:chrome.runtime,fetch});
async function ensureLocalService(){return localService?localService.ensure():null;}
chrome.runtime.onStartup?.addListener(()=>{ensureLocalService().catch(()=>{});});
chrome.runtime.onInstalled?.addListener(()=>{ensureLocalService().catch(()=>{});});
const bookWalkerNetwork=chrome.permissions?new BookWalkerNetwork(chrome):null;
bookWalkerNetwork?.install().catch(()=>{});
const SERVER="http://127.0.0.1:8765";
const running=new Set();
const checking=new Set();
const ahead=new Map();
const restoringAhead=new Map();
let lastCapture=0;
let captureQueue=Promise.resolve();
let controlEpoch=0,releaseTask=null;
async function api(path,data){
  const response=await fetch(SERVER+path,data===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
  const value=await response.json();if(!response.ok)throw new Error(value.error||"本機服務處理失敗");return value;
}
async function notify(tabId,message){try{return await chrome.tabs.sendMessage(tabId,message);}catch{return null;}}
async function active(tab){const [current]=await chrome.tabs.query({active:true,windowId:tab.windowId});return current?.id===tab.id;}
function capture(tab){
  const task=captureQueue.then(async()=>{
    const wait=650-(Date.now()-lastCapture);if(wait>0)await new Promise(resolve=>setTimeout(resolve,wait));
    if(!await active(tab))throw new Error("分頁已切換，請回到漫畫頁後重新翻譯");
    lastCapture=Date.now();return await chrome.tabs.captureVisibleTab(tab.windowId,{format:"png"});
  });
  captureQueue=task.catch(()=>{});return task;
}
async function crop(dataUrl,viewport,rect){
  const response=await fetch(dataUrl);const bitmap=await createImageBitmap(await response.blob());
  const box=cropGeometry(bitmap.width,bitmap.height,viewport,rect);
  const canvas=new OffscreenCanvas(box.width,box.height);const context=canvas.getContext("2d");
  context.drawImage(bitmap,box.left,box.top,box.width,box.height,0,0,box.width,box.height);bitmap.close();
  const sample=new OffscreenCanvas(64,64);const pen=sample.getContext("2d");pen.drawImage(canvas,0,0,64,64);
  const fingerprint=pen.getImageData(0,0,64,64).data;
  const blob=await canvas.convertToBlob({type:"image/png"});return {blob,fingerprint};
}
async function toDataUrl(blob){
  const bytes=new Uint8Array(await blob.arrayBuffer());let binary="";
  for(let start=0;start<bytes.length;start+=32768)binary+=String.fromCharCode(...bytes.subarray(start,start+32768));
  return "data:image/png;base64,"+btoa(binary);
}
async function stopAhead(tabId){const state=ahead.get(tabId);if(state){state.canceled=true;ahead.delete(tabId);if(state.queue)await state.queue.stop();else if(state.restoring){await Promise.allSettled(state.restoring.frames.filter(f=>f.job&&["queued","running"].includes(f.job.state)).map(f=>api(`/api/jobs/${f.job.id}/cancel`,{})));if(state.restoring.session)await api(`/api/prefetch/${state.restoring.session}/cancel`,{}).catch(()=>{});}}await chrome.storage.session.remove?.("comicAhead-"+tabId);await notify(tabId,{type:"comic:reader:clear"});}
function releaseGpu(){
  if(releaseTask)return releaseTask;
  controlEpoch++;
  for(const state of ahead.values())state.canceled=true;
  releaseTask=(async()=>{
    // Pause the backend first, so late uploads cannot load models again even
    // when a tab or suspended MV3 worker is still winding down.
    const result=await api("/api/gpu/release",{});
    const saved=await chrome.storage.session.get(null);
    const tabs=new Set([...ahead.keys(),...restoringAhead.keys(),...running]);
    for(const key of Object.keys(saved||{}))if(/^comicAhead-\d+$/.test(key))tabs.add(Number(key.slice(11)));
    await Promise.all([...tabs].map(async tabId=>{await stopAhead(tabId);if(running.has(tabId))await notify(tabId,{type:"comic:clear"});}));
    return result;
  })().finally(()=>{releaseTask=null;});
  return releaseTask;
}
function persistAhead(state){
  if(state.canceled||!state.payload)return;
  // Metadata only: PNGs and credentials are excluded. Completed images remain
  // in the loopback service RAM and can be reacquired after an MV3 suspension.
  return chrome.storage.session.set({["comicAhead-"+state.tabId]:{version:2,id:state.id,options:state.options,payload:state.payload,notice:state.notice||"",updated:Date.now(),queue:state.queue?.snapshot()||null}}).catch(()=>{});
}
function reportAhead(state){
  persistAhead(state);
  return api("/api/reader-diagnostics",{adapter:"reader-v1",platform:state.adapter||"bookwalker",phase:"prefetch",rolling:true,run_id:state.id,current_position:state.queue?.current,window_positions:state.queue?.windowPositions||[],refilling:!!state.queue?.refilling,notice:state.notice||state.queue?.refillError||"",calibration:state.calibration||null,native:state.native||null,error:state.error||state.queue?.error||"",
    frames:state.queue?.frames.map(f=>({position:f.position,source_pages:f.source_pages,job_id:f.job?.id,state:f.job?.state,
      metrics:f.job?.metrics?{seconds:f.job.metrics.seconds,timings:f.job.metrics.timings,blocks:f.job.metrics.blocks,translated_blocks:f.job.metrics.translated_blocks}:undefined,
      activated:!!f.activated}))||[]}).catch(()=>{});
}
async function restoreAhead(tab){
  if(releaseTask)return null;
  if(restoringAhead.has(tab.id))return restoringAhead.get(tab.id);
  const epoch=controlEpoch;
  const task=(async()=>{
    const key="comicAhead-"+tab.id,saved=(await chrome.storage.session.get?.(key))?.[key];
    if(epoch!==controlEpoch)return null;
    if(saved?.version!==2||Date.now()-saved.updated>12*3600000||!globalThis.ComicReaders.matches(tab.url,saved.options.book_ref))return null;
    const state={id:saved.id,tabId:tab.id,adapter:globalThis.ComicReaders.match(tab.url).id,queue:null,canceled:false,options:saved.options,payload:saved.payload,notice:saved.notice,restoring:saved.queue};
    ahead.set(tab.id,state);
    if(saved.queue){
      const frames=[];let missingUnread=false;
      for(const entry of saved.queue.frames){
        if(state.canceled)return null;
        const frame={...entry};
        if(frame.job){try{
          frame.job=await api(`/api/jobs/${frame.job.id}`);
          if(frame.job.state==="done"){const response=await fetch(SERVER+frame.job.image_url);if(!response.ok)throw new Error();frame.translated=await toDataUrl(await response.blob());}
          else if(!["queued","running"].includes(frame.job.state))throw new Error();
        }catch{if(!frame.activated)missingUnread=true;continue;}}
        frames.push(frame);
      }
      if(state.canceled||ahead.get(tab.id)!==state)return null;
      if(frames.length&&!missingUnread){
        const prepared={frames:frames.map(f=>({...f})),positions:saved.queue.windowPositions};
        state.queue=aheadQueue(tab,state,prepared).restore(saved.queue,frames);
        state.queue.start();
      }else if(saved.queue.session){await api(`/api/prefetch/${saved.queue.session}/cancel`,{}).catch(()=>{});}
    }
    state.restoring=null;
    return state;
  })().catch(()=>null).finally(()=>restoringAhead.delete(tab.id));
  restoringAhead.set(tab.id,task);return task;
}
async function nativeFrames(tabId,book,frame,count,state,known_positions=[]){
  const descriptor=globalThis.ComicReaders.get(frame.adapter||state.adapter||"bookwalker");
  if(!descriptor)throw new Error("閱讀器適配尚未載入。");state.adapter=descriptor.id;
  await chrome.scripting.executeScript({target:{tabId},world:"MAIN",files:descriptor.nativeFiles});
  const result=await chrome.scripting.executeScript({target:{tabId},world:"MAIN",args:[{book_ref:book,position:frame.position,count,width:frame.width,height:frame.height,background:frame.background,layout:frame.layout,known_positions,native_global:descriptor.nativeGlobal}],
    func:async input=>{const adapter=globalThis[input.native_global];try{return adapter?await adapter.prepare(input):{error:"此閱讀器版本尚未提供圖片處理介面。"};}catch(error){return {error:error.message,native:adapter?.status()};}}});
  const prepared=result[0]?.result;state.native=prepared?.native||null;
  if(prepared?.error)throw new Error(prepared.error);
  if(prepared?.book_ref!==book||!prepared.frames?.length||prepared.frames[0].position!==frame.position)throw new Error("閱讀器回傳的書籍或頁碼未能對應。");
  let reference,candidate;
  try{
    reference=await createImageBitmap(await (await fetch(frame.image)).blob());
    candidate=await createImageBitmap(await (await fetch(prepared.frames[0].image)).blob());
    const difference=rgbDifference(sample(reference),sample(candidate));
    state.calibration={difference,native_renderer:true,source_pages:prepared.frames[0].source_pages};
    if(difference>4.5){const error=new Error("閱讀器繪圖與目前畫面未能對應，已停止預翻譯。");error.calibration=state.calibration;error.comparison={current:frame.image,candidate:prepared.frames[0].image,source:prepared.frames[1]?.image||prepared.frames[0].image};throw error;}
    for(const item of prepared.frames){const bitmap=await createImageBitmap(await (await fetch(item.image)).blob());try{item.sample=Array.from(sample(bitmap,96,64));}finally{bitmap.close();}}
    const {resource_urls,...current}=frame;
    prepared.frames[0]={...current,source_pages:prepared.frames[0].source_pages,page_identity:prepared.frames[0].page_identity};
    prepared.positions=prepared.positions||prepared.frames.map(f=>f.position);
    return prepared;
  }finally{reference?.close();candidate?.close();}
}
async function currentAheadFrame(tabId){
  const results=await chrome.scripting.executeScript({target:{tabId},func:async()=>({frame:await globalThis.ComicReader.frame(),book_ref:globalThis.ComicReader.reference()})});
  const result=results[0]?.result;if(!result?.frame)throw new Error("正在等待閱讀器畫面載入…");return result;
}
async function currentStableFrame(tab,ticket,select){
  if(select||!globalThis.ComicReaders.match(tab.url))return null;
  await installReader(tab.id,tab.url);
  const current=await currentAheadFrame(tab.id);
  if(current.book_ref!==(ticket.book_context?.reader_ref||ticket.book_context?.ref))throw new Error("書籍已變更，請重新翻譯。");
  const prepared=await nativeFrames(tab.id,current.book_ref,current.frame,0,{});
  return {...prepared.frames[0],rect:current.frame.rect};
}
function aheadQueue(tab,state,prepared,history=[]){
  const queue=new ReadAheadQueue({id:state.id,book_ref:state.options.book_ref,options:state.payload,frames:prepared.frames,history,lookAhead:state.options.prefetch_count,endLabel:globalThis.ComicReaders.get(state.adapter)?.endLabel,api,
    refill:async(position,known)=>{
      if(state.canceled||state.rebasing||state.queue!==queue)return null;
      const ticket=await currentAheadFrame(tab.id);
      if(ticket.book_ref!==state.options.book_ref)throw new Error("書籍已變更，請重新開始自動翻譯。");
      if(ticket.frame.position!==position)return null;
      const next=await nativeFrames(tab.id,ticket.book_ref,ticket.frame,state.options.prefetch_count,state,known);
      return !state.canceled&&state.queue===queue?next:null;
    },
    onUpdate:()=>{if(state.queue===queue)reportAhead(state);},
    image:async job=>{const response=await fetch(SERVER+job.image_url);if(!response.ok)throw new Error("預翻譯圖片已清除。");return toDataUrl(await response.blob());}});
  queue.windowPositions=prepared.positions;return queue;
}
async function retargetAhead(tab,state,message){
  if(state.canceled||state.rebasing||Date.now()<(state.rebaseRetryAt||0))return;
  const key=message.page_id+":"+(message.frame_hash||"");
  if(state.captureCandidate?.key!==key){state.captureCandidate={key,at:Date.now()};return;}
  if(Date.now()-state.captureCandidate.at<600)return;
  state.rebasing=true;
  try{
    await state.queue?.refilling;
    const ticket=await currentAheadFrame(tab.id);
    if(state.canceled||String(ticket.frame.position)!==message.page_id||message.frame_hash&&ticket.frame.hash!==message.frame_hash)return;
    if(ticket.book_ref!==state.options.book_ref)throw new Error("書籍已變更，請重新開始自動翻譯。");
    const history=state.queue?.history()||[];
    const prepared=await nativeFrames(tab.id,ticket.book_ref,ticket.frame,state.options.prefetch_count,state,history.map(f=>f.position));
    if(state.canceled)return;
    await state.queue?.stop();if(state.canceled)return;
    state.queue=aheadQueue(tab,state,prepared,history);state.notice="";state.captureCandidate=null;
    state.queue.start();await reportAhead(state);
  }catch(error){
    if(!state.canceled){state.notice=/尚未接上閱讀器/.test(error.message)?"請正常翻一個畫面，接上閱讀器後會自動開始。":error.message;state.rebaseRetryAt=Date.now()+2500;await reportAhead(state);}
  }finally{state.rebasing=false;}
}
async function startAhead(tab,options){
  const descriptor=globalThis.ComicReaders.match(tab.url);
  const state={id:crypto.randomUUID(),tabId:tab.id,adapter:descriptor?.id,queue:null,canceled:false,options};ahead.set(tab.id,state);
  try{
    const settings=await api("/api/settings");
    if(state.canceled)return;
    const supportsPrefetch=settings.reader_prefetch===true||descriptor?.id==="bookwalker"&&settings.bookwalker_prefetch&&(settings.bookwalker_prefetch_version||1)>=2;
    if(!supportsPrefetch)throw new Error("請重新啟動本機服務以啟用持續自動預翻譯。");
    if(descriptor?.id!=="bookwalker"&&!settings.reader_platforms?.includes(descriptor?.id))throw new Error("請更新本機服務以啟用此平台的自動預翻譯。");
    if((options.prefetch_parallelism||1)>(settings.prefetch_parallelism_max||1))throw new Error("請重新啟動新版本機服務以啟用同時預翻。");
    if(options.prefetch_count>(settings.prefetch_count_max||3))throw new Error("請重新啟動新版本機服務以啟用五畫面預翻。");
    if(options.translator==="gemini"&&!(settings.gemini_ready??settings.has_gemini_key))throw new Error(globalThis.ComicGeminiConnection?.problem(settings)||"請先設定 Gemini API 連線。");
    await installReader(tab.id,tab.url,true);
    await notify(tab.id,{type:"comic:clear"});
    const ticket=await notify(tab.id,{type:"comic:reader:prepare",run_id:state.id});
    if(ticket?.error)throw new Error(ticket.error);
    if(!ticket?.frame||ticket.book_ref!==options.book_ref)throw new Error("書籍已變更，請重新選擇作品。");
    state.payload={mode:options.mode,ocr_model:options.ocr_model||"base",translator:options.translator,model:options.model||settings.model,cleaning:options.cleaning||"standard",layout:options.layout||"contour",work_id:options.work_id||"",segment_id:options.segment_id||"",force_translation:!!options.force_translation,catalogue_id:options.catalogue_id||"",catalogue_epoch:options.catalogue_epoch||"",gpu_epoch:settings.gpu_epoch||0,
      gemini_cache:options.gemini_cache||"off",gemini_tier:options.gemini_tier||"standard",memory_tokens:options.memory_tokens??12000,prefetch_parallelism:options.prefetch_parallelism||1};
    Object.assign(state.payload,globalThis.ComicGeminiConnection?.payload(settings)||{});
    await notify(tab.id,{type:"comic:reader:watch",run_id:state.id});
    state.rebasing=true;
    const prepared=await nativeFrames(tab.id,ticket.book_ref,ticket.frame,options.prefetch_count,state);
    if(state.canceled)return;
    state.queue=aheadQueue(tab,state,prepared);state.rebasing=false;
    await state.queue.start();
  }catch(error){if(!state.canceled){const text=/尚未接上閱讀器/.test(error.message)?"請正常翻一個畫面，接上閱讀器後會自動開始。":error.message||"預翻譯失敗。";if(state.payload){state.notice=text;state.rebaseRetryAt=Date.now()+2500;}else state.error=text;state.calibration=error.calibration||state.calibration;const probePath=globalThis.ComicReaders.get(state.adapter)?.probePath;if(error.comparison&&probePath)await api(probePath,error.comparison).catch(()=>{});await reportAhead(state);await notify(tab.id,{type:"comic:reader:status",run_id:state.id,text});}}
  finally{state.rebasing=false;}
}
async function run(tab,options){
  const epoch=controlEpoch;
  let ticket=null,job=null;
  try{
    const settings=await api("/api/settings");
    if(epoch!==controlEpoch)return;
    if(options.translator==="gemini"&&!(settings.gemini_ready??settings.has_gemini_key))throw new Error(globalThis.ComicGeminiConnection?.problem(settings)||"請先設定 Gemini API 連線。");
    await chrome.scripting.executeScript({target:{tabId:tab.id},files:[...platformFiles,"book-context.js","page-state.js","gemini-status.js","content.js"]});
    ticket=await chrome.tabs.sendMessage(tab.id,{type:"comic:prepare",select:options.select});
    if(!ticket||ticket.canceled)return;
    if(options.work_id&&options.book_ref&&(ticket.book_context?.reader_ref||ticket.book_context?.ref)&&options.book_ref!==(ticket.book_context.reader_ref||ticket.book_context.ref))throw new Error("書籍已變更，請重新開啟插件選擇作品記憶。");
    // Supported whole-page captures use stable physical identities. An
    // arbitrary selected crop or an unattached reader retains pixel identity.
    let source=null;
    try{source=await currentStableFrame(tab,ticket,options.select);}catch(error){if(/書籍已變更/.test(error.message))throw error;}
    if(source?.rect)ticket.rect=source.rect;else source=null;
    const screenshot=await capture(tab);
    const captured=await crop(screenshot,ticket.viewport,ticket.rect);
    const watchRect=ticket.watchRect||ticket.rect;
    const compared=await crop(screenshot,ticket.viewport,watchRect);
    const alive=await notify(tab.id,{type:"comic:status",token:ticket.token,text:"正在送往本機翻譯…"});
    if(!alive?.current||epoch!==controlEpoch)return;
    job=await api("/api/jobs",{image:source?.image||await toDataUrl(captured.blob),page_identity:source?.page_identity,mode:options.mode,ocr_model:options.ocr_model||"base",translator:options.translator,model:options.translator==="gemini"?(options.model||settings.model):undefined,cleaning:options.cleaning||"standard",layout:options.layout||"contour",
      work_id:options.translator==="gemini"?(options.work_id||""):"",segment_id:options.translator==="gemini"?(options.segment_id||""):"",force_translation:!!options.force_translation,catalogue_id:options.catalogue_id||"",catalogue_epoch:options.catalogue_epoch||"",gpu_epoch:settings.gpu_epoch||0,
      gemini_cache:options.translator==="gemini"?(options.gemini_cache||"off"):"off",gemini_tier:options.translator==="gemini"?(options.gemini_tier||"standard"):"standard",memory_tokens:options.memory_tokens??12000,...(options.translator==="gemini"?globalThis.ComicGeminiConnection?.payload(settings)||{}:{})});
    const deadline=Date.now()+1200000;
    while(!["done","error","canceled"].includes(job.state)){
      if(epoch!==controlEpoch)return;
      if(Date.now()>deadline)throw new Error("處理時間較長，請縮小擷取範圍後重試");
      const response=await notify(tab.id,{type:"comic:status",token:ticket.token,text:job.message});
      if(!response?.current){await api(`/api/jobs/${job.id}/cancel`,{});return;}
      await new Promise(resolve=>setTimeout(resolve,800));job=await api(`/api/jobs/${job.id}`);
    }
    if(epoch!==controlEpoch)return;
    if(job.state!=="done")throw new Error(job.error||job.message);
    const ready=await notify(tab.id,{type:"comic:check",token:ticket.token});
    if(!ready?.current)return;
    const now=await crop(await capture(tab),ticket.viewport,watchRect);
    if(meanDifference(compared.fingerprint,now.fingerprint)>1.5)throw new Error("漫畫畫面已改變，請在目前頁面重新翻譯");
    const result=await fetch(SERVER+job.image_url);if(!result.ok)throw new Error("譯圖已清除，請重新翻譯");
    const image=await toDataUrl(await result.blob());if(epoch!==controlEpoch)return;
    await notify(tab.id,{type:"comic:result",token:ticket.token,rect:ticket.rect,image,metrics:job.metrics,
      watch:{viewport:ticket.viewport,rect:watchRect,fingerprint:Array.from(compared.fingerprint)}});
  }catch(error){
    if(epoch!==controlEpoch)return;
    if(job?.id)api(`/api/jobs/${job.id}/cancel`,{}).catch(()=>{});
    const text=error instanceof TypeError?"無法連接本機服務，請先啟動 start-reader.ps1":error.message;
    if(ticket)await notify(tab.id,{type:"comic:error",token:ticket.token,text});
    else await chrome.storage.session.set({lastError:text});
  }finally{if(epoch!==controlEpoch&&job?.id)api(`/api/jobs/${job.id}/cancel`,{}).catch(()=>{});running.delete(tab.id);}
}
async function verifyPage(tab,message){
  if(checking.has(tab.id)||running.has(tab.id))return;
  const watch=message.watch;
  if(!watch?.viewport||!watch.rect||!Array.isArray(watch.fingerprint)||watch.fingerprint.length!==64*64*4)return;
  checking.add(tab.id);
  try{
    if(!await active(tab))return;
    const ready=await notify(tab.id,{type:"comic:check",token:message.token});if(!ready?.current)return;
    const now=await crop(await capture(tab),watch.viewport,watch.rect);
    if(meanDifference(watch.fingerprint,now.fingerprint)>1.5)await notify(tab.id,{type:"comic:invalidate",token:message.token});
  }catch{} // A failed/inactive capture must not dismiss an unchanged translation.
  finally{await notify(tab.id,{type:"comic:restore",token:message.token});checking.delete(tab.id);}
}
function handleAheadPage(state,tab,message,sendResponse){
  if(state.error){sendResponse({stopped:true,text:state.error});return;}
  if(!globalThis.ComicReaders.matches(tab.url,state.options.book_ref)){stopAhead(tab.id);sendResponse({stopped:true,text:"書籍或章節已變更，自動翻譯已停止。"});return;}
  if(!state.queue){if(state.payload)retargetAhead(tab,state,message);sendResponse({text:state.notice||"正在準備自動翻譯…"});return;}
  state.queue.view(message).then(result=>{if(result.needs_capture)retargetAhead(tab,state,message);sendResponse(result);}).catch(()=>sendResponse({stopped:true,text:"自動翻譯已失效，請重新開始。"}));
}
chrome.tabs.onRemoved?.addListener(tabId=>{stopAhead(tabId).catch(()=>{});});
chrome.tabs.onUpdated?.addListener((tabId,change)=>{if(change.status==="loading")stopAhead(tabId).catch(()=>{});});
chrome.runtime.onMessage.addListener((message,sender,sendResponse)=>{
  if(sender.id!==chrome.runtime.id)return;
  if(/^comic:bw:(page|stop)$/.test(message.type||""))message={...message,type:message.type.replace("comic:bw:","comic:reader:")};
  if(message.type==="comic:ensure-service"){
    const allowed=["popup.html","service.html"].some(name=>sender.url===`chrome-extension://${chrome.runtime.id}/${name}`||sender.url?.startsWith(`chrome-extension://${chrome.runtime.id}/${name}?`));
    if(!allowed){sendResponse({error:"不允許從此網頁啟動本機服務。"});return;}
    ensureLocalService().then(()=>sendResponse({ok:true})).catch(error=>sendResponse({error:error.message}));return true;
  }
  if(message.type==="comic:release-gpu"&&!sender.tab){
    releaseGpu().then(sendResponse).catch(error=>sendResponse({error:error.message||"無法釋放 GPU。"}));return true;
  }
  if(message.type==="comic:reader:diagnostic"&&sender.tab){
    const state=ahead.get(sender.tab.id);
    if(state?.id===message.run_id&&globalThis.ComicReaders.matches(sender.tab.url,state.options.book_ref)){
      api("/api/reader-diagnostics",{adapter:"reader-v1",platform:state.adapter,phase:message.phase==="display"?"display":"observe",run_id:state.id,
        current_position:Number.isInteger(message.position)?message.position:undefined,job_id:typeof message.job_id==="string"?message.job_id.slice(0,80):undefined,error:String(message.error||"").slice(0,300)}).catch(()=>{});
    }
    sendResponse({ok:true});return;
  }
  if(message.type==="comic:reader:page"&&sender.tab){
    const state=ahead.get(sender.tab.id);
    if(!state){restoreAhead(sender.tab).then(restored=>{if(restored?.id===message.run_id)handleAheadPage(restored,sender.tab,message,sendResponse);else sendResponse({stopped:true,text:"自動翻譯已停止，請重新開始。"});});return true;}
    if(state.id!==message.run_id){sendResponse({stopped:true,text:"自動翻譯已停止，請重新開始。"});return;}
    handleAheadPage(state,sender.tab,message,sendResponse);return true;
  }
  if(message.type==="comic:reader:stop"&&sender.tab){stopAhead(sender.tab.id).finally(()=>sendResponse({ok:true}));return true;}
  if(["comic:bw:network-watch","comic:bw:network-diagnose"].includes(message.type)){
    chrome.tabs.query({active:true,currentWindow:true}).then(async([tab])=>{
      if(!tab?.id||!tab.url?.startsWith("https://viewer.bookwalker.jp/"))throw new Error("請先開啟 BookWalker 閱讀頁。");
      const id=new URL(tab.url).searchParams.get("cid"),book="bookwalker:"+id;
      if(message.book_ref!==book||!bookWalkerNetwork)throw new Error("書籍已變更，請重新開啟插件。");
      const value=message.type==="comic:bw:network-watch"?await bookWalkerNetwork.watch(tab.id,book):await bookWalkerNetwork.diagnose(tab.id,book);
      sendResponse(value);
    }).catch(error=>sendResponse({error:error.message}));return true;
  }
  if(message.type==="comic:verify"&&sender.tab){verifyPage(sender.tab,message).finally(()=>sendResponse({ok:true}));return true;}
  if(message.type==="comic:start"){
    if(releaseTask){sendResponse({error:"正在釋放 GPU，完成後再開始翻譯。"});return;}
    const epoch=controlEpoch;
    if(!["standard","tiled","paddle"].includes(message.mode)||!["gemini","google-web"].includes(message.translator))return;
    if(message.ocr_model!==undefined&&!["nar","base","paddle"].includes(message.ocr_model)){sendResponse({error:"請選擇有效的 OCR 模型"});return;}
    if(message.translator==="gemini"&&message.model!==undefined&&(typeof message.model!=="string"||!/^[a-zA-Z0-9_.-]{1,100}$/.test(message.model))){sendResponse({error:"請選擇有效的 Gemini 模型"});return;}
    if(!["standard","comic-original","ctd-lama","ctd-aot","hybrid-lama","hybrid-aot"].includes(message.cleaning||"standard")||!["contour","rectangle","comic-original","sam"].includes(message.layout||"contour")){sendResponse({error:"請選擇有效的消字與排版方案"});return;}
    chrome.tabs.query({active:true,currentWindow:true}).then(async([tab])=>{
      if(!tab?.id||!/^https?:\/\//.test(tab.url||"")){sendResponse({error:"請在一般網頁或 BookWalker 閱讀頁使用"});return;}
      if(running.has(tab.id)){sendResponse({error:"這一頁仍在翻譯中"});return;}
      if(message.prefetch_count!==undefined&&(!Number.isInteger(message.prefetch_count)||message.prefetch_count<0||message.prefetch_count>5)){sendResponse({error:"預翻譯範圍須為 0–5 個畫面"});return;}
      await ensureLocalService();
      await stopAhead(tab.id);
      const gpu=await api("/api/gpu");
      if(epoch!==controlEpoch){sendResponse({error:"翻譯已因釋放 GPU 而停止。"});return;}
      await api("/api/gpu/resume",{gpu_epoch:gpu.gpu_epoch||0});
      if(epoch!==controlEpoch){sendResponse({error:"翻譯已因釋放 GPU 而停止。"});return;}
      if(message.prefetch_count>0&&!message.select){
        if(!globalThis.ComicReaders.match(tab.url)||!globalThis.ComicReaders.matches(tab.url,message.book_ref)){sendResponse({error:"請在支援的漫畫閱讀頁啟用預翻譯。"});return;}
        sendResponse({started:true});startAhead(tab,message);return;
      }
      running.add(tab.id);chrome.storage.session.remove("lastError");
      sendResponse({started:true});run(tab,message);
    }).catch(error=>sendResponse({error:error.message||"無法讀取目前分頁"}));return true;
  }
  if(message.type==="comic:clear"){
    chrome.tabs.query({active:true,currentWindow:true}).then(async([tab])=>{if(tab?.id){await stopAhead(tab.id);await notify(tab.id,{type:"comic:clear"});}sendResponse({ok:true});});return true;
  }
});

"use strict";
const $=id=>document.getElementById(id);
let available=false,hasKey=false,checkingGemini=false,contextReady=false,releasingGpu=false,canReleaseGpu=false;
let bookContext={title:"",author:"",ref:""},activeTab=null;
let catalogue={};
let connectionSettings={},changingConnection=false;
const cleaningModes=["standard","comic-original","ctd-lama","ctd-aot","hybrid-lama","hybrid-aot"],layouts=["contour","rectangle","comic-original","sam"];
const ocrModels=["nar","base","paddle"];
const modelPicker=ComicGeminiModels.create({select:$("gemini-model"),input:$("custom-model"),field:$("custom-model-field"),onChange:save});
async function workApi(path,data){const response=await fetch("http://127.0.0.1:8765"+path,data===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});const value=await response.json();if(!response.ok)throw new Error(value.error||"作品記憶連線失敗");return value;}
const workPicker=ComicWorkMemory.create({container:$("work-memory"),api:workApi,model:()=>modelPicker.value(),scope:()=>bookContext.ref,compact:true,onChange:async value=>{
  if(!value.loading){const binding={work_id:value.work_id,segment_id:value.segment_id};
    if(bookContext.ref){const saved=await chrome.storage.local.get("comicWorkBindings");const bindings=saved.comicWorkBindings||{};bindings[bookContext.ref]=binding;await chrome.storage.local.set({comicWorkBindings:bindings});}
    else if(activeTab?.id)await chrome.storage.session.set({["comicWorkTab-"+activeTab.id]:binding});
    if(contextReady){try{await registerBook(value.work_id);}catch(error){update();$("status").textContent=error.message;return;}}}
  update();
}});
async function registerBook(work_id="",prefer_existing=false){
  if(!bookContext.ref)return {};
  catalogue=await workApi("/api/library/books",{...bookContext,work_id,prefer_existing});
  $("manage-library").href="service.html?target=library&work="+encodeURIComponent(catalogue.catalogue_id);
  return catalogue;
}
function update(){
  const gemini=$("translator").value==="gemini";
  const ready=available&&contextReady&&!releasingGpu&&!changingConnection&&(!gemini||hasKey&&modelPicker.valid()&&!workPicker.value().loading);
  $("release-gpu").disabled=!available||!canReleaseGpu||releasingGpu;
  $("prefetch-field").hidden=!ComicReaders.match(activeTab?.url||"");
  const automatic=!$("prefetch-field").hidden&&Number($("prefetch-count").value)>0;
  $("capture").textContent=automatic?"開始自動翻譯":"翻譯目前畫面";
  $("reading-note").textContent=automatic?"翻頁後自動補足後續譯圖；可在閱讀頁按「停止自動翻譯」。":"翻頁、捲動後，請重新按一次翻譯。";
  $("gemini-model-controls").hidden=!gemini;
  workPicker.setEnabled(gemini);
  $("check-gemini").disabled=!available||!hasKey||!modelPicker.valid()||checkingGemini;
  $("capture").disabled=$("select").disabled=!ready;
  $("note").textContent=gemini?"本頁圖片與辨識文字會送至 Google Gemini API。":"只將辨識文字送至 Google 測試引擎。";
  if(available)$("status").textContent=gemini&&!hasKey?ComicGeminiConnection.problem(connectionSettings):gemini&&!modelPicker.valid()?"請輸入有效的 Gemini 模型 ID。":"";
}
$("gemini-backend").addEventListener("change",async()=>{
  let failure="";
  changingConnection=true;$("gemini-backend").disabled=true;update();
  try{
    connectionSettings=await workApi("/api/settings",{gemini_backend:$("gemini-backend").value});
    hasKey=ComicGeminiConnection.ready(connectionSettings);
    $("gemini-connection-state").textContent=ComicGeminiConnection.describe(connectionSettings);
    $("priority-note").textContent=ComicGeminiConnection.priorityNote(connectionSettings);
  }catch(error){failure=error.message;}
  finally{changingConnection=false;$("gemini-backend").disabled=false;$("gemini-backend").value=connectionSettings.gemini_backend||"developer";update();if(failure)$("status").textContent=failure;}
});
async function save(){const preferences={mode:$("mode").value,ocr_model:$("ocr-model").value,translator:$("translator").value,cleaning:$("cleaning").value,layout:$("layout").value,prefetch_count:Number($("prefetch-count").value),prefetch_parallelism:Number($("prefetch-parallelism").value),gemini_cache:$("gemini-cache").value,gemini_tier:$("gemini-tier").value,memory_tokens:Number($("memory-tokens").value)};if(modelPicker.valid())preferences.model=modelPicker.value();await chrome.storage.local.set(preferences);update();}
async function start(select){
  $("capture").disabled=$("select").disabled=true;
  try{
    const prefetch_count=!select&&!$("prefetch-field").hidden?Number($("prefetch-count").value):0;
    if($("translator").value==="gemini"&&!modelPicker.valid())throw new Error("請輸入有效的 Gemini 模型 ID。");
    const memory=$("translator").value==="gemini"?workPicker.value():{};
    const selectedCatalogue=await registerBook(memory.work_id||"");
    const result=await chrome.runtime.sendMessage({type:"comic:start",select,mode:$("mode").value,ocr_model:$("ocr-model").value,translator:$("translator").value,model:$("translator").value==="gemini"?modelPicker.value():undefined,cleaning:$("cleaning").value,layout:$("layout").value,
      work_id:memory.work_id||"",segment_id:memory.segment_id||"",force_translation:!!memory.force_translation,book_ref:bookContext.reader_ref||bookContext.ref,prefetch_count,prefetch_parallelism:Number($("prefetch-parallelism").value),
      gemini_cache:$("gemini-cache").value,gemini_tier:$("gemini-tier").value,memory_tokens:Number($("memory-tokens").value),...selectedCatalogue});
    if(result?.error)throw new Error(result.error);
    if(result?.started)window.close();else throw new Error("無法啟動翻譯，請重新開啟擴充套件");
  }catch(error){$("status").textContent=error.message;update();$("status").textContent=error.message;}
}
$("mode").addEventListener("change",save);$("translator").addEventListener("change",save);
$("prefetch-count").addEventListener("change",save);
$("prefetch-parallelism").addEventListener("change",save);
$("gemini-cache").addEventListener("change",save);$("gemini-tier").addEventListener("change",save);
$("memory-tokens").addEventListener("change",save);
$("clear-gemini-cache").addEventListener("click",async()=>{
  $("clear-gemini-cache").disabled=true;
  try{$("gemini-cloud-status").textContent=(await workApi("/api/gemini/cache/clear",{})).message;}
  catch(error){$("gemini-cloud-status").textContent=error.message;}
  finally{$("clear-gemini-cache").disabled=false;}
});
$("ocr-model").addEventListener("change",save);
$("check-gemini").addEventListener("click",async event=>{
  event.preventDefault();const model=modelPicker.value();let message="";checkingGemini=true;update();$("status").textContent="正在測試 Gemini 連線…";
  try{
    const response=await fetch("http://127.0.0.1:8765/api/gemini/check",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model})});
    const value=await response.json();if(!response.ok)throw new Error(value.error||"連線測試失敗");message=value.message;
  }catch(error){message=error instanceof TypeError?"無法連接本機服務。":error.message;}
  finally{checkingGemini=false;update();if(modelPicker.value()===model&&$("translator").value==="gemini")$("status").textContent=message;}
});
$("cleaning").addEventListener("change",save);$("layout").addEventListener("change",save);
$("comic-original").addEventListener("click",async event=>{event.preventDefault();$("cleaning").value=$("layout").value="comic-original";await save();$("processing-note").hidden=false;$("processing-note").textContent="已套用上游 AOT 消字＋Qt 排版。";});
$("capture").addEventListener("click",()=>start(false));$("select").addEventListener("click",()=>start(true));
$("retry-service").addEventListener("click",()=>location.reload());
$("clear").addEventListener("click",async()=>{await chrome.runtime.sendMessage({type:"comic:clear"});window.close();});
async function watchGpuRelease(value){
  while(value.state==="releasing"){
    $("status").textContent=value.message;
    await new Promise(resolve=>setTimeout(resolve,750));
    value=await workApi("/api/gpu");
  }
  if(value.state!=="released")throw new Error(value.message||"GPU 釋放未完成，請重試。");
  return value.message;
}
async function releaseGpu(existing){
  releasingGpu=true;update();$("status").textContent="正在停止全部翻譯並釋放 GPU…";
  let message;
  try{
    let value=existing;
    if(!value){
      try{value=await chrome.runtime.sendMessage({type:"comic:release-gpu"});}
      catch{value=await workApi("/api/gpu/release",{});}
    }
    if(value?.error)throw new Error(value.error);
    // An already-running MV3 worker may still have the previous code and
    // acknowledge an unknown message with {}. Pause the backend directly in
    // that case; its epoch guard also rejects late uploads from old queues.
    if(!["releasing","released","error"].includes(value?.state))value=await workApi("/api/gpu/release",{});
    message=await watchGpuRelease(value||{});
  }catch(error){message=error.message||"無法釋放 GPU，請確認本機服務已更新。";}
  finally{releasingGpu=false;update();$("status").textContent=message;}
}
$("release-gpu").addEventListener("click",()=>releaseGpu());
$("diagnose-reader").addEventListener("click",async()=>{
  $("diagnose-reader").disabled=true;
  try{
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    if(!tab?.id)throw new Error("請先開啟 BookWalker 閱讀頁。");
    await chrome.scripting.executeScript({target:{tabId:tab.id},files:["readers/registry.js","readers/bookwalker.js","readers/pixiv.js","book-context.js","bookwalker-reader.js"]});
    const results=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>globalThis.ComicBookWalker.diagnose()});
    const report=results[0]?.result;
    if(!report?.supported)throw new Error(report?.reason||"無法讀取閱讀器結構。");
    // Reopening the popup loads this diagnostic without restarting the existing
    // background queue, so completed translations remain available for comparison.
    try{
      const previous=await workApi("/api/reader-diagnostics");
      if(previous.phase==="prefetch"&&previous.frames?.some(f=>f.state==="done")){
        const captured=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>globalThis.ComicBookWalker.frame()});
        const frame=captured[0]?.result;
        if(frame){
          report.current_frame={position:frame.position,width:frame.width,height:frame.height,rect:frame.rect,sampling:2};
          report.prefetch_comparison={run_id:previous.run_id,positions:previous.frames.map(f=>f.position)};
          const expected=previous.frames.find(f=>f.position===frame.position)||previous.frames.find(f=>f.position>previous.frames[0].position);
          if(expected?.job_id){const response=await fetch("http://127.0.0.1:8765/api/jobs/"+expected.job_id+"/image");if(response.ok){
            const blob=await response.blob(),data=await new Promise(resolve=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.readAsDataURL(blob);});
            await workApi("/test/bookwalker-probe",{current:frame.image,candidate:data,source:frame.image});
            report.prefetch_comparison.compared_position=expected.position;
          }}
        }
      }
    }catch{}
    const network=await chrome.runtime.sendMessage({type:"comic:bw:network-diagnose",book_ref:report.book_ref});
    report.network=network||{watching:false,reason:"no-background-response"};
    try{const worker=await chrome.scripting.executeScript({target:{tabId:tab.id},world:"MAIN",func:()=>({worker:globalThis.__comicBookWalkerWorkerProbe?.snapshot()||null,native:globalThis.ComicBookWalkerNative?.status()||null})});if(worker[0]?.result){if(worker[0].result.worker)report.worker=worker[0].result.worker;if(worker[0].result.native)report.native=worker[0].result.native;}}catch{}
    // Keep diagnostics bounded even on long reading sessions.
    while(JSON.stringify(report).length>22500&&report.resources?.length)report.resources.shift();
    while(JSON.stringify(report).length>22500&&report.worker?.messages?.length>1)report.worker.messages.shift();
    while(JSON.stringify(report).length>22500&&report.worker?.rendering?.canvases?.length>1)report.worker.rendering.canvases.shift();
    while(JSON.stringify(report).length>22500&&report.network?.resources?.length)report.network.resources.shift();
    await workApi("/api/reader-diagnostics",report);
    $("status").textContent=`已回報 ${report.nodes.length} 個畫面節點，其中 ${report.nodes.filter(n=>n.readable).length} 個可直接讀取。診斷僅暫存在本機服務，不含帳密或圖片。`;
  }catch(error){$("status").textContent=error.message||"無法檢查閱讀器。";}
  finally{$("diagnose-reader").disabled=false;}
});
$("enable-reader-network").addEventListener("click",async()=>{
  try{
    // Chrome requires this optional permission request directly in a user gesture.
    const granted=await chrome.permissions.request({permissions:["webRequest"],origins:["https://bw-bv-epubs.bookwalker.jp/*"]});
    if(!granted)throw new Error("未開啟圖片請求診斷，可繼續使用原本的畫面翻譯。");
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    const current=ComicBookContext.fromMetadata(tab?.title,tab?.url);
    const result=await chrome.runtime.sendMessage({type:"comic:bw:network-watch",book_ref:current.ref});
    if(result?.error)throw new Error(result.error);
    $("status").textContent="已開始觀察此書的圖片請求。請重新整理 BookWalker 閱讀頁，等漫畫載入，再按「檢查 BookWalker 預讀」。簽章網址只留在插件記憶體，不回報至本機服務。";
  }catch(error){$("status").textContent=error.message;}
});
$("enable-reader-worker").addEventListener("click",async()=>{
  try{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    if(!tab?.id||!tab.url?.startsWith("https://viewer.bookwalker.jp/"))throw new Error("請在 BookWalker 閱讀頁使用。");
    await chrome.scripting.executeScript({target:{tabId:tab.id},world:"MAIN",files:["bookwalker-native.js","bookwalker-worker-probe.js"]});
    $("status").textContent="已開始觀察閱讀器自身的圖片處理，三分鐘後自動停止。請正常翻一個畫面，等載入後按「檢查 BookWalker 預讀」；啟用後請勿重新整理。只回報訊息欄位、繪圖次數與尺寸，不回報金鑰或圖片內容。";
  }catch(error){$("status").textContent=error.message;}
});
(async()=>{
  const saved=await chrome.storage.local.get(["mode","ocr_model","translator","cleaning","layout","model","prefetch_count","prefetch_parallelism","gemini_cache","gemini_tier","memory_tokens"]);
  if([1,2,3,4,5].includes(saved.prefetch_parallelism))$("prefetch-parallelism").value=String(saved.prefetch_parallelism);
  $("gemini-cache").value=saved.gemini_cache==="explicit"?"explicit":"off";
  $("gemini-tier").value=saved.gemini_tier==="priority"?"priority":"standard";
  $("memory-tokens").value=[6000,12000,24000,48000].includes(saved.memory_tokens)?String(saved.memory_tokens):"12000";
  $("ocr-model").value=ocrModels.includes(saved.ocr_model)?saved.ocr_model:"base";
  if(Number.isInteger(saved.prefetch_count)&&saved.prefetch_count>=0&&saved.prefetch_count<=5)$("prefetch-count").value=String(saved.prefetch_count);
  if(["standard","tiled","paddle"].includes(saved.mode))$("mode").value=saved.mode;
  if(["gemini","google-web"].includes(saved.translator))$("translator").value=saved.translator;
  if(cleaningModes.includes(saved.cleaning))$("cleaning").value=saved.cleaning;
  if(layouts.includes(saved.layout))$("layout").value=saved.layout;
  try{
    $("connection").textContent="正在啟動本機服務…";
    const startup=await chrome.runtime.sendMessage({type:"comic:ensure-service"});
    if(startup?.ok!==true)throw new Error(startup?.error||"本機啟動橋接尚未就緒，請重新載入插件。");
    const response=await fetch("http://127.0.0.1:8765/api/settings");if(!response.ok)throw new Error();
    const settings=await response.json();available=!!settings.cleaning_modes&&!!settings.layout_modes&&!!settings.ocr_models&&Array.isArray(settings.gemini_models);connectionSettings=settings;hasKey=ComicGeminiConnection.ready(settings);
    $("memory-tokens").disabled=!Array.isArray(settings.memory_budget?.choices);
    for(const option of $("memory-tokens").options)option.disabled=!(settings.memory_budget?.choices||[12000]).includes(Number(option.value));
    if($("memory-tokens").selectedOptions[0]?.disabled)$("memory-tokens").value="12000";
    $("gemini-backend").value=settings.gemini_backend||"developer";$("gemini-backend").disabled=!settings.gemini_backend;
    $("gemini-connection-state").textContent=ComicGeminiConnection.describe(settings);
    $("priority-note").textContent=ComicGeminiConnection.priorityNote(settings);
    canReleaseGpu=settings.gpu_release===true;releasingGpu=settings.gpu_state?.state==="releasing";
    $("prefetch-parallelism").disabled=(settings.prefetch_parallelism_max||1)<2;
    if($("prefetch-parallelism").disabled)$("prefetch-parallelism").value="1";
    for(const [id,maximum] of [["prefetch-count",settings.prefetch_count_max||3],["prefetch-parallelism",settings.prefetch_parallelism_max||1]]){
      for(const option of $(id).options)option.disabled=Number(option.value)>maximum;
      if($(id).selectedOptions[0]?.disabled)$(id).value=String(maximum);
    }
    if(!available){$("connection").textContent="服務需要更新";$("status").textContent="請重新啟動本機服務後使用新版處理選項。";return;}
    modelPicker.load(settings.gemini_models,ComicGeminiModels.valid(saved.model)?saved.model:settings.model);
    for(const id of ["gemini-cache","gemini-tier","clear-gemini-cache"])$(id).disabled=!settings.gemini_acceleration;
    $("gemini-cloud-status").textContent=settings.gemini_acceleration?`目前雲端快取 ${settings.gemini_cache?.active||0} 筆`+(settings.gemini_cache?.last_translation?" · 上次 API："+ComicGeminiStatus.format(settings.gemini_cache.last_translation):""):"請更新本機服務以啟用加速選項。";
    for(const option of $("ocr-model").options){const capability=settings.capabilities?.ocr?.[option.value];option.disabled=capability?.available!==true;option.title=capability?.reason||"";}
    if($("ocr-model").selectedOptions[0]?.disabled)$("ocr-model").value="base";
    for(const group of ["cleaning","layout"]){for(const option of $(group).options){const capability=settings.capabilities?.[group]?.[option.value];option.disabled=capability?.available!==true;if(option.disabled)option.textContent+="（未備妥）";}if($(group).selectedOptions[0]?.disabled)$(group).value=group==="cleaning"?"standard":"contour";}
    $("comic-original").disabled=!["cleaning","layout"].every(group=>settings.capabilities?.[group]?.["comic-original"]?.available===true);
    $("connection").textContent="● 本機已連線";update();
    let binding={};
    try{
      [activeTab]=await chrome.tabs.query({active:true,currentWindow:true});
      bookContext=ComicBookContext.fromMetadata(activeTab?.title,activeTab?.url);
      if(activeTab?.id&&/^https?:/.test(activeTab.url||"")){
        try{await chrome.scripting.executeScript({target:{tabId:activeTab.id},files:["readers/registry.js","readers/bookwalker.js","readers/pixiv.js","book-context.js"]});const result=await chrome.scripting.executeScript({target:{tabId:activeTab.id},func:()=>globalThis.ComicBookContext.read(document,location.href)});if(result[0]?.result)bookContext=result[0].result;}catch{}
      }
      if(bookContext.ref){const saved=await chrome.storage.local.get("comicWorkBindings");binding=saved.comicWorkBindings?.[bookContext.ref]||{};}
      else if(activeTab?.id){const saved=await chrome.storage.session.get("comicWorkTab-"+activeTab.id);binding=saved["comicWorkTab-"+activeTab.id]||{};}
    }catch{}
    if(settings.library_management)await registerBook(binding.work_id||"",true);
    workPicker.suggest(bookContext);await workPicker.load(binding);contextReady=true;update();
    const last=await chrome.storage.session.get("lastError");if(last.lastError){$("status").textContent=last.lastError;await chrome.storage.session.remove("lastError");}
    if(releasingGpu)releaseGpu(settings.gpu_state);
    else if(settings.gpu_state?.state==="released")$("status").textContent=settings.gpu_state.message;
  }catch(error){$("connection").textContent="服務未啟動";$("retry-service").hidden=false;$("status").textContent=error.message||"本機服務未能啟動。首次使用請執行 scripts/install-reader-autostart.ps1。";}
})();

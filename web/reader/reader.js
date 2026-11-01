"use strict";
const $ = id => document.getElementById(id);
const modeNames = {standard:"RT-DETR",tiled:"RT-DETR 分塊版",paddle:"PP-OCRv6"};
const ocrModels=["nar","base","paddle"];
const cleaningModes=["standard","comic-original","ctd-lama","ctd-aot","hybrid-lama","hybrid-aot"],layouts=["contour","rectangle","comic-original","sam"];
let pages = [{url:"/demo/synthetic-01.png",name:"自製排版測試圖 · 1"},{url:"/demo/synthetic-02.png",name:"自製排版測試圖 · 2"}];
let index=0, generation=0, currentJob=null, resultUrl=null, showingResult=false, extensionPath="";
let preferredModel="";
let rememberedWork={};
let connectionSettings={},connectionDirty=false;
const modelPicker=ComicGeminiModels.create({select:$("gemini-model"),input:$("custom-model"),field:$("custom-model-field"),onChange:value=>{
  $("model-status").textContent="";
  if(ComicGeminiModels.valid(value))preferredModel=value;
  clearResult();savePreferences();status(modelPicker.valid()?"模型已變更，下次翻譯會使用所選模型。":"請輸入有效的 Gemini 模型 ID。",!modelPicker.valid());
}});
const workPicker=ComicWorkMemory.create({container:$("work-memory"),api,model:()=>modelPicker.value(),onChange:value=>{
  rememberedWork={work_id:value.work_id,segment_id:value.segment_id};clearResult();savePreferences();
}});

async function api(path, data) {
  const response=await fetch(path, data===undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
  const value=await response.json();
  if(!response.ok) throw new Error(value.error||"本機服務連線失敗");
  return value;
}
function mode(){return document.querySelector('input[name="mode"]:checked').value;}
function status(text,error=false){$("status").textContent=text;$("status").classList.toggle("error",error);}
function clearResult(){
  generation++;
  if(currentJob) api(`/api/jobs/${currentJob}/cancel`,{}).catch(()=>{});
  currentJob=null;
  if(resultUrl) URL.revokeObjectURL(resultUrl);
  resultUrl=null;showingResult=false;$("toggle").disabled=true;$("translate").disabled=false;
  $("page").src=pages[index].url;$("result-label").textContent="";$("progress").value=0;
}
function showPage(){clearResult();$("page-title").textContent=pages[index].name;$("page-count").textContent=`${index+1} / ${pages.length}`;$("previous").disabled=index===0;$("next").disabled=index===pages.length-1;status("選擇模式後，翻譯這一頁。");}
function move(delta){if(index+delta<0||index+delta>=pages.length)return;index+=delta;showPage();}
function savePreferences(){localStorage.setItem("comic-reader-preferences",JSON.stringify({mode:mode(),ocr_model:$("ocr-model").value,translator:$("translator").value,cleaning:$("cleaning").value,layout:$("layout").value,model:modelPicker.valid()?modelPicker.value():preferredModel,gemini_cache:$("gemini-cache").value,gemini_tier:$("gemini-tier").value,memory_tokens:Number($("memory-tokens").value),...rememberedWork}));}
function providerNote(){
  $("gemini-model-controls").hidden=$("translator").value!=="gemini";
  workPicker.setEnabled($("translator").value==="gemini"&&!connectionDirty);
  $("provider-note").textContent=$("translator").value==="gemini" ? "使用 Gemini 時，本頁圖片與辨識文字會送至 Google API。" : "Google 測試引擎只傳送辨識文字，不需要金鑰；翻譯品質與可用性可能不同。";
}
async function dataUrl(url){const response=await fetch(url);if(!response.ok)throw new Error("無法讀取這張圖片");const blob=await response.blob();return await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(blob);});}
async function translate(){
  if(connectionDirty&&$("translator").value==="gemini"){status("請先套用 Gemini 連線設定。",true);return;}
  if(workPicker.value().loading){status("作品資料載入中，請稍候。",true);return;}
  if($("translator").value==="gemini"&&!modelPicker.valid()){status("請輸入有效的 Gemini 模型 ID。",true);return;}
  clearResult();const ticket=generation;$("translate").disabled=true;status("正在準備圖片…");
  try{
    const gpu=await api("/api/gpu");if(ticket!==generation)return;
    await api("/api/gpu/resume",{gpu_epoch:gpu.gpu_epoch});if(ticket!==generation)return;
    const image=await dataUrl(pages[index].url);if(ticket!==generation)return;
    const memory=$("translator").value==="gemini"?workPicker.value():{};
    let job=await api("/api/jobs",{image,mode:mode(),ocr_model:$("ocr-model").value,translator:$("translator").value,...ComicGeminiConnection.payload(connectionSettings),model:$("translator").value==="gemini"?modelPicker.value():undefined,cleaning:$("cleaning").value,layout:$("layout").value,work_id:memory.work_id||"",segment_id:memory.segment_id||"",force_translation:!!memory.force_translation,gpu_epoch:gpu.gpu_epoch,
      gemini_cache:$("gemini-cache").value,gemini_tier:$("gemini-tier").value,memory_tokens:Number($("memory-tokens").value)});
    workPicker.resetForce();
    if(ticket!==generation){api(`/api/jobs/${job.id}/cancel`,{}).catch(()=>{});return;}
    currentJob=job.id;const deadline=Date.now()+1200000;
    while(!["done","error","canceled"].includes(job.state)){
      if(ticket!==generation)return;
      $("progress").value=job.progress;status(job.message);
      if(Date.now()>deadline)throw new Error("處理時間較長，請稍後重試或縮小圖片");
      await new Promise(resolve=>setTimeout(resolve,700));job=await api(`/api/jobs/${job.id}`);
    }
    if(ticket!==generation)return;
    if(job.state!=="done")throw new Error(job.error||job.message);
    const response=await fetch(job.image_url);if(!response.ok)throw new Error("譯圖已清除，請重新翻譯");
    const blob=await response.blob();if(ticket!==generation)return;
    resultUrl=URL.createObjectURL(blob);$("page").src=resultUrl;showingResult=true;
    $("toggle").disabled=false;$("toggle").textContent="顯示原文";$("progress").value=100;
    $("result-label").textContent=[modeNames[job.metrics.mode],job.metrics.ocr_name,job.metrics.cleaning_name,job.metrics.layout_name,job.metrics.model,`${job.metrics.translated_blocks} 處文字 · ${job.metrics.seconds} 秒`,ComicGeminiStatus.format(job.metrics)].filter(Boolean).join(" · ");
    const sam=job.metrics.sam;
    status("翻譯完成，可切換原文比較。"+(job.metrics.translation_cached?" 已重用本頁譯文。":"")+(job.metrics.memory?.enabled?` 作品：${job.metrics.memory.work_title}。${job.metrics.memory.error||""}`:"")+(sam?.requested?` SAM 輪廓採用 ${sam.used||0}／${sam.requested} 個氣泡，其餘使用本機排版。`:""));
    workPicker.refresh().catch(()=>{});
  }catch(error){if(ticket===generation){if(currentJob)api(`/api/jobs/${currentJob}/cancel`,{}).catch(()=>{});status(error.message||"無法連接本機服務",true);}}
  finally{if(ticket===generation){$("translate").disabled=false;currentJob=null;}}
}
$("translate").addEventListener("click",translate);
$("check-gemini").addEventListener("click",async()=>{
  if(!modelPicker.valid()){$("model-status").textContent="請輸入有效的 Gemini 模型 ID。";return;}
  const model=modelPicker.value();$("check-gemini").disabled=true;$("model-status").textContent="正在測試 Gemini 連線…";
  try{const value=await api("/api/gemini/check",{model});if(modelPicker.value()===model)$("model-status").textContent=value.message;}
  catch(error){if(modelPicker.value()===model)$("model-status").textContent=error.message;}
  finally{$("check-gemini").disabled=false;}
});
$("toggle").addEventListener("click",()=>{showingResult=!showingResult;$("page").src=showingResult?resultUrl:pages[index].url;$("toggle").textContent=showingResult?"顯示原文":"顯示譯文";});
$("previous").addEventListener("click",()=>move(-1));$("next").addEventListener("click",()=>move(1));
document.addEventListener("keydown",event=>{if(event.target.matches("input,select,textarea"))return;if(event.key==="ArrowLeft")move(1);if(event.key==="ArrowRight")move(-1);});
for(const input of document.querySelectorAll('input[name="mode"]'))input.addEventListener("change",()=>{clearResult();savePreferences();status(`已選擇 ${modeNames[mode()]}，按翻譯開始。`);});
$("translator").addEventListener("change",()=>{clearResult();providerNote();savePreferences();});
for(const id of ["cleaning","layout","ocr-model","gemini-cache","gemini-tier"])$(id).addEventListener("change",()=>{clearResult();savePreferences();status("處理方案已變更，按翻譯比較效果。");});
$("memory-tokens").addEventListener("change",()=>{clearResult();savePreferences();status("作品上下文上限已變更，下次翻譯生效。");});
$("clear-gemini-cache").addEventListener("click",async()=>{
  $("clear-gemini-cache").disabled=true;
  try{$("gemini-cloud-status").textContent=(await api("/api/gemini/cache/clear",{})).message;}
  catch(error){$("gemini-cloud-status").textContent=error.message;}
  finally{$("clear-gemini-cache").disabled=false;}
});
$("comic-original").addEventListener("click",()=>{$("cleaning").value=$("layout").value="comic-original";clearResult();savePreferences();status("已套用 Comic Translate 上游消字與 Qt 排版，按翻譯比較效果。");});
$("files").addEventListener("change",event=>{const files=Array.from(event.target.files||[]).filter(file=>/^image\/(png|jpeg|webp)$/.test(file.type));if(!files.length)return;clearResult();for(const page of pages)if(page.url.startsWith("blob:"))URL.revokeObjectURL(page.url);pages=files.map(file=>({url:URL.createObjectURL(file),name:file.name}));index=0;showPage();});
$("settings-form").addEventListener("submit",async event=>{
  event.preventDefault();const payload={gemini_backend:$("gemini-backend").value,vertex_project:$("vertex-project").value.trim(),vertex_location:$("vertex-location").value.trim()};if(modelPicker.valid())payload.model=modelPicker.value();if($("api-key").value.trim())payload.key=$("api-key").value.trim();
  try{const settings=await api("/api/settings",payload);$("api-key").value="";applySettings(settings);$("settings-status").textContent="連線設定已保存。API 金鑰只保存在本次服務記憶體；Vertex AI 使用 Google Cloud CLI 的本機登入。";}
  catch(error){$("settings-status").textContent=error.message;}
});
function showConnectionFields(){
  const vertex=$("gemini-backend").value==="vertex";
  $("vertex-settings").hidden=!vertex;$("developer-settings").hidden=vertex;
  const project=$("vertex-project").value.trim();
  $("vertex-quota-command").textContent="gcloud auth application-default set-quota-project "+(/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(project)?project:"YOUR_PROJECT_ID");
}
for(const id of ["gemini-backend","vertex-project","vertex-location"]){
  $(id).addEventListener(id==="gemini-backend"?"change":"input",()=>{
    connectionDirty=true;showConnectionFields();$("settings-status").textContent="連線已變更，請按「套用設定」後再翻譯。";
    $("check-gemini").disabled=true;workPicker.setEnabled(false);
  });
}
function applySettings(settings){
  connectionSettings=settings;connectionDirty=false;
  $("memory-tokens").disabled=!Array.isArray(settings.memory_budget?.choices);
  for(const option of $("memory-tokens").options)option.disabled=!(settings.memory_budget?.choices||[12000]).includes(Number(option.value));
  if($("memory-tokens").selectedOptions[0]?.disabled)$("memory-tokens").value="12000";
  $("priority-note").textContent=ComicGeminiConnection.priorityNote(settings);
  $("gemini-backend").value=settings.gemini_backend||"developer";
  $("vertex-project").value=settings.vertex_project||"";$("vertex-location").value=settings.vertex_location||"global";
  $("vertex-auth-state").textContent=settings.vertex_auth?.message||"更新本機服務以啟用 Vertex AI。";
  showConnectionFields();$("check-gemini").disabled=false;
  for(const id of ["gemini-cache","gemini-tier","clear-gemini-cache"])$(id).disabled=!settings.gemini_acceleration;
  $("gemini-cloud-status").textContent=settings.gemini_acceleration?`目前雲端快取 ${settings.gemini_cache?.active||0} 筆`+(settings.gemini_cache?.last_translation?" · 上次 API："+ComicGeminiStatus.format(settings.gemini_cache.last_translation):""):"請更新本機服務以啟用加速選項。";
  for(const option of $("ocr-model").options){option.disabled=settings.capabilities?.ocr?.[option.value]?.available!==true;option.title=settings.capabilities?.ocr?.[option.value]?.reason||"";}
  if($("ocr-model").selectedOptions[0]?.disabled)$("ocr-model").value="base";
  $("key-state").textContent=ComicGeminiConnection.describe(settings)+(ComicGeminiConnection.ready(settings)?" · 可測試":" · 待設定");
  modelPicker.load(settings.gemini_models,preferredModel||settings.model);extensionPath=settings.extension_path||extensionPath;
  $("settings").open=!ComicGeminiConnection.ready(settings);
  for(const group of ["cleaning","layout"]){for(const option of $(group).options){option.disabled=settings.capabilities?.[group]?.[option.value]?.available!==true;option.title=settings.capabilities?.[group]?.[option.value]?.reason||"";}if($(group).selectedOptions[0]?.disabled)$(group).value=group==="cleaning"?"standard":"contour";}
  $("comic-original").disabled=!["cleaning","layout"].every(group=>settings.capabilities?.[group]?.["comic-original"]?.available===true);
  providerNote();
}
async function copy(text,button){try{await navigator.clipboard.writeText(text);const original=button.textContent;button.textContent="已複製";setTimeout(()=>button.textContent=original,1500);}catch{status(text);}}
$("copy-extensions").addEventListener("click",event=>copy("chrome://extensions",event.target));
$("copy-path").addEventListener("click",event=>copy(extensionPath,event.target));
(async()=>{
  try{
    const saved=JSON.parse(localStorage.getItem("comic-reader-preferences")||"{}");
    if(modeNames[saved.mode])document.querySelector(`input[value="${saved.mode}"]`).checked=true;
    if(["gemini","google-web"].includes(saved.translator))$("translator").value=saved.translator;
    if(ocrModels.includes(saved.ocr_model))$("ocr-model").value=saved.ocr_model;
    if(cleaningModes.includes(saved.cleaning))$("cleaning").value=saved.cleaning;
    if(layouts.includes(saved.layout))$("layout").value=saved.layout;
    $("gemini-cache").value=saved.gemini_cache==="explicit"?"explicit":"off";
    $("gemini-tier").value=saved.gemini_tier==="priority"?"priority":"standard";
    $("memory-tokens").value=[6000,12000,24000,48000].includes(saved.memory_tokens)?String(saved.memory_tokens):"12000";
    if(ComicGeminiModels.valid(saved.model))preferredModel=saved.model;
    rememberedWork={work_id:saved.work_id||"",segment_id:saved.segment_id||""};
  }catch{}
  providerNote();showPage();
  try{
    applySettings(await api("/api/settings"));$("connection").textContent="● 本機服務已連線";
    const requested=new URLSearchParams(location.search).get("work");await workPicker.load(requested?{work_id:requested}:rememberedWork);
  }catch{$("connection").textContent="本機服務未連線";status("請先啟動 scripts/start-reader.ps1",true);}
})();

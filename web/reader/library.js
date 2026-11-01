"use strict";
const $=id=>document.getElementById(id);
const h=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const statuses={empty:"尚未建立固定資料",building:"搜尋建檔中",ready:"資料已備妥",error:"建檔未完成",interrupted:"建檔已中斷"};
let storyEditor=null;
let works=[],runtime={},work=null,tab="profile",dirty=false,ticket=0,pageOffset=0,segment="",page=null,busy=false;
const date=value=>value?new Date(value*1000).toLocaleString("zh-TW",{hour12:false}):"—";
const size=value=>value>1048576?(value/1048576).toFixed(1)+" MB":Math.round(value/1024)+" KB";
function memoryPageLabel(page){
  const source=page.source_page;if(!source)return "記憶頁 "+page.seq;
  const reader=source.book_ref.startsWith("pixiv:episode:")?"pixivコミック · "+(source.chapter_title||"章節 ID "+source.book_ref.split(":").at(-1)):"BookWalker";
  return reader+" · 原始頁序 "+(source.index+1);
}
function message(value,error=false){$("message").hidden=!value;$("message").textContent=value;$("message").classList.toggle("error",error);}
async function api(path,data){let response;try{response=await fetch(path,data===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});}catch{throw new Error("無法連接本機服務，請確認 scripts/start-reader.ps1 正在運作。");}const value=await response.json();if(!response.ok)throw new Error(value.error||"讀取資料失敗");return value;}
async function perform(action){if(busy)return;busy=true;try{await action();}catch(error){message(error.message,true);}finally{busy=false;}}
function mayLeave(){return !dirty||confirm("有尚未儲存的修改，要放棄修改並繼續嗎？");}
async function download(path,name){const value=await api(path),url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:"application/json"})),a=document.createElement("a");a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);}
function platformBadges(value){
  const ids=ComicLibraryPlatforms.ids(value);
  return `<span class="platform-badges">${(ids.length?ids:["unassigned"]).map(id=>`<span class="platform-badge platform-${id}">${h(ComicLibraryPlatforms.labels[id])}</span>`).join("")}</span>`;
}
function renderBooks(){
  const query=$("search").value.trim().toLocaleLowerCase(),platform=$("platform-filter").value,shown=works.filter(w=>ComicLibraryPlatforms.matches(w,platform)&&[w.title,w.author,...w.books.map(b=>b.title)].join(" ").toLocaleLowerCase().includes(query));
  $("library-count").textContent=`顯示 ${shown.length} / ${works.length} 部作品 · ${shown.reduce((n,w)=>n+w.stats.pages,0)} 頁記憶`;
  $("books").innerHTML=shown.map(w=>`<button class="book ${work?.id===w.id?"active":""}" data-work="${h(w.id)}" ${work?.id===w.id?'aria-current="true"':""}><strong>${h(w.title)}</strong>${platformBadges(w)}<small>${h(w.author||"作者尚未填寫")}<br>${w.stats.pages} 頁記憶 · 固定資料 ${w.revision?"v"+w.revision:"待建立"}</small></button>`).join("")||'<p class="no-data">沒有符合的作品</p>';
}
async function refreshList(){const result=await api("/api/library");works=result.works;runtime=result.runtime;renderBooks();}
let researchPolling=false;
async function pollResearch(){
  if(researchPolling||busy||document.hidden||!works.some(w=>w.status==="building"))return;
  researchPolling=true;
  const id=work?.id,serial=ticket,previous=work?.status;
  try{
    await refreshList();
    if(busy||serial!==ticket||!id||work?.id!==id)return;
    const latest=works.find(w=>w.id===id);
    if(previous!=="building"||!latest||latest.status==="building")return;
    if(!dirty)await openWork(id,{check:false});
    else{
      // Preserve the user's draft and its original optimistic-lock version.
      work.status=latest.status;work.error=latest.error;
      const badge=document.querySelector(".work-heading .status");if(badge)badge.textContent=statuses[latest.status]||latest.status;
    }
    const result=latest.error|| (latest.status==="ready"?"作品資料建檔完成。":"作品建檔已停止。");
    message(result+(dirty?" 有未儲存修改，請先複製草稿，再重新載入最新資料。":""),latest.status!=="ready");
  }catch(error){message("無法更新建檔狀態："+error.message,true);}
  finally{researchPolling=false;}
}
async function openWork(id,{check=true}={}){
  if(check&&!mayLeave())return;
  const serial=++ticket;$("content").setAttribute("aria-busy","true");
  try{const result=await api("/api/library/"+encodeURIComponent(id));if(serial!==ticket)return;work=result;dirty=false;page=null;segment=result.segments.some(s=>s.id===segment)?segment:result.current_segment;pageOffset=0;history.replaceState(null,"",`/library?work=${encodeURIComponent(id)}`);renderBooks();await renderWork();}
  finally{if(serial===ticket)$("content").setAttribute("aria-busy","false");}
}
function empty(){storyEditor?.dispose();storyEditor=null;work=null;$("content").innerHTML='<div class="empty"><span class="empty-icon" aria-hidden="true">冊</span><h2>從第一部作品開始</h2><p>在插件開啟 BookWalker 或 pixivコミック 時，會按平台與閱讀器書名建立分類。不同平台的同名作品分開保存。<br>也可以先手動新增作品，整理人物、用語與翻譯指示。</p><button class="primary" data-action="create">新增作品</button></div>';$("content").setAttribute("aria-busy","false");history.replaceState(null,"","/library");renderBooks();}
async function renderWork(){
  storyEditor?.dispose();storyEditor=null;
  if(!work)return empty();
  $("content").innerHTML=`<div class="work-heading"><div><span class="eyebrow">WORKSPACE / 作品資料</span><h2>${h(work.title)}</h2>${platformBadges(work)}<p class="muted">${h([work.author,work.volume].filter(Boolean).join(" · ")||"書名與作者可在下方編輯")}</p></div><span class="status">${h(statuses[work.status]||work.status)}</span></div><dl class="stats"><div><dt>頁閱讀記憶</dt><dd>${work.stats.pages}</dd></div><div><dt>份文字譯文快取</dt><dd>${work.stats.translations}</dd></div><div><dt>版固定資料</dt><dd>${work.stats.profiles}</dd></div></dl><nav class="tabs" role="tablist" aria-label="作品資料分類">${[["profile","固定資料"],["memory","動態記憶"],["cache","翻譯與暫存"],["data","資料管理"]].map(([id,label])=>`<button role="tab" id="tab-${id}" aria-controls="tab-panel" aria-selected="${tab===id}" data-tab="${id}">${label}</button>`).join("")}</nav><div id="tab-panel" role="tabpanel" aria-labelledby="tab-${tab}"></div>`;
  if(tab==="profile")renderProfile();else if(tab==="memory")await renderMemory();else if(tab==="cache")renderCache();else renderData();
}
function termRow(term={}){return `<div class="term"><label>日文<input data-term="ja" value="${h(term.ja)}" maxlength="150"></label><label>繁體中文<input data-term="zh_tw" value="${h(term.zh_tw)}" maxlength="150"></label><label>備註<input data-term="notes" value="${h(term.notes)}" maxlength="1600"></label><button type="button" data-action="remove-term" aria-label="移除此人名或用語">×</button></div>`;}
function profileMetadata(){return {title:$("title").value,author:$("author").value,volume:$("volume").value};}
function renderProfile(){
  storyEditor?.dispose();
  const p=work.profile||{},glossary=p.glossary||p.characters||[],sources=work.research?.sources||[];
  $("tab-panel").innerHTML=`<section class="panel"><div class="panel-heading"><div><h3>讓每一頁，都認得同一部作品。</h3><p class="muted">這些資料會隨每頁翻譯一起提供給 Gemini。</p></div></div><form id="profile-form"><div class="field-grid"><label>分類書名<input id="title" value="${h(work.title)}" maxlength="300" required></label><label>作者<input id="author" value="${h(work.author)}" maxlength="200"></label><label>卷數<input id="volume" value="${h(work.volume)}" maxlength="200"></label></div><label>故事概要<textarea id="synopsis" rows="3">${h(typeof p.synopsis==="string"?p.synopsis:p.synopsis?.text||"")}</textarea></label>${ComicRecentStory.markup(work,h)}<label>額外翻譯指示<textarea id="instructions" rows="3" placeholder="例如：人物說話保持自然口語；稱呼統一使用……">${h(p.user_instructions||"")}</textarea></label><label>翻譯風格（每行一項）<textarea id="style" rows="2">${h((p.translation_style||[]).join("\n"))}</textarea></label><div class="callout">儲存會建立新版本，並停止這部作品的現有預翻譯與暫存。請在閱讀頁重新開啟插件啟動翻譯，讓新資料生效。</div><h3>人名與用語對照</h3><p class="muted">日文名稱、繁體譯名與翻譯備註。其他已知設定與來源會保留。</p><div id="glossary" class="glossary">${glossary.map(termRow).join("")}</div><button type="button" data-action="add-term" class="subtle terms-footer">＋ 加入人名或用語</button><div class="actions"><button class="primary" type="submit">儲存固定資料</button><span class="hint">目前版本 ${work.revision?"v"+work.revision:"尚未建立"} · 修改保存在本機</span></div></form></section><section class="panel"><h3>來源與版本</h3>${work.error?`<p class="hint">${h(work.error)}</p>`:""}${sources.length?`<ul class="source-list">${sources.map(s=>{let url="";try{const parsed=new URL(s.url);if(["http:","https:"].includes(parsed.protocol)&&!parsed.username&&!parsed.password)url=parsed.href;}catch{}return `<li>${url?`<a href="${h(url)}" target="_blank" rel="noopener noreferrer">${h(s.title||s.url)} ↗</a>`:h(s.title||"無有效網址")} <span class="hint">${h(s.id)}</span></li>`;}).join("")}</ul>`:'<p class="no-data">尚無搜尋來源；可從插件搜尋建檔，或先手動填入資料。</p>'}${work.research?`<details><summary>搜尋紀錄與用量（唯讀）</summary><pre>${h(JSON.stringify({research:work.research,usage:work.usage},null,2))}</pre></details>`:""}${work.profile_versions.map(v=>`<div class="version-row"><div>v${v.revision} ${v.revision===work.revision?"· 目前版本":""}<br><span>${h(date(v.created))} · ${h(v.model)}</span></div>${v.revision!==work.revision?`<button data-action="restore" data-revision="${v.revision}">還原為新版本</button>`:""}</div>`).join("")}</section><section class="panel"><details><summary>進階：查看或編輯完整固定資料 JSON</summary><p class="hint">包含人物、世界設定、來源標記等欄位。「儲存完整 JSON」會以此處內容為準。</p><label>完整固定資料<textarea id="profile-json" class="code" spellcheck="false">${h(JSON.stringify(p,null,2))}</textarea></label><div class="actions"><button data-action="save-json">儲存完整 JSON</button></div></details></section>`;
  const openedId=work.id;
  storyEditor=ComicRecentStory.mount({root:$("recent-story-panel"),work,api,canStart:()=>dirty?"請先儲存固定資料的修改，再更新近期故事。":busy?"請稍候目前操作完成。":true,onMessage:message,onDone:async task=>{
    if(work?.id!==openedId)return;
    if(dirty){message(task.message+" 有未儲存修改，請先複製草稿，再重新載入最新資料。");return;}
    message(task.message);await refreshList();if(work?.id===openedId){await openWork(openedId,{check:false});storyEditor?.selectSegment(task.segment_id);}
  }});
}
async function mutate(action,data={},notice="已儲存。重新啟動此作品的翻譯後生效。"){
  const id=work.id,version=work.data_version;
  await api(`/api/library/${id}/${action}`,{...data,version});dirty=false;page=null;message(notice);await refreshList();
  if(action==="delete"){const next=works[0];if(next)await openWork(next.id,{check:false});else empty();}else await openWork(id,{check:false});
}
async function saveProfile(advanced=false){
  let profile=advanced?JSON.parse($("profile-json").value):structuredClone(work.profile||{});
  if(!advanced){
    const synopsis=$("synopsis").value,old=profile.synopsis;
    profile.synopsis=typeof old==="object"&&old?{...old,text:synopsis}:{text:synopsis,source_ids:[],certainty:"provisional"};
    if((typeof old==="string"?old:old?.text||"")!==synopsis)profile.synopsis={text:synopsis,source_ids:[],certainty:"provisional",edited_by:"user"};
    const editedStory=storyEditor.editedStory();
    if(editedStory!==undefined)profile.recent_story=editedStory;
    profile.user_instructions=$("instructions").value;profile.translation_style=$("style").value.split("\n").map(x=>x.trim()).filter(Boolean);
    const oldTerms=profile.glossary||profile.characters||[];
    profile.glossary=Array.from($("glossary").children).map((row,i)=>{const get=key=>row.querySelector(`[data-term=${key}]`).value.trim(),ja=get("ja"),zh=get("zh_tw"),notes=get("notes");if(!ja&&!zh)return null;if(!ja||!zh)throw new Error("每筆人名或用語都需要日文與繁體中文。");const old=oldTerms.find(t=>t.ja===ja&&t.zh_tw===zh&&String(t.notes||"")===notes);const previous=oldTerms.find(t=>t.ja===ja);return old?{...old}:{...previous,ja,zh_tw:zh,notes,aliases:previous?.aliases||[],kind:previous?.kind||"term",source_ids:[],certainty:"provisional",edited_by:"user"};}).filter(Boolean);
    profile.characters=profile.glossary.filter(t=>t.kind==="character");
  }
  await mutate("update",{...profileMetadata(),profile});
}
async function renderMemory(){
  const serial=ticket,id=work.id;
  $("tab-panel").innerHTML=`<section class="panel"><div class="panel-heading"><div><h3>讀過的內容，才成為記憶。</h3><p class="muted">最新記錄在前；閱讀段落彼此獨立，尚未閱讀的預翻內容留在暫存分支。</p><p class="hint">上下文依插件所選 token 檔位截取；顯式快取成功時，固定資料另有獨立額度。完整資料仍保留於本機。已識別的閱讀器原始頁不因縮放或單／雙頁切換而重複記錄；手動選取範圍仍按圖片識別。</p></div></div><div class="memory-toolbar"><label>閱讀段落<select id="segment-select">${work.segments.map(s=>`<option value="${h(s.id)}" ${segment===s.id?"selected":""}>${h(s.label)} · ${s.pages} 頁</option>`).join("")}</select></label><button data-action="clear-segment" class="danger">清除此段記憶</button></div><div id="pages"><p class="no-data">讀取逐頁記憶…</p></div></section>`;
  const result=await api(`/api/library/${id}/pages?segment=${encodeURIComponent(segment)}&offset=${pageOffset}`);
  if(serial!==ticket||tab!=="memory"||id!==work?.id)return;
  const label=work.segments.find(s=>s.id===segment)?.label||"";
  $("pages").innerHTML=result.pages.map(p=>`<div class="page-row" data-key="${h(p.page_key)}"><div class="page-summary"><div><h4>${h(label)} · ${h(memoryPageLabel(p))}</h4><p>${h(p.scene||p.preview||"尚無摘要")}</p><small class="hint">${p.source_page?.chapter_title?`章節 ID ${h(p.source_page.book_ref.split(":").at(-1))} · `:""}${p.blocks} 個文字區塊 · ${h(date(p.created))}</small></div><button data-action="open-page" data-key="${h(p.page_key)}">查看／編輯</button></div><div data-editor></div></div>`).join("")||'<div class="no-data">這個段落還沒有閱讀記憶。<br>在插件選擇此作品，使用 Gemini 翻譯後會開始累積。</div>';
  $("pages").insertAdjacentHTML("beforeend",`<div class="pagination"><button data-action="previous-pages" ${pageOffset===0?"disabled":""}>上一批</button><span>${result.total?`${pageOffset+1}–${pageOffset+result.pages.length}`:"0"} / ${result.total} 頁</span><button data-action="next-pages" ${pageOffset+result.pages.length>=result.total?"disabled":""}>下一批</button></div>`);
}
async function openPage(key){
  if(!mayLeave())return;
  const id=work.id,chosenSegment=segment,serial=ticket;
  const result=await api(`/api/library/${id}/page?segment=${encodeURIComponent(chosenSegment)}&key=${encodeURIComponent(key)}`);
  if(serial!==ticket||tab!=="memory"||chosenSegment!==segment)return;
  document.querySelectorAll("[data-editor]").forEach(el=>el.replaceChildren());page=result;dirty=false;
  const target=Array.from(document.querySelectorAll(".page-row")).find(el=>el.dataset.key===key).querySelector("[data-editor]");
  target.innerHTML=`<div class="page-editor"><p class="hint">可修正原文、譯文與本頁摘要。儲存後會重建同段後續頁的上下文，並清除這個段落的文字譯文快取。</p><div id="dialogues">${page.blocks.map(b=>`<div class="dialogue" data-block="${h(b.id)}"><label>原文 · ${h(b.id)}<textarea data-source>${h(b.source_text)}</textarea></label><label>繁體譯文<textarea data-translation>${h(b.translation)}</textarea></label></div>`).join("")}</div><label>本頁記憶摘要 JSON<textarea id="delta-json" class="code" spellcheck="false">${h(JSON.stringify(page.delta,null,2))}</textarea></label><p class="hint">證據的 id 需對應本頁文字區塊；quote 需包含至少兩字且存在於該區塊原文。移除某項記憶時，請連同其 evidence 一起移除。</p><details><summary>儲存譯文版本（${page.translations.length}）</summary>${page.translations.map(t=>`<p class="hint">${h(t.mode)} · ${h({base:"Manga-OCR",nar:"NAR",paddle:"Paddle"}[t.ocr_model||"base"]||t.ocr_model)} · ${h(t.model)} · 固定資料 v${t.revision}</p><pre>${h(t.blocks.map(b=>b.id+"："+b.translation).join("\n"))}</pre>`).join("")||'<p class="hint">此頁目前沒有文字譯文快取。</p>'}</details><details><summary>本頁翻譯時的上下文（唯讀）</summary><pre>${h(JSON.stringify(page.context,null,2))}</pre></details><div class="actions"><button data-action="save-page" class="primary">儲存這一頁</button><button data-action="delete-page" class="danger">刪除此頁記憶</button></div></div>`;
}
function renderCache(){
  const r=work.runtime;
  $("tab-panel").innerHTML=`<section class="panel"><div class="panel-heading"><div><h3>短期譯圖與自動預翻</h3><p class="muted">${r.jobs.length} 個工作 · ${r.sessions.length} 個預翻分支 · 譯圖 ${size(r.image_bytes)}</p></div><button data-action="clear-runtime" class="danger">停止並清除暫存</button></div><p class="hint">譯圖只存在本次服務記憶體，受快取上限限制，不是整本漫畫檔案。</p>${r.jobs.map(j=>`<div class="runtime-row"><div>${h({done:"翻譯完成",queued:"等待翻譯",running:"處理中",error:"失敗",canceled:"已停止"}[j.state]||j.state)}<small>${h(j.metrics.model||j.metrics.translator||j.message)} · ${h(j.metrics.cleaning_name||"")} · ${size(j.image_bytes)}</small></div>${j.image_url?`<button data-action="preview" data-job="${h(j.id)}">查看譯圖</button>`:""}</div>`).join("")||'<p class="no-data">這部作品目前沒有短期翻譯工作。</p>'}<div id="cache-preview"></div></section><section class="panel"><h3>持久文字譯文快取</h3><p class="muted">SQLite 保存 ${work.stats.translations} 份譯文，依頁面、偵測模式、OCR 模型、Gemini 模型與固定資料版本區分。可在「動態記憶」查看原文與譯文。</p><div class="actions"><button data-action="clear-translations" class="danger">清除文字譯文快取</button><span class="hint">保留固定資料與已讀記憶</span></div></section>${runtime.jobs?.some(j=>!j.catalogue_id)?'<p class="hint">有舊版或本機範例工作尚未歸屬書名；新版插件開啟閱讀頁後會建立書名分類。</p>':""}`;
}
function renderData(){
  $("tab-panel").innerHTML=`<section class="panel"><h3>閱讀器書名</h3><p class="muted">同平台的作品可對應多個閱讀器書籍 ID；不同平台的同名作品分開保存，固定資料、閱讀記憶及快取各自管理。分類書名可在「固定資料」修改。</p>${work.books.map(b=>`<div class="reader-book">${platformBadges({platforms:[b.platform]})}<strong>${h(b.title)}</strong><code>${h(b.ref)}</code></div>`).join("")||'<p class="no-data">尚未與閱讀器書籍關聯。在插件選擇此作品後便會建立關聯。</p>'}</section><section class="panel"><h3>閱讀段落</h3><p class="muted">分卷、跳讀或不同閱讀順序可使用獨立段落，避免上下文錯接。</p>${work.segments.map(s=>`<div class="version-row"><label>${s.pages} 頁記憶<input data-segment-label="${h(s.id)}" value="${h(s.label)}" maxlength="200" aria-label="段落名稱：${h(s.label)}"></label><button data-action="rename-segment" data-segment="${h(s.id)}">重新命名</button></div>`).join("")}<div class="actions"><label>新閱讀段落<input id="new-segment" maxlength="200" placeholder="例如：第 2 卷／新章節"></label><button data-action="new-segment">新增段落</button></div></section><section class="panel"><h3>匯出與清理</h3><p class="muted">匯出內容包含全部固定資料版本、來源、段落、逐頁對話、摘要及持久譯文。不包含圖片或 API 金鑰。</p><div class="actions"><button data-action="export-work">匯出此作品 JSON</button><button data-action="clear-memory" class="danger">清除全部動態記憶</button></div><div class="callout">清除記憶會同時清除文字譯文快取；固定資料保留。刪除作品則會一併移除固定資料、版本、段落、閱讀器關聯與全部記憶。</div><button data-action="delete" class="danger">刪除整部作品與資料</button></section><p class="hint">SQLite：.runtime/work-memory.sqlite3 · API 金鑰只在服務記憶體 · 建立時間 ${h(date(work.created))}</p>`;
}
const confirmAction=(description)=>confirm(`「${work.title}」\n\n${description}\n\n此操作會停止這部作品的現有翻譯與預翻譯。`);
$("content").addEventListener("input",event=>{if(event.target.id!=="segment-select"&&!event.target.closest("[data-story-preferences]"))dirty=true;});
$("content").addEventListener("submit",event=>{event.preventDefault();if(event.target.id==="profile-form")perform(()=>saveProfile());});
$("content").addEventListener("change",event=>{if(event.target.id==="segment-select"){if(!mayLeave()){event.target.value=segment;return;}segment=event.target.value;pageOffset=0;dirty=false;page=null;perform(renderMemory);}});
$("content").addEventListener("click",event=>{
  const button=event.target.closest("button");if(!button)return;
  if(busy)return;
  if(button.dataset.tab){if(!mayLeave())return;tab=button.dataset.tab;dirty=false;page=null;ticket++;perform(renderWork);return;}
  const action=button.dataset.action;if(!action)return;
  perform(async()=>{
    if(action==="create")$("create-dialog").showModal();
    else if(action==="add-term"){$("glossary").insertAdjacentHTML("beforeend",termRow());dirty=true;}
    else if(action==="remove-term"){button.closest(".term").remove();dirty=true;}
    else if(action==="save-json")await saveProfile(true);
    else if(action==="restore"){if(mayLeave()&&confirmAction(`將 v${button.dataset.revision} 的固定資料還原成新版本，保留歷史。`))await mutate("restore-profile",{revision:Number(button.dataset.revision)});}
    else if(action==="open-page")await openPage(button.dataset.key);
    else if(action==="previous-pages"||action==="next-pages"){if(!mayLeave())return;dirty=false;page=null;pageOffset+=action==="previous-pages"?-25:25;await renderMemory();}
    else if(action==="save-page"){const blocks=Array.from($("dialogues").children).map(row=>({id:row.dataset.block,source_text:row.querySelector("[data-source]").value,translation:row.querySelector("[data-translation]").value}));await mutate("edit-page",{segment_id:page.segment_id,page_key:page.page_key,blocks,delta:JSON.parse($("delta-json").value)},"已更新這一頁並重建此段後續頁的上下文。重新啟動翻譯後生效。");}
    else if(action==="delete-page"){if(confirmAction(`刪除記憶頁 ${page.seq}，並重建此段後續頁的上下文？`))await mutate(action,{segment_id:page.segment_id,page_key:page.page_key});}
    else if(action==="clear-segment"){if(confirmAction("清除目前段落的全部閱讀記憶與文字譯文快取？固定資料保留。"))await mutate("clear-memory",{segment_id:segment});}
    else if(action==="clear-memory"){if(confirmAction(`清除這部作品的全部 ${work.stats.pages} 頁記憶與文字譯文快取？固定資料保留。`))await mutate(action);}
    else if(action==="clear-translations"){if(confirmAction("清除全部持久文字譯文快取？保留原文、記憶摘要與固定資料。"))await mutate(action);}
    else if(action==="clear-runtime"){if(confirmAction("停止此作品待處理任務，清除本次服務的譯圖與處理暫存？持久資料保留。"))await mutate(action,{},"已停止並清除短期暫存。閱讀頁若仍顯示舊圖，可按「移除譯圖」。");}
    else if(action==="preview"){const image=document.createElement("img");image.className="preview-image";image.src=`/api/jobs/${encodeURIComponent(button.dataset.job)}/image`;image.alt="本次服務暫存的譯圖";$("cache-preview").replaceChildren(image);}
    else if(action==="rename-segment"){const input=Array.from(document.querySelectorAll("[data-segment-label]")).find(x=>x.dataset.segmentLabel===button.dataset.segment);await mutate("segment",{segment_id:button.dataset.segment,label:input.value});}
    else if(action==="new-segment")await mutate("segment",{label:$("new-segment").value});
    else if(action==="export-work")await download(`/api/library/${work.id}/export`,`comic-work-${work.id}.json`);
    else if(action==="delete"){if(confirmAction("永久刪除整部作品的固定資料、所有版本、閱讀器關聯、段落與全部記憶？建議先匯出。"))await mutate(action,{},"已刪除此作品與資料。");}
  });
});
$("books").addEventListener("click",event=>{const button=event.target.closest("[data-work]");if(button)perform(()=>openWork(button.dataset.work));});
$("search").addEventListener("input",renderBooks);
$("platform-filter").addEventListener("change",renderBooks);
$("refresh").addEventListener("click",()=>perform(async()=>{if(!mayLeave())return;message("");await refreshList();if(work&&works.some(w=>w.id===work.id))await openWork(work.id,{check:false});else if(works[0])await openWork(works[0].id,{check:false});else empty();}));
$("export-all").addEventListener("click",()=>perform(()=>download("/api/library/export","comic-library-backup.json")));
$("create").addEventListener("click",()=>$("create-dialog").showModal());$("cancel-create").addEventListener("click",()=>$("create-dialog").close());
$("create-form").addEventListener("submit",event=>{event.preventDefault();perform(async()=>{if(!mayLeave())return;const form=new FormData(event.target),result=await api("/api/library/create",Object.fromEntries(form));$("create-dialog").close();event.target.reset();dirty=false;await refreshList();tab="profile";await openWork(result.id,{check:false});message("已建立作品分類，可填入固定資料；新增分類不會呼叫 Gemini。");});});
window.addEventListener("beforeunload",event=>{if(dirty){event.preventDefault();event.returnValue="";}});
setInterval(()=>pollResearch(),2000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)pollResearch();});
perform(async()=>{await refreshList();const requested=new URLSearchParams(location.search).get("work");if(works.some(w=>w.id===requested))await openWork(requested,{check:false});else if(works[0])await openWork(works[0].id,{check:false});else empty();});

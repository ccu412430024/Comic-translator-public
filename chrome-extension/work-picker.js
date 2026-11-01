/* Shared local-reader / extension controls. Remote text is always textContent. */
(()=>{
  const statuses={empty:"尚未建檔",building:"Gemini 正在搜尋與整理…",ready:"作品資料已備妥",error:"作品建檔未完成",interrupted:"上次建檔已中斷"};
  function create({container,api,model,onChange=()=>{},compact=false,scope=()=>""}){
    container.innerHTML=`<label class="field">作品記憶<select data-work="select"><option value="">不使用作品記憶</option></select></label>
      <div data-work="chosen" hidden><label class="field">閱讀段落<select data-work="segment"></select></label><p data-work="state" class="hint" role="status"></p>
      <label class="work-check"><input type="checkbox" data-work="force">重新翻譯本頁，不重用譯文</label></div>
      <details data-work="details"><summary>建立作品／管理記憶</summary>
      <label class="field">日文書名<input data-work="title" type="text" maxlength="300" autocomplete="off" placeholder="輸入日文書名，或從閱讀器取得"></label>
      <label class="field">作者（可留空）<input data-work="author" type="text" maxlength="200" autocomplete="off"></label>
      <label class="field">卷數／起始段落（可留空）<input data-work="volume" type="text" maxlength="200" placeholder="例如：第 1 卷"></label>
      <label class="field">資料搜尋方式<select data-work="method"><option value="web">公開網路搜尋＋Gemini 整理（預設）</option><option value="gemini">Gemini 內建 Google 搜尋</option></select></label>
      <label class="field">建檔搜尋模型<select data-work="research-model"><option value="gemini-3.5-flash-lite">Gemini 3.5 Flash-Lite</option><option value="gemini-3.8-flash">Gemini 3.8 Flash</option><option value="gemini-3.1-pro-preview">Gemini 3.1 Pro</option><option value="">沿用目前翻譯模型</option></select></label>
      <p class="hint">預設由公開搜尋、百科與關聯網站取得資料，Gemini 規劃與整理；不需要內建搜尋權限。內建 Google 搜尋依 API 方案；資料整理沿用目前翻譯模型。</p>
      <button type="button" data-work="create" class="secondary">搜尋並建立作品</button>
      <div data-work="manage" hidden><button type="button" data-work="research" class="secondary">重新搜尋作品資料</button>
      <label class="field">從此處開始新段落<input data-work="label" type="text" maxlength="200" placeholder="例如：第 2 卷／跳讀至新章"></label>
      <button type="button" data-work="new-segment" class="secondary">開始新閱讀段落</button>
      <button type="button" data-work="export" class="secondary">匯出作品記憶</button><button type="button" data-work="delete" class="quiet">刪除此作品與記憶</button>
      <a data-work="view" target="_blank" rel="noopener">查看作品資料與來源</a><div data-work="profile" hidden></div></div>
      <p class="hint">建檔可參考官方網站、百科及社群等來源。開啟後，本機保存原文、譯文與摘要，並將相關上下文提供給 Gemini；圖片不存入作品記憶。</p>
      </details><p data-work="message" class="hint" role="status"></p>`;
    const el=name=>container.querySelector(`[data-work="${name}"]`);
    let selected=null,timer=null,serial=0,requested="",requestedSegment="",disposed=false,loading=false;
    const selectedValue=()=>({work_id:selected?.id||"",segment_id:selected?el("segment").value||"":"",force_translation:el("force").checked,loading});
    function notify(){onChange(selectedValue());}
    function message(text){el("message").textContent=text;}
    function profile(work){
      const panel=el("profile");panel.replaceChildren();panel.hidden=compact||!work.profile;
      if(panel.hidden)return;
      const synopsis=document.createElement("p");synopsis.textContent=work.profile.synopsis?.text||"沒有取得可靠的簡介。";panel.append(synopsis);
      if(work.research?.method){const method=document.createElement("p");method.className="hint";method.textContent=work.research.method==="agent-web-bootstrap"?"此初始資料由本次查網整理，Gemini 自動建檔尚未成功；可在配額恢復後重新搜尋。":"資料來源："+work.research.method;panel.append(method);}
      const details=document.createElement("details"),summary=document.createElement("summary"),pre=document.createElement("pre");summary.textContent="固定作品資料";pre.textContent=JSON.stringify(work.profile,null,2);details.append(summary,pre);panel.append(details);
      const list=document.createElement("ul");
      for(const source of work.research?.sources||[]){if(!/^https?:\/\//i.test(source.url))continue;const li=document.createElement("li"),a=document.createElement("a");a.href=source.url;a.target="_blank";a.rel="noopener noreferrer";a.textContent=`${source.id} · ${source.title||"來源"}`;li.append(a);list.append(li);}
      panel.append(list);
      if(work.research?.search_suggestions){const frame=document.createElement("iframe");frame.src=`/api/works/${work.id}/search-suggestions`;frame.title="Google 搜尋建議";frame.setAttribute("sandbox","allow-popups allow-popups-to-escape-sandbox");frame.className="work-search-suggestions";panel.append(frame);}
    }
    function render(work){
      selected=work;el("chosen").hidden=el("manage").hidden=!work;
      if(!work)return;
      const current=el("segment").value;
      el("segment").replaceChildren();for(const segment of work.segments||[]){const option=document.createElement("option");option.value=segment.id;option.textContent=segment.label;el("segment").append(option);}
      const wanted=requestedSegment||current||work.current_segment;el("segment").value=(work.segments||[]).some(x=>x.id===wanted)?wanted:work.current_segment;requestedSegment="";
      el("state").textContent=`${statuses[work.status]||work.status} · 已記憶 ${work.pages} 頁`+(work.error?` · ${work.error}`:"");
      const option=Array.from(el("select").options).find(option=>option.value===work.id);
      if(option)option.textContent=`${work.title} · ${ComicLibraryPlatforms.names(work)} · ${work.pages} 頁`+(work.status==="building"?" · 建檔中":work.status==="error"?" · 建檔未完成":"");
      if(work.status==="error"||work.status==="interrupted")message(work.error||statuses[work.status]);
      el("research").disabled=work.status==="building";
      el("view").href=`http://127.0.0.1:8765/library?work=${encodeURIComponent(work.id)}`;
      profile(work);
      clearTimeout(timer);if(work.status==="building")timer=setTimeout(()=>loadWork(work.id,false),1800);
    }
    async function loadWork(id,changed=true){
      const ticket=++serial;clearTimeout(timer);
      if(!id){loading=false;render(null);if(changed)notify();return;}
      if(changed){loading=true;render(null);notify();}
      try{const work=await api(`/api/works/${id}`);if(ticket!==serial||disposed)return;if(!ComicLibraryPlatforms.allows(work,scope()))throw new Error("作品平台不符，請選擇目前平台的作品。");loading=false;render(work);if(changed)notify();}
      catch(error){if(ticket!==serial)return;loading=false;message(error.message);render(null);el("select").value="";if(changed)notify();}
    }
    async function refresh(id=requested||el("select").value){
      const value=await api("/api/works");if(disposed)return;
      const works=value.works.filter(work=>ComicLibraryPlatforms.allows(work,scope()));
      el("select").replaceChildren();const none=document.createElement("option");none.value="";none.textContent="不使用作品記憶";el("select").append(none);
      for(const work of works){const option=document.createElement("option");option.value=work.id;option.textContent=`${work.title} · ${ComicLibraryPlatforms.names(work)} · ${work.pages} 頁${work.status==="building"?" · 建檔中":""}`;el("select").append(option);}
      el("select").value=works.some(w=>w.id===id)?id:"";requested="";await loadWork(el("select").value);
    }
    async function perform(action){try{message("");await action();}catch(error){message(error.message||"作品記憶操作失敗");}}
    el("select").addEventListener("change",()=>loadWork(el("select").value));el("segment").addEventListener("change",notify);
    el("create").addEventListener("click",()=>perform(async()=>{
      el("create").disabled=true;
      try{const work=await api("/api/works",{ref:scope(),title:el("title").value.trim(),author:el("author").value.trim(),volume:el("volume").value.trim(),model:el("research-model").value||model(),organizer_model:model(),method:el("method").value});await refresh(work.id);message("作品建檔已開始，可關閉選單，稍後再開啟查看進度。");}
      finally{el("create").disabled=false;}
    }));
    el("research").addEventListener("click",()=>perform(async()=>{const id=selected.id;await api(`/api/works/${id}/research`,{model:el("research-model").value||model(),organizer_model:model(),method:el("method").value});await loadWork(id);}));
    el("new-segment").addEventListener("click",()=>perform(async()=>{const work=await api(`/api/works/${selected.id}/segment`,{label:el("label").value});requestedSegment=work.current_segment;await loadWork(work.id);message("已開始新段落，前段對話不會被當作緊接的上一頁；固定作品資料仍會沿用。");}));
    el("export").addEventListener("click",()=>perform(async()=>{const data=await api(`/api/works/${selected.id}/export`);const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download=`comic-memory-${selected.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);}));
    el("delete").addEventListener("click",()=>perform(async()=>{if(!confirm(`刪除「${selected.title}」的作品資料與全部閱讀記憶？`))return;await api(`/api/works/${selected.id}/delete`,{});await refresh("");}));
    return {load:async(value={})=>{requested=value.work_id||"";requestedSegment=value.segment_id||"";await refresh(requested);},
      value:selectedValue,resetForce:()=>{el("force").checked=false;},refresh:()=>selected?loadWork(selected.id,false):Promise.resolve(),
      suggest:({title,author}={})=>{if(title&&!el("title").value)el("title").value=title;if(author&&!el("author").value)el("author").value=author;},
      setEnabled:value=>{container.hidden=!value;},
      dispose:()=>{disposed=true;serial++;clearTimeout(timer);}};
  }
  globalThis.ComicWorkMemory={create};
})();

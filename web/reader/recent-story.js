"use strict";

globalThis.ComicRecentStory=(()=>{
  const active=task=>["queued","running"].includes(task?.state);
  const textOf=story=>typeof story==="string"?story:story?.text||"";
  const ownerOf=story=>story?.source?.segment_id||story?.segment_id;
  function forSegment(story,segment){return !ownerOf(story)||ownerOf(story)===segment?story:story?.other_segments?.[segment];}
  function provenance(story){
    const source=story?.source;
    if(!source)return "尚未從此段落的已讀記憶建立大綱。";
    const legacy=story.kind!=="rolling-outline-v1"?"舊版近期內容；首次更新會轉成累積大綱並核對此段落全部已讀記憶。 ":"";
    return legacy+`${source.segment_label} · 已累積 ${source.page_count} 頁文字記憶 · 納入進度：記憶頁序 ${source.last_seq}`+(story.model?` · ${story.model}`:"");
  }
  function editedStory(root,segment,text){
    const previous=forSegment(root,segment);
    if(text===textOf(previous))return root;
    const story={...(typeof previous==="object"&&previous?structuredClone(previous):{}),text,edited_by:"user"};
    delete story.metrics;delete story.sections;delete story.other_segments;
    if(!story.source)story.segment_id=segment;
    const archives=structuredClone(root?.other_segments||{}),owner=ownerOf(root);
    if(owner&&owner!==segment){archives[owner]=structuredClone(root);delete archives[owner].other_segments;delete archives[owner].sections;}
    delete archives[segment];
    if(Object.keys(archives).length)story.other_segments=archives;
    return story;
  }
  function markup(work,h){
    const story=forSegment(work.profile?.recent_story,work.current_segment);
    return `<section class="recent-story" id="recent-story-panel"><h3>累積故事大綱</h3><p class="muted">以上次大綱整合新增的已讀記憶，持續保留故事主線、重要事件、人物關係變化與未解問題。此欄位獨立於上方的「故事概要」，不會覆寫它。</p>
      <label>大綱內容（可手動修改，最多 12,000 字）<textarea id="recent-story-text" rows="12" maxlength="12000" placeholder="首次從此段落全部已讀記憶建立；之後只整合尚未納入的新記憶。">${h(textOf(story))}</textarea></label>
      <p class="hint" id="story-provenance">${h(provenance(story))}</p>
      <div class="story-preferences" data-story-preferences><div class="field-grid"><label>整理模型<select id="story-model"></select></label><label id="story-custom-field" hidden>自訂模型 ID<input id="story-custom-model" maxlength="100" autocomplete="off"></label><label>閱讀段落<select id="story-segment">${work.segments.map(s=>`<option value="${h(s.id)}" ${s.id===work.current_segment?"selected":""}>${h(s.label)} · ${s.pages} 頁</option>`).join("")}</select></label></div>
      <div class="field-grid story-limits"><label>更新方式<select id="story-mode"><option value="incremental">延續上次大綱＋新增記憶</option><option value="rebuild">從此段落全部記憶重新建立</option></select></label><label>單批處理頁數<select id="story-pages">${[25,50,100,200].map(n=>`<option value="${n}" ${n===200?"selected":""}>每批最多 ${n} 頁</option>`).join("")}</select></label><label>大綱篇幅上限<select id="story-length">${[2000,4000,8000,12000].map(n=>`<option value="${n}" ${n===8000?"selected":""}>最多 ${n.toLocaleString()} 字</option>`).join("")}</select></label></div></div>
      <p class="hint" id="story-connection">讀取模型與連線設定…</p><p class="hint">每次自動補到本次按下更新時的最新已讀記憶，資料多時依先後分批整合；不會只取最後幾頁而跳過中間內容。沒有新增內容就不呼叫模型。不加入逐頁對話摘錄，也不為快取門檻補寫細節。較早頁面會依來源範圍避免帶入後續劇情；各閱讀段落的大綱分開保存。清除動態記憶仍會保留大綱，之後來源有變動時會從現存記憶重建。</p>
      <div class="actions"><button type="button" id="update-recent-story" disabled>更新累積故事大綱</button><span id="story-status" class="hint" role="status" aria-live="polite"></span></div><p id="story-tokens" class="story-tokens" role="status"></p></section>`;
  }
  function mount({root,work,api,canStart,onDone,onMessage}){
    const el=id=>root.querySelector("#"+id);
    let disposed=false,timer=null,ready=false,task=null,pending=false,segment=work.current_segment;
    const selected=()=>forSegment(work.profile?.recent_story,segment);
    const picker=ComicGeminiModels.create({select:el("story-model"),input:el("story-custom-model"),field:el("story-custom-field"),onChange:savePreferences});
    function savePreferences(){try{localStorage.setItem("comic-outline-preferences",JSON.stringify({model:picker.value(),batch:el("story-pages").value,length:el("story-length").value}));}catch{}}
    for(const id of ["story-pages","story-length"])el(id).addEventListener("change",savePreferences);
    function selectSegment(next){
      if(!Array.from(el("story-segment").options).some(option=>option.value===next))return;
      if(el("recent-story-text").value!==textOf(selected())){el("story-segment").value=segment;onMessage("請先儲存大綱的文字修改，再切換閱讀段落。",true);return;}
      segment=next;el("story-segment").value=next;el("recent-story-text").value=textOf(selected());
      el("story-provenance").textContent=provenance(selected());metrics(selected()?.metrics);
    }
    el("story-segment").addEventListener("change",()=>selectSegment(el("story-segment").value));
    function metrics(value){
      if(!value){el("story-tokens").textContent="";return;}
      const before=value.prefix_tokens_before,after=value.prefix_tokens;
      el("story-tokens").textContent=`固定前綴 token：${before==null?"未取得":before.toLocaleString()} → ${after==null?"未取得":after.toLocaleString()} · ${(value.characters||0).toLocaleString()} 字`+
        (value.new_pages==null?"":` · 本次整合 ${value.new_pages} 頁／${value.batches} 批`)+`（${value.token_model}；僅供快取資訊，尚未建立雲端快取）`;
    }
    function render(value){
      task=value;el("story-status").textContent=value.message||"";
      el("update-recent-story").disabled=!ready||pending||active(value);
      for(const control of root.querySelectorAll("[data-story-preferences] input,[data-story-preferences] select"))control.disabled=pending||active(value);
      metrics((value.segment_id===segment?value.metrics:null)||selected()?.metrics);
    }
    async function accept(value){
      render(value);
      if(active(value))timer=setTimeout(()=>poll(value.id),1500);
      else if(value.state==="done"&&value.revision>work.revision)await onDone(value);
    }
    async function poll(taskId=""){
      try{
        const value=await api(`/api/library/${work.id}/recent-story`+(taskId?`?task=${encodeURIComponent(taskId)}`:""));
        if(disposed||!root.isConnected)return;
        await accept(value);
      }catch(error){if(!disposed){el("story-status").textContent=error.message;el("update-recent-story").disabled=!ready;}}
    }
    async function start(){
      if(pending||active(task)||!ready)return;
      const reason=canStart();if(reason!==true){onMessage(reason||"請先儲存固定資料，再更新故事大綱。",true);return;}
      if(!picker.valid()){onMessage("請填入有效的 Gemini 整理模型 ID。",true);return;}
      pending=true;render(task||{});savePreferences();
      try{
        const value=await api(`/api/library/${work.id}/recent-story`,{version:work.data_version,epoch:work.epoch,profile_revision:work.revision,model:picker.value(),segment_id:segment,page_limit:Number(el("story-pages").value),length:Number(el("story-length").value),update_mode:el("story-mode").value});
        if(disposed)return;
        pending=false;clearTimeout(timer);await accept(value);
      }catch(error){if(!disposed){onMessage(error.message,true);pending=false;render(task||{});}}
    }
    el("update-recent-story").addEventListener("click",start);
    (async()=>{
      try{
        const settings=await api("/api/settings");if(disposed)return;
        let saved={};try{saved=JSON.parse(localStorage.getItem("comic-outline-preferences")||localStorage.getItem("comic-story-preferences")||"{}");}catch{}
        picker.load(settings.gemini_models,saved.model||settings.model);
        for(const [name,id] of [["batch","story-pages"],["length","story-length"]])if(Array.from(el(id).options).some(o=>o.value===String(saved[name])))el(id).value=String(saved[name]);
        ready=!!settings.rolling_story_outline;
        el("story-connection").textContent=`沿用目前連線：${settings.gemini_backend==="vertex"?"Vertex AI":"AI Studio"}。整理期間可繼續閱讀；完成後會停止此作品舊版的翻譯與預翻，請重新啟動。`;
        if(!ready)el("story-status").textContent="請更新並重新啟動本機服務。";
        await poll();
      }catch(error){if(!disposed)onMessage(error.message,true);}
    })();
    return {selectSegment,editedStory(){return editedStory(work.profile?.recent_story,segment,el("recent-story-text").value);},dispose(){disposed=true;clearTimeout(timer);}};
  }
  return {markup,mount,forSegment,editedStory};
})();

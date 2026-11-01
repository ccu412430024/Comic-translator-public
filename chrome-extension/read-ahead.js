import {rgbDifference} from "./frame-utils.js";

export class ReadAheadQueue{
  constructor({id,book_ref,options,frames,history=[],lookAhead=frames.length-1,refill=null,api,image,onUpdate=()=>{},endLabel="已到書末",delay=ms=>new Promise(resolve=>setTimeout(resolve,ms))}){
    Object.assign(this,{id,book_ref,options,api,image,delay,onUpdate,refill,lookAhead,endLabel});
    this.origin=frames[0].position;this.current=this.origin;
    this.session="";this.canceled=false;this.error="";this.refillError="";
    this.processing=null;this.refilling=null;this.lastRefilled=this.current;this.retryAt=0;
    this.frames=history.filter(f=>{
      if(!f.translated||!f.activated||f.width!==frames[0].width||f.height!==frames[0].height)return false;
      const incoming=frames.find(p=>p.position===f.position);
      return !incoming||rgbDifference(f.sample,incoming.sample)<=4.5;
    }).map(f=>({...f,historical:true}));
    this.windowPositions=frames.map(f=>f.position);
    this.merge({frames,positions:this.windowPositions});
  }
  get ready(){return this.frames.filter(f=>f.translated).length;}
  pending(){return this.frames.filter(f=>f.job&&!f.foreground&&!f.historical&&!f.activated).length;}
  next(active=new Map()){
    const last=this.windowPositions.at(-1);
    return this.frames.find(f=>!active.has(f)&&!f.translated&&(f.job||f.image&&f.position<=last&&(f.foreground||this.pending()<5)));
  }
  merge({frames,positions}){
    for(const incoming of frames){
      const old=this.frames.find(f=>f.position===incoming.position);
      if(old){
        if(old.width!==incoming.width||old.height!==incoming.height||rgbDifference(old.sample,incoming.sample)>4.5)throw new Error("預翻譯版面已改變，正在重新對應目前畫面。");
        if(!old.job&&!old.image)old.image=incoming.image;
        continue;
      }
      this.frames.push(Object.assign(incoming,{foreground:incoming.position===this.origin}));
    }
    this.frames.sort((a,b)=>a.position-b.position);
    this.windowPositions=positions||frames.map(f=>f.position);
    this.prune();
  }
  prune(){
    // Keep a small back-reading cache, the current screen and unread jobs.
    while(this.frames.length>10){
      const removable=this.frames.filter(f=>f.position!==this.current&&!this.windowPositions.includes(f.position)&&(f.activated||f.historical||!f.job));
      removable.sort((a,b)=>Math.abs(b.position-this.current)-Math.abs(a.position-this.current));
      const drop=removable[0];if(!drop)break;
      this.frames.splice(this.frames.indexOf(drop),1);delete drop.image;delete drop.translated;
    }
  }
  start(){
    if(this.processing)return this.processing;
    if(this.canceled||this.error)return Promise.resolve();
    this.processing=this.drain().finally(()=>{
      this.processing=null;
      if(!this.canceled&&!this.error&&this.next())return this.start();
    });
    return this.processing;
  }
  async drain(){
    const active=new Map();
    try{
      while(!this.canceled&&!this.error){
        const limit=Math.max(1,Math.min(5,this.options.prefetch_parallelism||1));
        let frame;
        while(active.size<limit&&!this.canceled&&!this.error&&(frame=this.next(active))){
          // Finish the visible screen before creating the speculative snapshot.
          if(active.size&&(frame.foreground||[...active.keys()].some(f=>f.foreground)))break;
          await this.submitFrame(frame);
          if(this.canceled||this.error)break;
          const current=frame;
          const task=this.finishFrame(current).catch(error=>{
            if(!this.canceled)this.error=error.message||"預翻譯失敗。";
            this.onUpdate();
          }).finally(()=>active.delete(current));
          active.set(current,task);
          if(frame.foreground)break;
        }
        if(!active.size)break;
        await Promise.race(active.values());
      }
    }catch(error){if(!this.canceled)this.error=error.message||"預翻譯失敗。";this.onUpdate();}
    finally{
      if((this.canceled||this.error)&&this.session)await this.api(`/api/prefetch/${this.session}/cancel`,{}).catch(()=>{});
      await Promise.allSettled(active.values());
    }
  }
  async submitFrame(frame){
    if(frame.job)return;
    if(!frame.foreground&&!this.session){
      const branch=await this.api("/api/prefetch",{...this.options,book_ref:this.book_ref});this.session=branch.id;
      if(Number.isInteger(branch.parallelism))this.options.prefetch_parallelism=branch.parallelism;
    }
    if(this.canceled||this.error)return;
    frame.session=frame.foreground?"":this.session;
    frame.job=await this.api(frame.foreground?"/api/jobs":`/api/prefetch/${this.session}/pages`,frame.foreground?{...this.options,image:frame.image,page_identity:frame.page_identity}:{image:frame.image,page_id:String(frame.position),page_identity:frame.page_identity});
    if(this.canceled||this.error)await this.api(`/api/jobs/${frame.job.id}/cancel`,{}).catch(()=>{});
    this.onUpdate();
  }
  async finishFrame(frame){
    while(["queued","running"].includes(frame.job.state)&&!this.canceled&&!this.error){
      await this.delay(650);if(this.canceled||this.error)break;
      frame.job=await this.api(`/api/jobs/${frame.job.id}`);
    }
    if(this.canceled||this.error)return;
    if(frame.job.state!=="done")throw new Error(frame.job.error||"預翻譯未完成。");
    const translated=await this.image(frame.job);if(this.canceled||this.error)return;
    frame.translated=translated;delete frame.image;
    if(frame.foreground)frame.activated=true;
    this.onUpdate();
  }
  requestRefill(){
    if(!this.refill||this.canceled||this.error||this.refilling||this.current===this.lastRefilled||Date.now()<this.retryAt)return;
    const position=this.current;
    this.refilling=Promise.resolve().then(()=>this.refill(position,this.frames.filter(f=>f.job||f.image||f.translated).map(f=>f.position))).then(prepared=>{
      if(this.canceled||this.current!==position||!prepared)return;
      this.merge(prepared);this.lastRefilled=position;this.refillError="";
      this.onUpdate();this.start();
    }).catch(error=>{
      if(!this.canceled&&this.current===position){this.refillError=error.message||"後續畫面尚未載入";this.retryAt=Date.now()+2500;this.onUpdate();}
    }).finally(()=>{this.refilling=null;});
  }
  status(){
    const future=this.windowPositions.filter(p=>p>this.current).slice(0,this.lookAhead);
    const ready=future.filter(p=>this.frames.find(f=>f.position===p)?.translated).length;
    const replenishing=this.current!==this.lastRefilled;
    const running=this.frames.filter(f=>!f.foreground&&f.job&&["queued","running"].includes(f.job.state)).length;
    const retry=this.frames.find(f=>f.position>this.current&&f.job?.state==="running"&&f.job?.stage==="retry");
    return (!replenishing&&!future.length?this.endLabel+" · ":`自動預翻 ${ready}/${replenishing?this.lookAhead:future.length} · `)+(running>1?`${running} 個畫面處理中 · `:"")+(retry?`後頁：${retry.job.message} · `:"");
  }
  async view({page_id,sample,shown,width,height}){
    if(this.canceled)return {stopped:true,text:"自動翻譯已停止。"};
    const frame=this.frames.find(f=>String(f.position)===page_id);
    if(!frame)return {needs_capture:true,text:"正在以目前畫面重新安排自動翻譯…"};
    if(width&&width!==frame.width||height&&height!==frame.height||!Array.isArray(sample)||sample.length!==96*64*4||rgbDifference(frame.sample,sample)>4.5)return {needs_capture:true,text:"正在等待閱讀器畫面載入，或畫面已變更。"};
    this.current=frame.position;
    if(!frame.job&&!frame.image&&!frame.translated)return {needs_capture:true,text:"正在重新取得目前畫面…"};
    if(this.error&&!frame.translated)return {text:this.status()+this.error};
    if(!frame.translated)return {text:this.status()+(frame.job?.message||"等待前頁翻譯完成…")};
    if(!frame.foreground&&!frame.historical&&!frame.activated){
      if(this.frames.some(prior=>prior.session===frame.session&&prior.position<frame.position&&!prior.translated)){
        return {text:this.status()+"本頁譯圖已就緒，等待前頁記憶處理完成。",job_id:frame.job.id,
          ...(shown===frame.job.id?{}:{image:frame.translated}),metrics:frame.job.metrics};
      }
      frame.activating=frame.activating||this.api(`/api/prefetch/${frame.session}/activate`,{book_ref:this.book_ref,page_id}).then(()=>{
        for(const prior of this.frames)if(prior.session===frame.session&&prior.position<=frame.position)prior.activated=true;
      });
      try{await frame.activating;}catch(error){this.error=error.message;return {text:this.error,stopped:true};}
      this.onUpdate();
    }
    this.prune();this.requestRefill();this.start();
    const note=this.error?" · 後續自動翻譯已停止："+this.error:this.refillError?" · 預讀稍後重試："+this.refillError:"";
    const acceleration=globalThis.ComicGeminiStatus?.format(frame.job.metrics)||"";
    return {text:this.status()+"已套用本頁譯圖"+(acceleration?" · "+acceleration:"")+note,job_id:frame.job.id,...(shown===frame.job.id?{}:{image:frame.translated}),metrics:frame.job.metrics};
  }
  history(){return this.frames.filter(f=>f.translated&&f.activated).map(f=>({...f}));}
  snapshot(){
    return {origin:this.origin,current:this.current,session:this.session,windowPositions:this.windowPositions,lookAhead:this.lookAhead,error:this.error,
      frames:this.frames.map(f=>{const {image,translated,activating,...metadata}=f;return metadata;})};
  }
  restore(snapshot,frames){
    Object.assign(this,{origin:snapshot.origin,current:snapshot.current,session:snapshot.session,windowPositions:snapshot.windowPositions,lookAhead:snapshot.lookAhead,error:snapshot.error||"",frames,lastRefilled:null});
    return this;
  }
  async stop(){
    this.canceled=true;
    await Promise.allSettled(this.frames.filter(f=>f.job&&["queued","running"].includes(f.job.state)).map(f=>this.api(`/api/jobs/${f.job.id}/cancel`,{})));
    if(this.session)await this.api(`/api/prefetch/${this.session}/cancel`,{}).catch(()=>{});
    for(const frame of this.frames){delete frame.image;delete frame.translated;}
  }
}

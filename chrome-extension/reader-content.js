/* Read-only page watcher. No synthetic input or navigation. */
(()=>{
  "use strict";
  if(globalThis.__comicReaderContent)return;
  globalThis.__comicReaderContent=true;
  let run="",host=null,root=null,layer=null,label=null,toggle=null,timer=null,busy=false,shown="",lastHash="",lastInfo=null,lastError="",errorAt=0;
  const css=`:host{all:initial;position:fixed;inset:0;z-index:2147483647;pointer-events:none;font:13px/1.4 'Microsoft JhengHei',sans-serif}canvas{position:fixed;pointer-events:none}.bar{position:fixed;top:14px;right:14px;display:flex;gap:8px;align-items:center;box-sizing:border-box;max-width:min(560px,calc(100vw - 28px));background:#173c2f;color:white;border-radius:9px;padding:10px 12px;pointer-events:auto;box-shadow:0 2px 16px #0004}.bar span{min-width:0}.bar button{white-space:nowrap;flex-shrink:0;font:12px 'Microsoft JhengHei',sans-serif;border:0;border-radius:5px;padding:6px 9px;background:#edf4e8;color:#214531;cursor:pointer}`;
  function mount(){
    if(host?.isConnected)return;
    host=document.createElement("div");host.dataset.comicReader=ComicReaders.match(location.href)?.id||"reader";root=host.attachShadow({mode:"closed"});const sheet=new CSSStyleSheet();sheet.replaceSync(css);root.adoptedStyleSheets=[sheet];
    (document.fullscreenElement||document.documentElement).append(host);
    const bar=document.createElement("div");bar.className="bar";
    for(const type of ["pointerdown","pointerup","click","dblclick","keydown"])bar.addEventListener(type,e=>e.stopPropagation());
    label=document.createElement("span");bar.append(label);
    toggle=document.createElement("button");toggle.textContent="顯示原文";toggle.hidden=true;toggle.onclick=()=>{if(layer){layer.hidden=!layer.hidden;toggle.textContent=layer.hidden?"顯示譯文":"顯示原文";}};
    const stop=document.createElement("button");stop.textContent="停止自動翻譯";stop.onclick=()=>{chrome.runtime.sendMessage({type:"comic:reader:stop",run_id:run}).catch(()=>{});clear();};
    bar.append(toggle,stop);root.append(bar);
  }
  function clear(){clearInterval(timer);timer=null;host?.remove();host=root=layer=label=toggle=null;shown=lastHash="";lastInfo=null;run="";}
  function text(value){mount();label.textContent=value;}
  function paint(bitmap,rect){layer?.remove();layer=document.createElement("canvas");layer.width=bitmap.width;layer.height=bitmap.height;layer.getContext("2d").drawImage(bitmap,0,0);Object.assign(layer.style,{left:rect.x+"px",top:rect.y+"px",width:rect.width+"px",height:rect.height+"px"});root.prepend(layer);}
  async function probe(){
    if(busy||!run||document.hidden)return;busy=true;const id=run;
    try{
      const state=ComicReader.current(),key=[state.position,state.fingerprint,state.width,state.height,state.rect.x,state.rect.y,state.rect.width,state.rect.height].join(":");
      if(lastInfo?.key!==key)lastInfo={key,frame:await ComicReader.frame()};
      if(id!==run)return;
      const frame=lastInfo.frame;
      if(layer)Object.assign(layer.style,{left:frame.rect.x+"px",top:frame.rect.y+"px",width:frame.rect.width+"px",height:frame.rect.height+"px"});
      if(lastHash!==frame.hash){layer?.remove();layer=null;toggle.hidden=true;shown="";lastHash=frame.hash;}
      const result=await chrome.runtime.sendMessage({type:"comic:reader:page",run_id:id,page_id:String(frame.position),frame_hash:frame.hash,width:frame.width,height:frame.height,sample:frame.sample,shown});
      if(id!==run)return;
      if(result?.text)text(result.text);
      if(result?.stopped){clearInterval(timer);timer=null;}
      if(result?.image&&result.job_id!==shown){
        const bitmap=await createImageBitmap(await (await fetch(result.image)).blob());
        const now=await ComicReader.frame();
        if(id===run&&now.hash===frame.hash&&now.position===frame.position){paint(bitmap,now.rect);shown=result.job_id;toggle.hidden=false;toggle.textContent="顯示原文";
          chrome.runtime.sendMessage({type:"comic:reader:diagnostic",run_id:id,phase:"display",position:frame.position,job_id:shown}).catch(()=>{});}
        bitmap.close();
      }
    }catch(error){
      layer?.remove();layer=null;if(toggle)toggle.hidden=true;shown="";
      const message=String(error.message||"正在等待閱讀器畫面載入…").slice(0,300);if(label)label.textContent=message;
      if(lastError!==message||Date.now()-errorAt>15000){lastError=message;errorAt=Date.now();chrome.runtime.sendMessage({type:"comic:reader:diagnostic",run_id:id,error:message}).catch(()=>{});}
    }
    finally{busy=false;}
  }
  chrome.runtime.onMessage.addListener((message,sender,reply)=>{
    if(sender.id!==chrome.runtime.id)return;
    if(message.type==="comic:reader:prepare"){
      clear();run=message.run_id;text("正在取得本頁與後續圖片，不會切換閱讀位置…");
      ComicReader.frame().then(frame=>reply({frame,book_ref:ComicReader.reference()}),error=>{text(error.message);reply({error:error.message});});return true;
    }
    if(message.type==="comic:reader:watch"&&message.run_id===run){clearInterval(timer);timer=setInterval(probe,650);reply({ok:true});return;}
    if(message.type==="comic:reader:clear"){clear();reply({ok:true});return;}
    if(message.type==="comic:reader:status"&&message.run_id===run){text(message.text);reply({ok:true});}
  });
})();

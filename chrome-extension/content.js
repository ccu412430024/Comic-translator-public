(()=>{
  "use strict";
  if(globalThis.__comicReaderOverlay)return;
  globalThis.__comicReaderOverlay=true;
  let serial=0,host=null,root=null,canvas=null,bar=null,label=null,toggle=null,selection=null,snapshot=null;
  let pageWatch=null,watchTimer=null,captureCheck=null;
  const checks=new Set();
  const css=`:host{all:initial;position:fixed;inset:0;pointer-events:none;z-index:2147483647;font:14px/1.5 'Microsoft JhengHei',sans-serif}canvas{position:fixed;pointer-events:none}.bar{position:fixed;right:14px;top:14px;max-width:420px;display:flex;align-items:center;gap:8px;padding:10px 12px;border-radius:9px;background:#173c2f;color:#fff;box-shadow:0 2px 16px #0004;pointer-events:auto}.bar span{font-size:12px}.bar button{background:#edf4e8;color:#214531;border:0;border-radius:5px;padding:6px 9px;cursor:pointer;font:12px 'Microsoft JhengHei',sans-serif}.selector{position:fixed;inset:0;pointer-events:auto;cursor:crosshair;background:#0003}.selector p{position:fixed;top:20px;left:50%;transform:translateX(-50%);background:#173c2f;color:white;padding:10px 16px;border-radius:8px;margin:0}.box{position:fixed;border:2px solid #66e59e;background:#63e39e22;pointer-events:none}`;
  function mount(){
    if(host?.isConnected)return;
    host=document.createElement("div");host.dataset.comicReader="overlay";root=host.attachShadow({mode:"closed"});
    const sheet=new CSSStyleSheet();sheet.replaceSync(css);root.adoptedStyleSheets=[sheet];
    (document.fullscreenElement||document.documentElement).append(host);
  }
  function clear(){serial++;selection?.cancel();selection=null;host?.remove();host=root=canvas=bar=label=toggle=null;snapshot=null;pageWatch=captureCheck=null;clearInterval(watchTimer);watchTimer=null;for(const timer of checks)clearTimeout(timer);checks.clear();}
  function state(){return {width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,url:location.href};}
  function samePosition(){const now=state();return snapshot&&now.width===snapshot.width&&now.height===snapshot.height&&now.x===snapshot.x&&now.y===snapshot.y&&now.url===snapshot.url;}
  function checkPage(){if(!snapshot||selection||document.hidden)return;if(!samePosition()||pageWatch?.changed())clear();}
  function current(token){checkPage();return token===serial&&!!samePosition();}
  function verifyAfterNavigation(){
    if(!captureCheck||checks.size||!pageWatch?.needsCapture)return;
    const token=serial;
    for(const delay of [250,1050,2050]){
      const timer=setTimeout(()=>{checks.delete(timer);if(current(token)&&!document.hidden&&captureCheck)chrome.runtime.sendMessage({type:"comic:verify",token,watch:captureCheck}).catch(()=>{});},delay);
      checks.add(timer);
    }
  }
  function status(text){
    mount();host.style.visibility="visible";
    if(!bar){bar=document.createElement("div");bar.className="bar";
      for(const type of ["pointerdown","pointerup","click","dblclick","keydown"])bar.addEventListener(type,event=>event.stopPropagation());
      label=document.createElement("span");bar.append(label);const close=document.createElement("button");close.textContent="關閉";close.onclick=clear;bar.append(close);root.append(bar);}
    label.textContent=text;
  }
  async function painted(){await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));}
  function selectRect(){
    mount();return new Promise(resolve=>{
      const screen=document.createElement("div");screen.className="selector";
      const hint=document.createElement("p");hint.textContent="拖曳選取漫畫範圍 · Esc 取消";
      const box=document.createElement("div");box.className="box";screen.append(hint,box);root.append(screen);
      let start=null;
      const finish=value=>{screen.remove();document.removeEventListener("keydown",onKey,true);selection=null;resolve(value);};
      const onKey=event=>{if(event.key==="Escape"){event.preventDefault();event.stopImmediatePropagation();finish(null);}};
      screen.addEventListener("pointerdown",event=>{event.preventDefault();event.stopPropagation();start={x:event.clientX,y:event.clientY};screen.setPointerCapture(event.pointerId);});
      screen.addEventListener("pointermove",event=>{if(!start)return;Object.assign(box.style,{left:Math.min(start.x,event.clientX)+"px",top:Math.min(start.y,event.clientY)+"px",width:Math.abs(start.x-event.clientX)+"px",height:Math.abs(start.y-event.clientY)+"px"});});
      screen.addEventListener("pointerup",event=>{event.preventDefault();event.stopPropagation();if(!start)return;const rect={x:Math.max(0,Math.min(start.x,event.clientX)),y:Math.max(0,Math.min(start.y,event.clientY)),width:Math.abs(start.x-event.clientX),height:Math.abs(start.y-event.clientY)};finish(rect.width>=32&&rect.height>=32?rect:null);});
      selection={cancel:()=>finish(null)};document.addEventListener("keydown",onKey,true);
    });
  }
  chrome.runtime.onMessage.addListener((message,sender,sendResponse)=>{
    if(sender.id!==chrome.runtime.id)return;
    if(message.type==="comic:prepare"){
      (async()=>{clear();const token=serial;mount();let rect={x:0,y:0,width:innerWidth,height:innerHeight};
        if(message.select){rect=await selectRect();if(!rect||serial!==token){sendResponse({canceled:true});return;}}
        snapshot=state();pageWatch=new ComicReaderPageWatch(rect);watchTimer=setInterval(checkPage,800);
        host.style.visibility="hidden";await painted();
        sendResponse({token,rect,watchRect:pageWatch?.captureRect||rect,viewport:{width:innerWidth,height:innerHeight},book_context:globalThis.ComicBookContext?.read(document,location.href)||null});
      })();return true;
    }
    if(message.type==="comic:clear"){clear();sendResponse({ok:true});return;}
    const valid=current(message.token);
    if(message.type==="comic:status"){if(valid)status(message.text);sendResponse({current:!!valid});return;}
    if(message.type==="comic:check"){
      (async()=>{if(valid){host.style.visibility="hidden";await painted();}sendResponse({current:!!current(message.token)});})();return true;
    }
    if(message.type==="comic:restore"){if(valid)host.style.visibility="visible";sendResponse({current:!!valid});return;}
    if(message.type==="comic:invalidate"){if(valid)clear();sendResponse({current:false});return;}
    if(message.type==="comic:error"){if(valid)status(message.text);sendResponse({current:!!valid});return;}
    if(message.type==="comic:result"){
      (async()=>{if(!valid){sendResponse({current:false});return;}
        const binary=atob(message.image.split(",")[1]);const bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));
        const bitmap=await createImageBitmap(new Blob([bytes],{type:"image/png"}));
        if(!current(message.token)){bitmap.close();sendResponse({current:false});return;}
        canvas=document.createElement("canvas");canvas.width=bitmap.width;canvas.height=bitmap.height;
        Object.assign(canvas.style,{left:message.rect.x+"px",top:message.rect.y+"px",width:message.rect.width+"px",height:message.rect.height+"px"});
        canvas.getContext("2d").drawImage(bitmap,0,0);bitmap.close();root.prepend(canvas);
        captureCheck=message.watch||null;
        status([message.metrics.mode_name,message.metrics.cleaning_name,message.metrics.layout_name,message.metrics.model,message.metrics.memory?.work_title,`${message.metrics.translated_blocks} 處文字`,globalThis.ComicGeminiStatus?.format(message.metrics),message.metrics.memory?.error].filter(Boolean).join(" · "));
        toggle=document.createElement("button");toggle.textContent="顯示原文";toggle.onclick=()=>{canvas.hidden=!canvas.hidden;toggle.textContent=canvas.hidden?"顯示譯文":"顯示原文";};bar.insertBefore(toggle,bar.lastChild);
        sendResponse({current:true});
      })().catch(()=>{status("無法顯示譯圖，請重新翻譯");sendResponse({current:false});});return true;
    }
  });
  document.addEventListener("click",event=>{
    if(!host||selection||event.composedPath().includes(host))return;
    checkPage();if(!host)return;
    const control=event.target.closest?.('button,a,input,select,textarea,[role="button"],[contenteditable="true"]');
    const label=(control?.getAttribute("aria-label")||control?.getAttribute("title")||control?.textContent||"").trim();
    if(/下一頁|上一頁|下頁|上頁|下一页|上一页|次のページ|前のページ|次へ|前へ|\bnext\b|\bprev(?:ious)?\b|^[<>‹›«»←→]$/i.test(label)||(!control&&(!pageWatch?.items.length||pageWatch.contains(event.clientX,event.clientY))))verifyAfterNavigation();
  },true);
  document.addEventListener("keydown",event=>{
    if(selection||event.composedPath().includes(host)||event.target.closest?.('input,select,textarea,[contenteditable="true"]'))return;
    if(event.key==="Escape")clear();
    else if(["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","PageUp","PageDown","Home","End"," "].includes(event.key))verifyAfterNavigation();
  },true);
  for(const event of ["scroll","resize","popstate","hashchange","fullscreenchange"])window.addEventListener(event,checkPage,{passive:true,capture:event==="scroll"});
  document.addEventListener("visibilitychange",()=>{if(!document.hidden){checkPage();verifyAfterNavigation();}});
})();

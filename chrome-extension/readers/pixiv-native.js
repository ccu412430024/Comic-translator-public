/* Public episode loader. URLs and per-image parameters remain in page RAM. */
(()=>{
  "use strict";
  if(location.hostname!=="comic.pixiv.net"||!/^\/viewer\/stories\/[1-9]\d*\/?$/.test(location.pathname))return;
  const VERSION=4;
  if(globalThis.ComicPixivNative?.status?.()?.version===VERSION)return;
  const images=new Map();let cached=null,pending=null,preparing=false,loaded=0,lastError="",observed=null;
  const reference=()=>location.hostname==="comic.pixiv.net"&&/^\/viewer\/stories\/[1-9]\d*\/?$/.test(location.pathname)?"pixiv:episode:"+location.pathname.match(/\/stories\/(\d+)/)[1]:"";
  async function episode(){
    const ref=reference();if(cached?.ref===ref&&Date.now()-cached.at<180000)return cached.value;
    if(pending?.ref===ref)return pending.promise;
    const promise=(async()=>{
      let props;try{props=JSON.parse(document.getElementById('__NEXT_DATA__').textContent).props.pageProps;}catch{throw new Error("尚未取得 Pixiv 閱讀器公開資料。");}
      if(String(props.id)!==ref.split(':').at(-1)){
        // Next's initial script can remain from the previous SPA chapter. Read
        // this chapter's public HTML instead of borrowing its old salt/identity.
        const html=await fetch(location.pathname,{credentials:"same-origin",signal:AbortSignal.timeout(20000)});
        if(!html.ok)throw new Error("章節資料仍在切換，稍後重試。");
        const doc=new DOMParser().parseFromString(await html.text(),"text/html");
        try{props=JSON.parse(doc.getElementById('__NEXT_DATA__').textContent).props.pageProps;}catch{throw new Error("尚未取得新章節的公開資料。");}
      }
      if(String(props.id)!==ref.split(':').at(-1)||typeof props.salt!=="string")throw new Error("章節資料仍在切換，請等閱讀器載入。");
      const time=new Date().toISOString().replace(/\.\d{3}Z$/,"Z"),digest=await crypto.subtle.digest("SHA-256",new TextEncoder().encode(time+props.salt));
      const hash=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,"0")).join("");
      const response=await fetch(`/api/app/episodes/${props.id}/read_v4`,{headers:{"X-Requested-With":"pixivcomic","X-Client-Time":time,"X-Client-Hash":hash},credentials:"same-origin",signal:AbortSignal.timeout(20000)});
      if(!response.ok)throw new Error(`Pixiv 閱讀資料暫時無法取得（HTTP ${response.status}）。`);
      const raw=await response.json(),value=raw.data?.reading_episode;
      if(reference()!==ref)throw new Error("閱讀章節已變更。");
      observed={ref,state:typeof value?.state==="string"?value.state.slice(0,80):null,displayable:typeof value?.is_displayable==="boolean"?value.is_displayable:null,pages:Array.isArray(value?.pages)?value.pages.length:0};
      if(!value||String(value.id)!==String(props.id))throw new Error("Pixiv 閱讀資料與目前章節未能對應，請等閱讀器載入後重試。");
      // is_displayable is not reading authorization: episode 55937 returns
      // false while the official endpoint grants readable and supplies pages.
      if(value.state!=="readable")throw new Error("此章節目前無法閱讀，請先確認閱讀器能正常顯示。");
      if(!Array.isArray(value.pages)||!value.pages.length)throw new Error("Pixiv 尚未提供本章節的頁面資料，請等閱讀器載入後重試。");
      if(value.pages.length>10000)throw new Error("章節頁數超出支援範圍。");
      const order=await globalThis.ComicPixivOrder?.load(props.workId,props.id)||null;
      if(reference()!==ref)throw new Error("閱讀章節已變更。");
      cached={ref,at:Date.now(),value,order};return value;
    })();
    pending={ref,promise};try{return await promise;}finally{if(pending?.promise===promise)pending=null;}
  }
  async function load(page){
    let url;try{url=new URL(page.url);}catch{throw new Error("Pixiv 圖片來源無效。");}
    if(url.protocol!=="https:"||!["img-comic.pximg.net","public-img-comic.pximg.net"].includes(url.hostname)||url.username||url.password)throw new Error("Pixiv 圖片來源尚未支援。");
    const key=page.url+":"+String(page.key||"");if(images.has(key)){const value=images.get(key);images.delete(key);images.set(key,value);return value;}
    if(![page.width,page.height].every(n=>Number.isInteger(n)&&n>0)||page.width*page.height>20_000_000)throw new Error("Pixiv 圖片尺寸無效。");
    const response=await fetch(url.href,{headers:page.key?{"X-Cobalt-Thumber-Parameter-GridShuffle-Key":page.key}:{},credentials:"omit",signal:AbortSignal.timeout(20000)});
    if(!response.ok){cached=null;throw new Error(`Pixiv 圖片暫時無法載入（HTTP ${response.status}），稍後會重新取得閱讀資料。`);}
    const blob=await response.blob();if(blob.size>16*1024*1024)throw new Error("Pixiv 圖片過大。");
    const bitmap=await createImageBitmap(blob);
    try{
      if(bitmap.width!==page.width||bitmap.height!==page.height)throw new Error("Pixiv 圖片尺寸與閱讀資料不符。");
      const canvas=document.createElement("canvas");canvas.width=page.width;canvas.height=page.height;
      const pen=canvas.getContext("2d",{willReadFrequently:true});pen.drawImage(bitmap,0,0);
      if(page.key){const pixels=pen.getImageData(0,0,canvas.width,canvas.height),restored=await ComicPixivImage.restore(pixels.data,canvas.width,canvas.height,page.gridsize||32,page.key);pen.putImageData(new ImageData(restored,canvas.width,canvas.height),0,0);}
      images.set(key,canvas);loaded++;
      while(images.size>12){const oldest=images.keys().next().value,drop=images.get(oldest);images.delete(oldest);drop.width=drop.height=0;}
      return canvas;
    }finally{bitmap.close();}
  }
  function status(){const ref=reference(),current=cached?.ref===ref?cached:null,latest=observed?.ref===ref?observed:null;
    return {ready:!!current,version:VERSION,episode_id:ref.split(':').at(-1)||"",page_count:current?.value.pages.length||0,
      read_state:latest?.state??null,is_displayable:latest?.displayable??null,reported_pages:latest?.pages||0,
      chapter_order_source:current?.order?.source||"unavailable",chapter_order_count:current?.order?.chapters.length||0,
      decoded_images:images.size,loaded_images:loaded,last_error:lastError};}
  async function prepare(input){
    if(preparing)throw new Error("Pixiv 預讀正在準備中。");
    if(input.book_ref!==reference()||!Number.isInteger(input.position)||input.position<1||!Number.isInteger(input.count)||input.count<0||input.count>5||![input.width,input.height].every(n=>Number.isInteger(n)&&n>=128)||input.width*input.height>20_000_000||!["spread","single","vertical"].includes(input.layout?.mode))throw new Error("Pixiv 預讀參數無效。");
    preparing=true;lastError="";
    try{
      const value=await episode(),mode=input.layout.mode,groups=ComicPixivImage.groups(value.pages.length,mode,value.two_page_layout==="left");
      const known=new Set((input.known_positions||[]).slice(0,10)),frames=[],positions=[];
      if(input.position>groups.length)throw new Error("閱讀器頁序與章節資料不符。");
      for(let p=input.position;p<=Math.min(groups.length,input.position+input.count);p++){
        positions.push(p);if(p!==input.position&&known.has(p))continue;
        if(input.book_ref!==reference())throw new Error("閱讀章節已變更。");
        const group=groups[p-1],canvas=document.createElement("canvas");canvas.width=input.width;canvas.height=input.height;
        const pen=canvas.getContext("2d");pen.fillStyle=input.background||"white";pen.fillRect(0,0,canvas.width,canvas.height);
        const regions=[];
        for(let slot=0;slot<group.length;slot++){
          const index=group[slot];if(index===null)continue;
          const page=value.pages[index],image=await load(page);
          let rect;
          if(mode==="vertical")rect={x:0,y:0,width:canvas.width,height:canvas.height};
          else{
            const box={x:group.length===2?(slot===0?canvas.width/2:0):0,y:0,width:canvas.width/group.length,height:canvas.height};
            rect=ComicPixivImage.fit(page.width,page.height,box,group.length===2?(slot===0?"left":"right"):"center");
          }
          pen.drawImage(image,rect.x,rect.y,rect.width,rect.height);
          const clip=(n,size)=>Math.max(0,Math.min(1,n/size));
          regions.push({index,bbox:[clip(rect.x,canvas.width),clip(rect.y,canvas.height),clip(rect.x+rect.width,canvas.width),clip(rect.y+rect.height,canvas.height)]});
        }
        if(input.book_ref!==reference())throw new Error("閱讀章節已變更。");
        const image=canvas.toDataURL("image/png");canvas.width=canvas.height=0;
        if(image.length>16*1024*1024)throw new Error("Pixiv 預讀畫面過大。");
        const page_identity={book_ref:input.book_ref,pages:regions};
        if(cached?.order)page_identity.chapter_order=cached.order;
        frames.push({position:p,width:input.width,height:input.height,image,source_pages:regions.map(r=>r.index),page_identity});
      }
      return {book_ref:input.book_ref,positions,frames,native:status()};
    }catch(error){lastError=error.name==="TimeoutError"?"Pixiv 載入逾時，稍後重試。":error.message;throw new Error(lastError);}
    finally{preparing=false;}
  }
  globalThis.ComicPixivNative={prepare,status};
})();

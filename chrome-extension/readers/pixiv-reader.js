/* Read visible page nodes. Never navigate or change the site's reading state. */
(()=>{
  "use strict";
  function rect(node){const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};}
  function area(r){return Math.max(0,Math.min(innerWidth,r.x+r.width)-Math.max(0,r.x))*Math.max(0,Math.min(innerHeight,r.y+r.height)-Math.max(0,r.y));}
  function current(){
    if(!ComicReaders.match(location.href)||ComicReaders.match(location.href).id!=="pixiv")throw new Error("已離開 Pixiv 閱讀頁。");
    const nodes=[...document.querySelectorAll('[id^="page-"]')].filter(n=>/^page-\d+$/.test(n.id)).map(node=>({node,rect:rect(node),index:Number(node.id.slice(5))}))
      .filter(n=>n.rect.width>=64&&n.rect.height>=64&&area(n.rect)>0&&getComputedStyle(n.node).visibility!=="hidden").sort((a,b)=>area(b.rect)-area(a.rect));
    if(!nodes.length)throw new Error("正在等待 Pixiv 漫畫頁面載入…");
    const best=nodes[0],vertical=!!best.node.querySelector('canvas')||getComputedStyle(best.node).backgroundSize==="cover",spread=!vertical&&best.rect.width<innerWidth*.6;
    const group=spread?Math.floor(best.index/2):best.index,selected=spread?nodes.filter(n=>Math.floor(n.index/2)===group).sort((a,b)=>a.index-b.index):[best];
    if(spread&&(selected.length!==2||selected.some(n=>Math.abs(n.rect.y)>3||Math.min(Math.abs(n.rect.x),Math.abs(n.rect.x-innerWidth/2))>3)))throw new Error("正在等待 Pixiv 翻頁動畫完成…");
    if(!vertical&&!spread&&Math.abs(best.rect.x)>3)throw new Error("正在等待 Pixiv 翻頁動畫完成…");
    const region=spread?{x:0,y:0,width:innerWidth,height:best.rect.height}:best.rect;
    // A fixed pixel space lets vertical pages of slightly different widths
    // share the queue. Painting back into each page rect restores its aspect.
    const canonical=vertical?document.querySelector('#page-0')?.getBoundingClientRect():region;
    const dpr=Math.min(2,devicePixelRatio||1),width=Math.round((canonical?.width||region.width)*dpr),height=Math.round((canonical?.height||region.height)*dpr);
    if(width<128||height<128||width*height>20_000_000)throw new Error("Pixiv 畫面尺寸尚未就緒。");
    const fingerprint=selected.map(({node,index})=>{
      const canvas=node.querySelector('canvas');if(!canvas)return index+":"+getComputedStyle(node).backgroundImage;
      const c=document.createElement('canvas');c.width=c.height=8;const p=c.getContext('2d');p.drawImage(canvas,0,0,8,8);return index+":"+Array.from(p.getImageData(0,0,8,8).data).join(',');
    }).join('|');
    return {nodes:selected,position:group+1,rect:region,width,height,fingerprint,layout:{mode:vertical?"vertical":spread?"spread":"single"},background:"white"};
  }
  async function frame(){
    const state=current(),copy=document.createElement('canvas');copy.width=state.width;copy.height=state.height;
    const pen=copy.getContext('2d');pen.fillStyle=state.background;pen.fillRect(0,0,copy.width,copy.height);
    for(const item of state.nodes){
      const canvas=item.node.querySelector('canvas');
      if(canvas){pen.drawImage(canvas,0,0,copy.width,copy.height);continue;}
      const style=getComputedStyle(item.node),match=style.backgroundImage.match(/^url\(["']?(.*?)["']?\)$/);
      if(!match)throw new Error("Pixiv 圖片仍在載入。");
      const url=new URL(match[1],location.href);
      if(url.protocol!=="blob:"&&!(url.protocol==="https:"&&url.hostname==="public-img-comic.pximg.net"))throw new Error("Pixiv 畫面圖片尚未就緒。");
      const bitmap=await createImageBitmap(await (await fetch(url.href)).blob());
      try{
        if(state.layout.mode==="vertical"){pen.drawImage(bitmap,0,0,copy.width,copy.height);continue;}
        const box={x:(item.rect.x-state.rect.x)*state.width/state.rect.width,y:(item.rect.y-state.rect.y)*state.height/state.rect.height,width:item.rect.width*state.width/state.rect.width,height:item.rect.height*state.height/state.rect.height};
        const align=["100%","right"].includes(style.backgroundPositionX)?"right":["0%","0px","left"].includes(style.backgroundPositionX)?"left":"center",r=ComicPixivImage.fit(bitmap.width,bitmap.height,box,align);
        pen.drawImage(bitmap,r.x,r.y,r.width,r.height);
      }finally{bitmap.close();}
    }
    const image=copy.toDataURL('image/png');copy.width=copy.height=0;
    if(image.length>16*1024*1024)throw new Error("Pixiv 畫面過大。");
    const decoded=await createImageBitmap(await (await fetch(image)).blob()),thumbnail=new OffscreenCanvas(96,64),brush=thumbnail.getContext('2d');
    try{brush.drawImage(decoded,0,0,96,64);}finally{decoded.close();}
    const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(image));
    const {nodes,...metadata}=state;
    return {...metadata,fingerprint:"png-v1",image,sample:Array.from(brush.getImageData(0,0,96,64).data),hash:Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('')};
  }
  globalThis.ComicPixivReader={current,frame};
})();

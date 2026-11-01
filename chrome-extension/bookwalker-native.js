/* Version-specific adapter to the reader's existing page loader and renderer.
   No navigation, copied decoder, credentials, or signed URLs leave this world. */
(()=>{
  "use strict";
  if(location.hostname!=="viewer.bookwalker.jp"||!/^\/03\/\d+\/viewer\.html$/.test(location.pathname))return;
  if(globalThis.ComicBookWalkerNative?.version===3){globalThis.ComicBookWalkerNative.arm();return;}
  globalThis.ComicBookWalkerNative?.stop?.();
  const cid=new URL(location.href).searchParams.get("cid"),bookRef="bookwalker:"+cid;
  const nf=globalThis.NFBR,prototype=nf?.a6G?.a5x?.prototype;
  if(!prototype||typeof prototype.e1p!=="function")return;
  let renderer=null,preparing=false,stopped=false,armTimer=null;
  const original=prototype.e1p;
  function sameBook(){return new URL(location.href).searchParams.get("cid")===cid;}
  function restore(){if(prototype.e1p===capture)prototype.e1p=original;}
  function capture(){
    if(sameBook()&&this.model?.get("a2u")&&this.a6l){renderer=this;restore();}
    return Reflect.apply(original,this,arguments);
  }
  function arm(){if(!sameBook())return false;if(renderer&&!stopped)return true;stopped=false;if(prototype.e1p!==original&&prototype.e1p!==capture)return false;prototype.e1p=capture;if(armTimer!==null)clearTimeout(armTimer);armTimer=setTimeout(restore,180000);return true;}
  arm();
  function selection(){
    if(stopped||!sameBook())throw new Error("書籍已變更，請重新開始預翻譯。");
    if(!renderer)throw new Error("尚未接上閱讀器：請正常翻一個畫面，再按翻譯；不用重新整理。");
    const model=renderer.model,content=model.get("a2u"),page=model.get("viewerPage");
    if(!content||!Array.isArray(content.r8q)||typeof content.getSpreadIndexByPage!=="function")throw new Error("此閱讀器版本的頁面結構尚未支援。");
    return {model,content,page,index:content.getSpreadIndexByPage(page)};
  }
  function status(){
    try{const s=selection();return {ready:true,version:3,model_page:s.page,spread_index:s.index,spread_count:s.content.r8q.length,
      spreads:s.content.r8q.slice(s.index,s.index+6).map(spread=>({page_index:spread.pageIndex,double:!!spread.isDoublePage,
        pages:[spread.left,spread.right].filter(p=>p&&p.index>=0).map(p=>({index:p.index,info:!!p.info}))}))};
    }catch(error){return {ready:false,version:3,reason:error.message};}
  }
  async function prepare(input){
    if(preparing)throw new Error("閱讀器預翻譯正在準備中。");
    if(input.book_ref!==bookRef||!Number.isInteger(input.count)||input.count<0||input.count>5||!Number.isInteger(input.width)||!Number.isInteger(input.height)||input.width<128||input.height<128||input.width*input.height>20_000_000)throw new Error("預翻譯參數無效。");
    const initial=selection(),controller=renderer.a6l,book=controller.a5U?.(),screen=renderer.currentScreen;
    if(!book||!screen?.leftRect||!screen.rightRect||!screen.centerRect||typeof controller.getPageCacheUrl_!=="function"||typeof controller.getPageContentUrl_!=="function"||typeof nf.a6j!=="function")throw new Error("此閱讀器版本的圖片介面尚未支援。");
    const alignment=nf.a6G.a5x.Alignment;
    const dummy=[document.createElement("canvas"),document.createElement("canvas")],temporary=[document.createElement("canvas")];
    const isolated=Object.create(renderer);
    // Native rendering uses scratch canvases. Give it private scratch canvases so
    // neither the visible page nor the reader's in-progress rendering is touched.
    isolated.$=selector=>{if(selector==="canvas.dummy")return globalThis.$(dummy);if(selector==="canvas.temporary")return globalThis.$(temporary);throw new Error("閱讀器使用了未支援的畫布介面。");};
    const owned=[],loaders=[];
    const alive=()=>{if(selection().page!==initial.page)throw new Error("閱讀位置已改變，請在目前頁面重新開始預翻譯。");};
    const load=async page=>{
      if(!page.info||typeof page.n4q!=="function")throw new Error("閱讀器尚未提供後續頁的版面資料，請先使用目前畫面翻譯。");
      page.n4q(book);
      const key=controller.getPageCacheUrl_(book,page),cached=nf.a6G.a6L.E9a.get(key),existing=cached?.bitmap||cached;
      if(existing?.complete&&existing.width>0&&existing.height>0)return existing;
      const image=new Image(),loader=new nf.a6j();owned.push(image);loaders.push(loader);
      loader.load(image,controller.getPageContentUrl_(book,page));
      const deadline=Date.now()+20000;
      while(Date.now()<deadline){alive();const bitmap=image.bitmap||image;if(bitmap.complete&&bitmap.width>0&&bitmap.height>0){if(bitmap.width*bitmap.height>20_000_000)throw new Error("BookWalker 圖片尺寸過大。");return bitmap;}await new Promise(resolve=>setTimeout(resolve,50));}
      throw new Error("閱讀器載入後續圖片逾時，請稍後重試。");
    };
    preparing=true;
    try{
      const frames=[],positions=[],known=new Set((Array.isArray(input.known_positions)?input.known_positions:[]).slice(0,10)),spreads=initial.content.r8q.slice(initial.index,initial.index+input.count+1),first=spreads[0];
      if(!Number.isInteger(first?.pageIndex))throw new Error("無法對應閱讀器頁序。");
      for(const spread of spreads){
        const position=input.position+spread.pageIndex-first.pageIndex;
        if(!Number.isInteger(position)||position<input.position||positions.length&&position<=positions.at(-1))throw new Error("閱讀器頁序不連續，已停止預翻譯。");
        positions.push(position);
        if(position!==input.position&&known.has(position))continue;
        alive();const pages=[spread.left,spread.right].filter(p=>p&&p.index>=0);
        if(!pages.length||pages.length>2)throw new Error("此頁面格式尚未支援預翻譯。");
        const images=await Promise.all(pages.map(load));alive();
        const canvas=document.createElement("canvas");canvas.width=input.width;canvas.height=input.height;
        const pen=canvas.getContext("2d");pen.fillStyle=input.background;pen.fillRect(0,0,canvas.width,canvas.height);
        const sourceRegions=[];
        for(let i=0;i<pages.length;i++){
          const left=pages[i]===spread.left,rect=spread.isDoublePage?(left?screen.leftRect:screen.rightRect):screen.centerRect;
          const x=Number(rect.x),y=Number(rect.y),w=Number(rect.width),h=Number(rect.height);
          if(![x,y,w,h].every(Number.isFinite)||w<=0||h<=0)throw new Error("閱讀器原始頁面範圍無效。");
          const clip=(n,size)=>Math.max(0,Math.min(1,n/size));
          const bbox=[clip(x,input.width),clip(y,input.height),clip(x+w,input.width),clip(y+h,input.height)];
          if(bbox[0]>=bbox[2]||bbox[1]>=bbox[3])throw new Error("閱讀器原始頁面超出畫面。");
          sourceRegions.push({index:pages[i].index,bbox});
          Reflect.apply(original,isolated,[canvas,pages[i],images[i],{...rect},spread.isDoublePage?(left?alignment.RIGHT:alignment.LEFT):alignment.CENTER]);
        }
        const image=canvas.toDataURL("image/png");if(image.length>16*1024*1024)throw new Error("預翻譯畫面過大。");
        frames.push({position,width:input.width,height:input.height,image,source_pages:pages.map(p=>p.index),page_identity:{book_ref:bookRef,pages:sourceRegions}});
      }
      return {book_ref:bookRef,frames,positions,native:status()};
    }finally{
      for(const loader of loaders)loader.e8Q?.();
      for(const image of owned){image.bitmap?.close();image.src="";}
      for(const canvas of [...dummy,...temporary]){canvas.width=canvas.height=0;}
      preparing=false;
    }
  }
  globalThis.ComicBookWalkerNative={version:3,status,prepare,arm,stop(){stopped=true;renderer=null;restore();}};
})();

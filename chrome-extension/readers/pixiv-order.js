/* Public chapter order only. No images, access tokens or reader navigation. */
(()=>{
  "use strict";
  if(globalThis.ComicPixivOrder?.version===2)return;
  const cache=new Map(),pending=new Map(),valid=id=>/^[1-9]\d{0,11}$/.test(String(id)),LIMIT=4096;
  async function load(workId,episodeId){
    workId=String(workId||"");episodeId=String(episodeId||"");
    if(!valid(workId)||!valid(episodeId))return null;
    const fallback={work_ref:"pixiv:work:"+workId,source:"pixiv-id-fallback-v1",chapters:[]};
    const prior=cache.get(workId);
    if(prior&&(Date.now()<prior.retryAt||Date.now()-prior.at<600000&&prior.value.chapters.includes(episodeId)))return prior.value;
    if(pending.has(workId))return pending.get(workId);
    const task=(async()=>{
      try{
        const chapters=[],titles={},seen=new Set(),pages=new Set();let page=1;
        do{
          if(pages.has(page)||pages.size>=50)throw new Error("Invalid chapter pagination");pages.add(page);
          const response=await fetch(`/api/app/works/${workId}/episodes/v2?order=asc&page=${page}`,{
            headers:{"X-Requested-With":"pixivcomic"},credentials:"same-origin",signal:AbortSignal.timeout(8000)});
          if(!response.ok)throw new Error("Chapter list unavailable");
          const data=(await response.json()).data;
          if(!Array.isArray(data?.episodes))throw new Error("Invalid chapter list");
          for(const item of data.episodes){
            // Locked/expired episodes still establish order when their ID is
            // supplied. Anonymous publication notices establish no identity.
            const id=item?.episode?.id;if(id==null)continue;
            if(!valid(id)||seen.has(String(id))||chapters.length>=LIMIT)throw new Error("Invalid chapter identity");
            seen.add(String(id));chapters.push(String(id));
            const name=item.episode;
            titles[String(id)]={numbering_title:typeof name.numbering_title==="string"?name.numbering_title.trim().slice(0,120):"",
              sub_title:typeof name.sub_title==="string"?name.sub_title.trim().slice(0,300):""};
          }
          const next=data.next_page_number??data.nextPageNumber??null;
          if(next!=null&&(!Number.isInteger(next)||next<=page))throw new Error("Invalid next page");
          page=next;
        }while(page!=null);
        if(!chapters.length)throw new Error("Empty chapter list");
        const value={work_ref:fallback.work_ref,source:"pixiv-official-list-v1",chapters,titles};
        cache.set(workId,{at:Date.now(),value});while(cache.size>8)cache.delete(cache.keys().next().value);
        return value;
      }catch{
        // A temporary failure retains the last official order. Comparisons
        // involving chapters missing from that list use the scoped ID fallback.
        const value=prior?.value||fallback;
        cache.set(workId,{at:prior?.at||0,retryAt:Date.now()+30000,value});
        return value;
      }
    })();
    pending.set(workId,task);try{return await task;}finally{pending.delete(workId);}
  }
  globalThis.ComicPixivOrder={load,version:2};
})();

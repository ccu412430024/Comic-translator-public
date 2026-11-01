/* Shared catalogue labels and platform scope. Unbound manual works can be claimed once. */
(()=>{
  const labels={bookwalker:"BookWalker",pixiv:"pixivコミック",unassigned:"未指定平台"};
  function fromRef(ref){
    if(typeof ref!=="string")return "";
    if(/^bookwalker:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(ref)||ref.startsWith("title:")&&ref.length>6)return "bookwalker";
    return /^pixiv:(?:work|episode):[1-9][0-9]{0,11}$/.test(ref)?"pixiv":"";
  }
  function ids(work){
    const origins=Array.isArray(work.platforms)?work.platforms:(work.books||[]).map(b=>fromRef(b.ref));
    return Object.keys(labels).filter(p=>p!=="unassigned"&&origins.includes(p));
  }
  function allows(work,ref){const platform=fromRef(ref),origins=ids(work);return !platform||!origins.length||origins.length===1&&origins[0]===platform;}
  function matches(work,platform){const origins=ids(work);return !platform||platform==="unassigned"&&!origins.length||origins.includes(platform);}
  function names(work){const origins=ids(work);return (origins.length?origins:["unassigned"]).map(p=>labels[p]).join("／");}
  globalThis.ComicLibraryPlatforms={labels,fromRef,ids,allows,matches,names};
})();

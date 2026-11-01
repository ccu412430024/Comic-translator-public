/* Stable interface consumed by the shared content watcher. */
(()=>{
  function adapter(){const descriptor=ComicReaders.match(location.href),reader=descriptor&&globalThis[descriptor.readerGlobal];
    if(!reader)throw new Error("目前頁面沒有可用的閱讀器適配。");return {descriptor,reader};}
  function current(){return adapter().reader.current();}
  async function frame(){const {descriptor,reader}=adapter();return {...await reader.frame(),adapter:descriptor.id};}
  globalThis.ComicReader={current,frame,reference:()=>ComicReaders.reference(location.href)};
})();

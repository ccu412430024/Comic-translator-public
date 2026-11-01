/* Platform descriptors contain selectors and loading policy; queues stay shared. */
(()=>{
  "use strict";
  if(globalThis.ComicReaders)return;
  const platforms=new Map();
  function register(adapter){
    if(!/^[a-z][a-z0-9-]*$/.test(adapter.id)||typeof adapter.supported!=="function"||typeof adapter.context!=="function")throw new Error("Invalid reader adapter");
    platforms.set(adapter.id,Object.freeze(adapter));
  }
  function match(url){return [...platforms.values()].find(adapter=>adapter.supported(url))||null;}
  function context(doc,url,title,author=""){
    for(const adapter of platforms.values()){const result=adapter.context(doc,url,title,author);if(result)return result;}
    return {title:"",author:"",ref:""};
  }
  function reference(url){return match(url)?.reference(url)||"";}
  globalThis.ComicReaders={register,match,context,reference,get:id=>platforms.get(id)||null,
    matches:(url,ref)=>!!ref&&reference(url)===ref,list:()=>[...platforms.values()].map(({id,label})=>({id,label}))};
})();

/* Only public catalogue metadata crosses the reader boundary. */
(()=>{
  function fromMetadata(title,url,author=""){return globalThis.ComicReaders?.context(null,url,title,author)||{title:"",author:"",ref:""};}
  function read(doc,url){return globalThis.ComicReaders?.context(doc,url,doc.querySelector('meta[property="og:title"]')?.content||doc.title,
    doc.querySelector('meta[name="author"]')?.content||"")||{title:"",author:"",ref:""};}
  globalThis.ComicBookContext={fromMetadata,read};
})();

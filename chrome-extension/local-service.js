/* Fixed-purpose native bridge. No page data or credentials are passed to it. */
(()=>{
  const SERVER="http://127.0.0.1:8765",HOST="com.comic_translator.reader";
  const failures={origin_denied:"本機啟動橋接不允許目前的插件，請重新執行安裝程式。",
    installation_missing:"找不到本機服務，請重新執行 scripts/install-reader-autostart.ps1。",
    port_occupied:"本機連接埠 8765 已被其他程式占用。",launch_denied:"Windows 未能啟動本機服務，可先執行 scripts/start-reader.ps1。",
    server_exited:"本機服務啟動失敗，請檢查 Python 環境與資料庫。",startup_timeout:"本機服務啟動逾時，請按重試。",
    startup_busy:"本機服務仍在啟動，請稍後重試。"};
  function create({runtime,fetch:request=globalThis.fetch,onProgress=()=>{}}){
    let starting=null;
    async function probe(){
      let response;
      try{response=await request(SERVER+"/api/health",{signal:AbortSignal.timeout(1800)});}
      catch{return null;}
      if(!response.ok)throw new Error(failures.port_occupied);
      let value;try{value=await response.json();}catch{throw new Error(failures.port_occupied);}
      if(value?.app!=="comic-reader"||value.ready!==true)throw new Error(failures.port_occupied);
      return value;
    }
    async function start(){
      const ready=await probe();if(ready)return ready;
      onProgress("正在啟動本機服務與作品資料庫…");
      let result;
      try{result=await runtime.sendNativeMessage(HOST,{action:"ensure_started"});}
      catch{throw new Error("本機啟動橋接尚未安裝或未獲允許。請執行 scripts/install-reader-autostart.ps1，並重新載入插件。");}
      if(result?.ok!==true)throw new Error(failures[result?.reason]||"本機服務未能啟動，請按重試或執行 scripts/start-reader.ps1。");
      const checked=await probe();if(!checked)throw new Error("本機服務尚未就緒，請按重試。");
      return checked;
    }
    return {probe,ensure(){
      if(!starting)starting=start().finally(()=>{starting=null;});
      return starting;
    }};
  }
  function target(search){
    const params=new URLSearchParams(search),url=new URL(params.get("target")==="library"?"/library":"/",SERVER);
    const work=params.get("work");if(url.pathname==="/library"&&/^[0-9a-f]{24}$/.test(work||""))url.searchParams.set("work",work);
    return url.href;
  }
  globalThis.ComicLocalService={create,target,SERVER,HOST};
})();

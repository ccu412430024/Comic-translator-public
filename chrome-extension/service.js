"use strict";
const status=document.getElementById("status"),retry=document.getElementById("retry"),help=document.getElementById("help");
let opening=false;
async function openService(){
  if(opening)return;opening=true;retry.hidden=help.hidden=true;
  status.textContent="正在啟動本機服務與作品資料庫…";
  try{
    const result=await chrome.runtime.sendMessage({type:"comic:ensure-service"});
    if(result?.ok!==true)throw new Error(result?.error||"本機服務未能啟動。");
    location.replace(ComicLocalService.target(location.search));
  }catch(error){status.textContent=error.message;retry.hidden=help.hidden=false;}
  finally{opening=false;}
}
retry.addEventListener("click",openService);
openService();

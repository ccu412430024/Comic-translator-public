// Browser DOM integration test only. No screenshots, keys or external requests.
let receiver,ticket;
window.chrome=window.chrome||{};
window.chrome.runtime={id:'overlay-test',onMessage:{addListener(callback){receiver=callback;}},async sendMessage(){return {ok:true};}};
function message(value){return new Promise(resolve=>receiver(value,{id:'overlay-test'},resolve));}
document.getElementById('prepare').onclick=async()=>{
  ticket=await message({type:'comic:prepare',select:false});
  await message({type:'comic:status',token:ticket.token,text:'正在排版測試'});
  document.getElementById('test-status').textContent='擷取準備完成';
};
document.getElementById('result').onclick=async()=>{
  ticket=await message({type:'comic:prepare',select:false});
  await message({type:'comic:check',token:ticket.token});
  let response=await fetch('/test/translated.png');if(!response.ok)response=await fetch('/demo/synthetic-02.png');const blob=await response.blob();
  const image=await new Promise(resolve=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.readAsDataURL(blob);});
  const b=document.getElementById('source-page').getBoundingClientRect();
  const result=await message({type:'comic:result',token:ticket.token,rect:{x:b.left,y:b.top,width:b.width,height:b.height},image,metrics:{mode_name:'RT-DETR 分塊版',translated_blocks:8}});
  document.getElementById('test-status').textContent=result.current?'覆蓋完成':'已取消';
};
document.getElementById('ordinary').onclick=()=>{document.getElementById('test-status').textContent=document.querySelector('[data-comic-reader="overlay"]')?'一般網頁按鈕已點擊，譯圖與工具列保留':'覆蓋已消失';};
document.getElementById('turn').onclick=()=>{document.getElementById('source-page').src='/demo/synthetic-01.png';document.getElementById('test-status').textContent='測試圖已切換，等待舊覆蓋移除';};
document.getElementById('inspect').onclick=()=>{document.getElementById('test-status').textContent=document.querySelector('[data-comic-reader="overlay"]')?'譯圖與工具列仍存在':'舊譯圖已移除';};

// Local DOM test wrapper only; no extension installation or real capture.
const realFetch=window.fetch.bind(window);
window.fetch=(url,options)=>{
  const target=typeof url==='string'?url.replace('http://127.0.0.1:8765',location.origin):url;
  // UI-only checks must not create or bind a synthetic work in the live library.
  if(typeof target==='string'&&target.endsWith('/api/library/books'))return Promise.resolve(new Response('{}',{headers:{'Content-Type':'application/json'}}));
  return realFetch(target,options);
};
let preferences=JSON.parse(sessionStorage.getItem('popup-test-preferences')||'{}');
window.chrome=window.chrome||{};
window.chrome.storage={local:{async get(){return preferences;},async set(values){Object.assign(preferences,values);sessionStorage.setItem('popup-test-preferences',JSON.stringify(preferences));}},session:{async get(){return {};},async remove(){}}};
window.chrome.tabs={async query(){return [{id:7,title:'BookWalker 預讀介面測試',url:'https://viewer.bookwalker.jp/03/30/viewer.html?cid=00000000-0000-0000-0000-000000000000'}];}};
window.chrome.scripting={async executeScript(){return [];}};
window.chrome.permissions={async request(){return true;}};
window.chrome.runtime={async sendMessage(value){if(value.type==='comic:ensure-service')return {ok:true};if(value.type==='comic:release-gpu'){const response=await realFetch('/api/gpu/release',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});return response.json();}return {error:'介面測試收到選項：'+value.cleaning+' / '+value.layout+' / '+(value.model||value.translator)+' / 預翻 '+(value.prefetch_count||0)+' 個畫面 / 同時 '+(value.prefetch_parallelism||1)+' 個畫面；未呼叫翻譯。'};}};

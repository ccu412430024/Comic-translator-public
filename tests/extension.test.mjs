import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
const root=new URL('../chrome-extension/',import.meta.url);
for(const file of ['readers/registry.js','readers/bookwalker.js','readers/pixiv.js'])new Function(await readFile(new URL('../chrome-extension/'+file,import.meta.url),'utf8'))();

new Function(await readFile(new URL('gemini-connection.js',root),'utf8'))();
const utils=await readFile(new URL('capture-utils.js',root),'utf8');
const {cropGeometry,meanDifference}=await import('data:text/javascript;base64,'+Buffer.from(utils).toString('base64'));
const source=(await readFile(new URL('background.js',root),'utf8')).replace(/^import .*?;\r?\n/gm,'');

test('capture coordinates follow display scaling and clip at screenshot edges',()=>{
  assert.deepEqual(cropGeometry(2400,1600,{width:1200,height:800},{x:100,y:50,width:500,height:700}),{left:200,top:100,width:1000,height:1400});
  assert.deepEqual(cropGeometry(1200,800,{width:1200,height:800},{x:1100,y:700,width:200,height:200}),{left:1100,top:700,width:100,height:100});
});
test('screenshot comparison ignores alpha and detects changed artwork',()=>{
  assert.equal(meanDifference([20,30,40,0],[20,30,40,255]),0);
  assert.equal(meanDifference([0,0,0,255],[255,255,255,255]),255);
});

function harness({changed=false,stale=false,inactive=false,captureError=false,captureWait=null,onCapture=()=>{},connection={}}={}){
  const messages=[],requests=[],payloads=[];let captures=0;
  const chrome={runtime:{id:'test',onMessage:{addListener(){}}},storage:{session:{async set(){},async get(){return {};},async remove(){}}},
    scripting:{async executeScript(){}},tabs:{async query(){return [{id:inactive?8:7}];},
      async captureVisibleTab(){onCapture();if(captureWait)await captureWait;if(captureError)throw new Error('capture unavailable');captures++;return 'data:image/png;base64,'+(changed&&captures>1?'CHANGED':'ORIGINAL');},
      async sendMessage(id,message){messages.push(message);if(message.type==='comic:prepare')return {token:1,viewport:{width:100,height:100},rect:{x:0,y:0,width:100,height:100}};return {current:!stale};}}};
  const png={async arrayBuffer(){return new Uint8Array([1,2,3]).buffer;}};
  const fetch=async(url,options)=>{
    requests.push(url);
    if(url.startsWith('data:'))return {async blob(){return {tag:url.endsWith('CHANGED')?255:0};}};
    if(url.endsWith('/api/settings'))return {ok:true,async json(){return {has_gemini_key:true,model:'gemini-3.5-flash-lite',gpu_epoch:7,...connection};}};
    if(url.endsWith('/api/jobs')){payloads.push(JSON.parse(options.body));return {ok:true,async json(){return {id:'job',state:'done',image_url:'/image',metrics:{}};}};}
    if(url.endsWith('/image'))return {ok:true,async blob(){return png;}};
    return {ok:true,async json(){return {};}};
  };
  class Canvas{
    constructor(){this.tag=0;}
    getContext(){return {drawImage:(image)=>this.tag=image.tag,getImageData:()=>{const data=new Uint8Array(64*64*4);for(let i=0;i<data.length;i+=4){data[i]=data[i+1]=data[i+2]=this.tag;data[i+3]=255;}return {data};}};}
    async convertToBlob(){return png;}
  }
  const factory=new Function('chrome','fetch','createImageBitmap','OffscreenCanvas','btoa','setTimeout','cropGeometry','meanDifference',source+'\nreturn {run,verifyPage,releaseGpu};');
  const api=factory(chrome,fetch,async blob=>({tag:blob.tag,width:100,height:100,close(){}}),Canvas,x=>Buffer.from(x,'binary').toString('base64'),resolve=>{resolve();},cropGeometry,meanDifference);
  return {api,messages,requests,payloads,get captures(){return captures;}};
}

test('selected Gemini model is passed with the captured page instead of changing global settings',async()=>{
  const h=harness();await h.api.run({id:7,windowId:1},{mode:'standard',translator:'gemini',model:'gemini-3.1-pro-preview'});
  assert.equal(h.payloads[0].model,'gemini-3.1-pro-preview');
  assert.equal(h.payloads[0].gpu_epoch,7);
  assert.equal(h.requests.filter(url=>url.endsWith('/api/settings')).length,1);
  const fallback=harness();await fallback.api.run({id:7,windowId:1},{mode:'standard',translator:'gemini'});
  assert.equal(fallback.payloads[0].model,'gemini-3.5-flash-lite');
  const google=harness();await google.api.run({id:7,windowId:1},{mode:'standard',translator:'google-web',model:'gemini-3.1-pro-preview'});
  assert.equal(google.payloads[0].model,undefined);
});

test('OCR choice travels with screenshot jobs and omission preserves Manga-OCR',async()=>{
  const chosen=harness();await chosen.api.run({id:7,windowId:1},{mode:'standard',translator:'google-web',ocr_model:'nar'});
  assert.equal(chosen.payloads[0].ocr_model,'nar');
  const omitted=harness();await omitted.api.run({id:7,windowId:1},{mode:'paddle',translator:'google-web'});
  assert.equal(omitted.payloads[0].ocr_model,'base');
});

test('paid acceleration choices travel with Gemini captures and stay off for Google',async()=>{
  const h=harness();await h.api.run({id:7,windowId:1},{mode:'standard',translator:'gemini',gemini_cache:'explicit',gemini_tier:'priority'});
  assert.equal(h.payloads[0].gemini_cache,'explicit');assert.equal(h.payloads[0].gemini_tier,'priority');
  const google=harness();await google.api.run({id:7,windowId:1},{mode:'standard',translator:'google-web',gemini_cache:'explicit',gemini_tier:'priority'});
  assert.equal(google.payloads[0].gemini_cache,'off');assert.equal(google.payloads[0].gemini_tier,'standard');
});
test('successful browser capture returns the translated overlay',async()=>{
  const h=harness();await h.api.run({id:7,windowId:1},{mode:'paddle',translator:'gemini'});
  assert.equal(h.captures,2);
  assert.equal(h.messages.filter(m=>m.type==='comic:result').length,1);
});

test('GPU release cancels a screenshot still being captured before it can upload a job',async()=>{
  let entered,finish;
  const started=new Promise(resolve=>entered=resolve),wait=new Promise(resolve=>finish=resolve);
  const h=harness({captureWait:wait,onCapture:entered});
  const translating=h.api.run({id:7,windowId:1},{mode:'standard',translator:'gemini'});
  await started;await h.api.releaseGpu();finish();await translating;
  assert.ok(h.requests.some(url=>url.endsWith('/api/gpu/release')));
  assert.equal(h.payloads.length,0);assert.ok(!h.messages.some(m=>m.type==='comic:result'));
});

test('selected work and reading segment travel with the page and Google does not use them',async()=>{
  const h=harness();await h.api.run({id:7,windowId:1},{mode:'standard',translator:'gemini',work_id:'work-a',segment_id:'volume-2',force_translation:true});
  assert.equal(h.payloads[0].work_id,'work-a');assert.equal(h.payloads[0].segment_id,'volume-2');assert.equal(h.payloads[0].force_translation,true);
  const google=harness();await google.api.run({id:7,windowId:1},{mode:'standard',translator:'google-web',work_id:'work-a',segment_id:'volume-2'});
  assert.equal(google.payloads[0].work_id,'');assert.equal(google.payloads[0].segment_id,'');
});

test('title catalogue stays separate from opt-in translation memory',async()=>{
  const h=harness();await h.api.run({id:7,windowId:1},{mode:'standard',translator:'google-web',catalogue_id:'catalogue-a',catalogue_epoch:'epoch-a'});
  assert.equal(h.payloads[0].catalogue_id,'catalogue-a');assert.equal(h.payloads[0].catalogue_epoch,'epoch-a');
  assert.equal(h.payloads[0].work_id,'');assert.equal(h.payloads[0].segment_id,'');
});
test('changed canvas page is rejected before the translated overlay is shown',async()=>{
  const h=harness({changed:true});await h.api.run({id:7,windowId:1},{mode:'tiled',translator:'gemini'});
  assert.equal(h.messages.filter(m=>m.type==='comic:result').length,0);
  assert.ok(h.messages.find(m=>m.type==='comic:error'&&m.text.includes('畫面已改變')));
});
test('navigation invalidates the capture before uploading the page',async()=>{
  const h=harness({stale:true});await h.api.run({id:7,windowId:1},{mode:'standard',translator:'gemini'});
  assert.equal(h.requests.filter(url=>url.endsWith('/api/jobs')).length,0);
});
function watchMessage(){const fingerprint=Array(64*64*4).fill(0);for(let i=3;i<fingerprint.length;i+=4)fingerprint[i]=255;return {token:1,watch:{viewport:{width:100,height:100},rect:{x:0,y:0,width:100,height:100},fingerprint}};}
test('unchanged protected canvas is restored after verification without another translation request',async()=>{
  const h=harness();await h.api.verifyPage({id:7,windowId:1},watchMessage());
  assert.ok(h.messages.some(m=>m.type==='comic:restore'));
  assert.ok(!h.messages.some(m=>m.type==='comic:invalidate'));
  assert.ok(!h.requests.some(url=>url.endsWith('/api/jobs')));
});
test('protected canvas is invalidated only when a fresh capture actually changes',async()=>{
  const h=harness({changed:true});await h.api.verifyPage({id:7,windowId:1},watchMessage());
  assert.ok(!h.messages.some(m=>m.type==='comic:invalidate'));
  await h.api.verifyPage({id:7,windowId:1},watchMessage());
  assert.equal(h.messages.filter(m=>m.type==='comic:invalidate').length,1);
});
test('capture failures and inactive tabs preserve the existing overlay',async()=>{
  for(const options of [{inactive:true},{captureError:true}]){
    const h=harness(options);await h.api.verifyPage({id:7,windowId:1},watchMessage());
    assert.ok(!h.messages.some(m=>m.type==='comic:invalidate'));
    assert.ok(h.messages.some(m=>m.type==='comic:restore'));
  }
});


test('Vertex captures send the selected project and region without requiring a Developer key',async()=>{
  const h=harness({connection:{gemini_backend:'vertex',vertex_project:'test-project',vertex_location:'global',gemini_ready:true,has_gemini_key:false}});
  await h.api.run({id:7,windowId:1},{mode:'standard',translator:'gemini'});
  assert.equal(h.payloads.length,1);assert.equal(h.payloads[0].gemini_backend,'vertex');assert.equal(h.payloads[0].vertex_project,'test-project');
  assert.equal(h.payloads[0].vertex_location,'global');
  const google=harness({connection:{gemini_backend:'vertex',vertex_project:'test-project',gemini_ready:false}});
  await google.api.run({id:7,windowId:1},{mode:'standard',translator:'google-web'});
  assert.equal(google.payloads[0].gemini_backend,undefined);
});

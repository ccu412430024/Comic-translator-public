import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const root=new URL('../chrome-extension/',import.meta.url);
const pageSource=await readFile(new URL('page-state.js',root),'utf8');
const contentSource=await readFile(new URL('content.js',root),'utf8');

function harness({kind='IMG',tainted=false}={}){
  const docEvents=new Map(),windowEvents=new Map(),timers=new Map(),intervals=new Map(),sent=[];
  let receiver,timer=0;
  class Element{
    constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.style={};this.dataset={};this.isConnected=false;this.textContent='';this.width=300;this.height=400;this.color=20;}
    append(...nodes){for(const node of nodes){node.parent=this;node.isConnected=true;this.children.push(node);}}
    prepend(node){node.parent=this;node.isConnected=true;this.children.unshift(node);}
    remove(){this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(node=>node!==this);}
    attachShadow(){this.shadow=new Element('shadow');return this.shadow;}
    addEventListener(){}
    insertBefore(node,before){node.parent=this;this.children.splice(this.children.indexOf(before),0,node);}
    get lastChild(){return this.children.at(-1);}
    getBoundingClientRect(){return {left:500,top:130,width:this.width,height:this.height};}
    getAttribute(){return null;}
    closest(){return ['BUTTON','INPUT','TEXTAREA','SELECT'].includes(this.tagName)?this:null;}
    getContext(){let source;return {drawImage(node){source=node;},getImageData(){if(tainted&&source?.tagName==='CANVAS')throw new Error('SecurityError');const data=new Uint8ClampedArray(32*32*4);for(let i=0;i<data.length;i+=4){data[i]=data[i+1]=data[i+2]=source?.color||0;data[i+3]=255;}return {data};}};}
  }
  const media=new Element(kind);media.isConnected=true;media.src=media.currentSrc='page-one';
  const html=new Element('html');html.isConnected=true;
  const document={hidden:false,documentElement:html,fullscreenElement:null,querySelectorAll(){return [media];},
    createElement(tag){return new Element(tag);},addEventListener(type,handler){docEvents.set(type,handler);},removeEventListener(){}};
  const context=vm.createContext({document,innerWidth:1200,innerHeight:800,scrollX:0,scrollY:0,location:{href:'https://reader.test/book'},
    window:{addEventListener(type,handler){windowEvents.set(type,handler);}},getComputedStyle(){return {display:'block',visibility:'visible',opacity:'1'};},
    CSSStyleSheet:class{replaceSync(){}},requestAnimationFrame(resolve){resolve();},Blob,Uint8Array,Uint8ClampedArray,
    atob:value=>Buffer.from(value,'base64').toString('binary'),createImageBitmap:async()=>({width:300,height:400,close(){}}),
    setInterval(fn){intervals.set(++timer,fn);return timer;},clearInterval(id){intervals.delete(id);},
    setTimeout(fn){timers.set(++timer,fn);return timer;},clearTimeout(id){timers.delete(id);},
    chrome:{runtime:{id:'test',onMessage:{addListener(callback){receiver=callback;}},async sendMessage(message){sent.push(message);return {ok:true};}}}});
  vm.runInContext(pageSource,context);vm.runInContext(contentSource,context);
  const send=message=>new Promise(resolve=>receiver(message,{id:'test'},resolve));
  const host=()=>html.children.find(node=>node.isConnected);
  const event=(target,extra={})=>({target,clientX:550,clientY:200,composedPath:()=>[target],...extra});
  async function rendered(){const ticket=await send({type:'comic:prepare',select:false});await send({type:'comic:result',token:ticket.token,rect:ticket.rect,image:'data:image/png;base64,AQID',metrics:{mode_name:'RT-DETR',translated_blocks:7},watch:{fingerprint:[0,0,0,255],rect:ticket.rect,viewport:ticket.viewport}});return ticket;}
  return {media,context,document,sent,host,send,rendered,Element,
    click(target){docEvents.get('pointerdown')?.(event(target));docEvents.get('click')?.(event(target));},
    key(target,key){docEvents.get('keydown')?.(event(target,{key}));},
    tick(){for(const fn of [...intervals.values()])fn();},
    visibility(hidden){document.hidden=hidden;docEvents.get('visibilitychange')?.();},
    scroll(){windowEvents.get('scroll')?.();},
    async delayed(){for(const [id,fn] of [...timers]){timers.delete(id);await fn();}await Promise.resolve();}};
}
test('clicking an unrelated website button preserves the translated canvas and toolbar',async()=>{
  const h=harness();const ticket=await h.rendered();const original=h.host();
  const button=new h.Element('button');button.textContent='開啟設定';h.click(button);h.tick();
  assert.equal(h.host(),original);assert.ok(original.shadow.children.some(node=>node.tagName==='CANVAS'));
  assert.equal((await h.send({type:'comic:status',token:ticket.token,text:'仍在原頁'})).current,true);
  assert.equal(h.sent.length,0);
});
test('switching tabs preserves the translation when the manga stays unchanged',async()=>{
  const h=harness();await h.rendered();const original=h.host();h.visibility(true);h.tick();h.visibility(false);
  assert.equal(h.host(),original);
});
test('image page changes clear the old translation',async()=>{
  const h=harness();const ticket=await h.rendered();h.media.src=h.media.currentSrc='page-two';h.tick();
  assert.equal(h.host(),undefined);assert.equal((await h.send({type:'comic:status',token:ticket.token,text:'old'})).current,false);
});
test('readable canvas page changes clear the old translation without an outside-click rule',async()=>{
  const h=harness({kind:'CANVAS'});await h.rendered();const button=new h.Element('button');button.textContent='設定';h.click(button);h.tick();assert.ok(h.host());
  h.media.color=200;h.tick();assert.equal(h.host(),undefined);
});
test('nested UI scrolling is retained while actual viewport scrolling invalidates alignment',async()=>{
  const h=harness();await h.rendered();h.scroll();assert.ok(h.host());h.context.scrollY=100;h.scroll();assert.equal(h.host(),undefined);
});
test('navigation keys alone and editing input values do not dismiss an unchanged page',async()=>{
  const h=harness();await h.rendered();h.key(new h.Element('input'),'ArrowLeft');h.key(new h.Element('body'),'ArrowRight');h.tick();assert.ok(h.host());
});
test('tainted canvas verifies navigation but skips unrelated website buttons',async()=>{
  const h=harness({kind:'CANVAS',tainted:true});await h.rendered();
  const button=new h.Element('button');button.textContent='開啟設定';h.click(button);await h.delayed();assert.equal(h.sent.length,0);assert.ok(h.host());
  button.textContent='下一頁';h.click(button);await h.delayed();assert.equal(h.sent.length,3);assert.ok(h.sent.every(m=>m.type==='comic:verify'));assert.ok(h.host());
});
test('temporary screenshot checks restore the overlay and stale invalidation cannot close a new page',async()=>{
  const h=harness();const ticket=await h.rendered();await h.send({type:'comic:check',token:ticket.token});assert.equal(h.host().style.visibility,'hidden');
  await h.send({type:'comic:restore',token:ticket.token});assert.equal(h.host().style.visibility,'visible');
  await h.send({type:'comic:invalidate',token:ticket.token-1});assert.ok(h.host());await h.send({type:'comic:invalidate',token:ticket.token});assert.equal(h.host(),undefined);
});
test('a small selected bubble still tracks the containing page rather than losing page-change detection',()=>{
  const h=harness();const watch=new h.context.ComicReaderPageWatch({x:550,y:200,width:32,height:32});
  assert.equal(watch.items.length,1);assert.equal(watch.captureRect.width,300);assert.equal(watch.captureRect.height,400);assert.equal(watch.changed(),false);
  h.media.src=h.media.currentSrc='page-two';assert.equal(watch.changed(),true);
});

(()=>{
  "use strict";
  if(globalThis.ComicReaderPageWatch)return;
  function difference(a,b){
    if(!a||!b||a.length!==b.length)return Infinity;
    let total=0;for(let i=0;i<a.length;i+=4)total+=Math.abs(a[i]-b[i])+Math.abs(a[i+1]-b[i+1])+Math.abs(a[i+2]-b[i+2]);
    return total/(a.length/4*3);
  }
  function box(node){const b=node.getBoundingClientRect();return {x:b.left,y:b.top,width:b.width,height:b.height};}
  function intersection(a,b){const x=Math.max(a.x,b.x),y=Math.max(a.y,b.y);return {x,y,width:Math.max(0,Math.min(a.x+a.width,b.x+b.width)-x),height:Math.max(0,Math.min(a.y+a.height,b.y+b.height)-y)};}
  function visible(node){const style=getComputedStyle(node);return node.isConnected&&style.display!=="none"&&style.visibility!=="hidden"&&Number(style.opacity)!==0;}
  function sample(node){
    if(node.tagName!=="CANVAS")return null;
    try{const probe=document.createElement("canvas");probe.width=probe.height=32;const pen=probe.getContext("2d");pen.drawImage(node,0,0,32,32);return pen.getImageData(0,0,32,32).data;}
    catch{return null;} // Cross-origin / protected canvas: use tab capture on navigation input.
  }
  class PageWatch{
    constructor(rect){
      this.rect=rect;
      this.items=Array.from(document.querySelectorAll("img,canvas")).filter(visible).map(node=>{
        const position=box(node),part=intersection(position,rect);
        const watchPart=intersection(position,{x:0,y:0,width:innerWidth,height:innerHeight});
        return {node,position,part,watchPart,source:node.currentSrc||node.src||"",width:node.width,height:node.height,pixels:sample(node)};
      }).filter(item=>Math.min(item.position.width,item.position.height)>=64&&item.position.width*item.position.height>=10000&&item.part.width*item.part.height>=Math.max(256,rect.width*rect.height*.03))
        .sort((a,b)=>b.part.width*b.part.height-a.part.width*a.part.height).slice(0,8);
    }
    get needsCapture(){return !this.items.length||this.items.some(item=>item.node.tagName==="CANVAS"&&!item.pixels);}
    get captureRect(){
      if(!this.items.length)return this.rect;
      const x=Math.min(...this.items.map(item=>item.watchPart.x)),y=Math.min(...this.items.map(item=>item.watchPart.y));
      return {x,y,width:Math.max(...this.items.map(item=>item.watchPart.x+item.watchPart.width))-x,height:Math.max(...this.items.map(item=>item.watchPart.y+item.watchPart.height))-y};
    }
    contains(x,y){return this.items.some(item=>x>=item.part.x&&x<=item.part.x+item.part.width&&y>=item.part.y&&y<=item.part.y+item.part.height);}
    changed(){
      return this.items.some(item=>{
        const node=item.node;if(!visible(node))return true;
        const now=box(node);
        if(["x","y","width","height"].some(key=>Math.abs(now[key]-item.position[key])>1))return true;
        if((node.currentSrc||node.src||"")!==item.source||node.width!==item.width||node.height!==item.height)return true;
        return item.pixels&&difference(item.pixels,sample(node))>1.5;
      });
    }
  }
  globalThis.ComicReaderPageWatch=PageWatch;
})();

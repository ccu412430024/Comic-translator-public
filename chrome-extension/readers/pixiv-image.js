/* Pixel helpers for public episode images. No network or persistent state here. */
(()=>{
  "use strict";
  if(globalThis.ComicPixivImage)return;
  const rotate=(value,bits)=>((value<<bits)|(value>>>(32-bits)))>>>0;
  function random(seed){
    const state=new Uint32Array(seed);if(!state.some(Boolean))state[0]=1;
    return ()=>{const result=Math.imul(rotate(Math.imul(state[1],5)>>>0,7),9)>>>0,shift=state[1]<<9;
      state[2]^=state[0];state[3]^=state[1];state[1]^=state[2];state[0]^=state[3];state[2]^=shift;state[3]=rotate(state[3],11);return result;};
  }
  async function restore(data,width,height,grid,key){
    if(![width,height,grid].every(Number.isSafeInteger)||width<=0||height<=0||grid<=0||width*height>20_000_000||data.length!==width*height*4)throw new Error("Pixiv 圖片尺寸無效。");
    if(!key)return new Uint8ClampedArray(data);
    const hash=await crypto.subtle.digest("SHA-256",new TextEncoder().encode("4wXCKprMMoxnyJ3PocJFs4CYbfnbazNe"+key));
    const next=random(new Uint32Array(hash,0,4));for(let i=0;i<100;i++)next();
    const columns=Math.floor(width/grid),tables=[];
    for(let row=0;row<Math.ceil(height/grid);row++){
      const order=Array.from({length:columns},(_,i)=>i);
      for(let i=columns-1;i>0;i--){const j=next()%(i+1);[order[i],order[j]]=[order[j],order[i]];}
      const inverse=[];for(let i=0;i<columns;i++)inverse[order[i]]=i;tables.push(inverse);
    }
    const output=new Uint8ClampedArray(data);
    for(let y=0;y<height;y++)for(let x=0;x<columns;x++){
      const start=(y*width+tables[Math.floor(y/grid)][x]*grid)*4;
      output.set(data.subarray(start,start+grid*4),(y*width+x*grid)*4);
    }
    return output;
  }
  function fit(iw,ih,box,align="center"){
    const scale=Math.min(box.width/iw,box.height/ih),width=iw*scale,height=ih*scale;
    return {x:box.x+(align==="right"?box.width-width:align==="left"?0:(box.width-width)/2),y:box.y+(box.height-height)/2,width,height};
  }
  function groups(length,mode,leadingBlank){
    const indexes=Array.from({length},(_,i)=>i);if(mode!=="spread")return indexes.map(i=>[i]);
    if(length===1)return [[0]];if(leadingBlank)indexes.unshift(null);if(indexes.length%2)indexes.push(null);
    return Array.from({length:indexes.length/2},(_,i)=>indexes.slice(i*2,i*2+2));
  }
  globalThis.ComicPixivImage={restore,fit,groups};
})();

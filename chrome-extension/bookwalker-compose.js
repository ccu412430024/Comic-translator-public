import {rgbDifference,sample} from "./frame-utils.js";
export {rgbDifference,sample};
/* Calibrate against the visible rendered image before predicting any spread. */
export function plans(position,sources){
  const result=[];
  for(const offset of [-1,0,-2,1]){
    const a=position+offset,b=a+1;
    if(sources.has(a)&&sources.has(b))for(const rtl of [true,false])result.push({pages:rtl?[b,a]:[a,b],offset,step:2,rtl});
  }
  for(const offset of [0,-1,1])if(sources.has(position+offset))result.push({pages:[position+offset],offset,step:1,rtl:true});
  return result;
}
export function pageLayout(width,height,bitmaps){
  const ratio=bitmaps.reduce((sum,b)=>sum+b.width/b.height,0),scale=Math.min(height,width/ratio);
  const sizes=bitmaps.map(b=>({width:b.width/b.height*scale,height:scale}));
  let left=(width-sizes.reduce((sum,b)=>sum+b.width,0))/2;
  return sizes.map(size=>{const box={x:left,y:(height-size.height)/2,...size};left+=size.width;return box;});
}
export function compose(width,height,bitmaps,background){
  const canvas=new OffscreenCanvas(width,height),pen=canvas.getContext("2d");pen.fillStyle=background;pen.fillRect(0,0,width,height);
  const boxes=pageLayout(width,height,bitmaps);
  bitmaps.forEach((bitmap,i)=>{const b=boxes[i];pen.drawImage(bitmap,b.x,b.y,b.width,b.height);});
  return canvas;
}
export function calibrate(frame,reference,sources){
  const actual=sample(reference);let best=null;
  for(const plan of plans(frame.position,sources)){
    const canvas=compose(frame.width,frame.height,plan.pages.map(p=>sources.get(p)),frame.background);
    const difference=rgbDifference(actual,sample(canvas));
    if(!best||difference<best.difference)best={...plan,difference};
  }
  if(!best||best.difference>4.5){const error=new Error("頁面圖片與目前版面未能對應，已停止預翻譯；可使用原本的畫面翻譯。");error.calibration=best;throw error;}
  return best;
}

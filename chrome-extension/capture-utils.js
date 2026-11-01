export function cropGeometry(imageWidth,imageHeight,viewport,rect){
  const sx=imageWidth/viewport.width,sy=imageHeight/viewport.height;
  const left=Math.max(0,Math.min(imageWidth-1,Math.round(rect.x*sx)));
  const top=Math.max(0,Math.min(imageHeight-1,Math.round(rect.y*sy)));
  const width=Math.max(1,Math.min(imageWidth-left,Math.round(rect.width*sx)));
  const height=Math.max(1,Math.min(imageHeight-top,Math.round(rect.height*sy)));
  return {left,top,width,height};
}
export function meanDifference(a,b){
  if(a.length!==b.length)return Infinity;
  let total=0,count=0;
  for(let i=0;i<a.length;i+=4){total+=Math.abs(a[i]-b[i])+Math.abs(a[i+1]-b[i+1])+Math.abs(a[i+2]-b[i+2]);count+=3;}
  return count?total/count:Infinity;
}

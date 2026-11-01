/* Platform-independent sampling for render validation and queue reuse. */
export function rgbDifference(a,b){
  if(a.length!==b.length)return Infinity;let sum=0;
  for(let i=0;i<a.length;i+=4)sum+=Math.abs(a[i]-b[i])+Math.abs(a[i+1]-b[i+1])+Math.abs(a[i+2]-b[i+2]);
  return sum/(a.length/4*3);
}
export function sample(image,width=160,height=96){const c=new OffscreenCanvas(width,height),pen=c.getContext("2d");pen.drawImage(image,0,0,width,height);return pen.getImageData(0,0,width,height).data;}

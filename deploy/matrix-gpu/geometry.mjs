export function inverseQuad(quad) {
 if(!Array.isArray(quad)||quad.length!==8||quad.some(v=>!Number.isFinite(v))) throw Error('Invalid quad');
 const [x0,y0,x1,y1,x2,y2,x3,y3]=quad;
 const dx1=x1-x2,dx2=x3-x2,dx3=x0-x1+x2-x3,dy1=y1-y2,dy2=y3-y2,dy3=y0-y1+y2-y3;
 const divisor=dx1*dy2-dx2*dy1;
 if(Math.abs(divisor)<1e-9) return null;
 const g=(dx3*dy2-dx2*dy3)/divisor,h=(dx1*dy3-dx3*dy1)/divisor;
 const m=[x1-x0+g*x1,x3-x0+h*x3,x0,y1-y0+g*y1,y3-y0+h*y3,y0,g,h,1];
 const [a,b,c,d,e,f,j,k,l]=m,det=a*(e*l-f*k)-b*(d*l-f*j)+c*(d*k-e*j);
 if(Math.abs(det)<1e-10) return null;
 return [(e*l-f*k)/det,(c*k-b*l)/det,(b*f-c*e)/det,(f*j-d*l)/det,(a*l-c*j)/det,(c*d-a*f)/det,(d*k-e*j)/det,(b*j-a*k)/det,(a*e-b*d)/det];
}
export function rectPoints(x,y,w,h) {return [[x,y],[x+w,y],[x+w,y+h],[x,y+h]];}
function length(value,reference) {
 if(!/^-?(?:\d+(?:\.\d*)?|\.\d+)(?:px|%)?$/.test(value.trim())) throw Error('Unsupported clip length: '+value);
 return parseFloat(value)*(value.endsWith('%')?reference/100:1);
}
export function cssClip(value,w,h) {
 if(value==='none') return [];
 if(value.startsWith('polygon(')) {
  const content=value.slice(8,-1).replace(/^(nonzero|evenodd),\s*/, '');
  return [{kind:'polygon',points:content.split(',').map(pair=>{const parts=pair.trim().split(/\s+/);if(parts.length!==2)throw Error('Invalid polygon');return [length(parts[0],w)/w,length(parts[1],h)/h];})}];
 }
 if(value.startsWith('inset(')) {
  const parts=value.slice(6,-1).trim().split(/\s+/);
  if(parts.includes('round')||parts.length>4) throw Error('Rounded inset requires explicit adapter');
  const [t,r=t,b=t,l=r]=parts;
  const left=length(l,w),right=length(r,w),top=length(t,h),bottom=length(b,h);
  return [{kind:'polygon',points:rectPoints(left/w,top/h,Math.max(0,w-left-right)/w,Math.max(0,h-top-bottom)/h)}];
 }
 if(value.startsWith('circle(')||value.startsWith('ellipse(')) {
  const circle=value.startsWith('circle('),inside=value.slice(circle?7:8,-1).split(/\s+at\s+/);
  const center=(inside[1]||'50% 50%').trim().split(/\s+/);
  const radii=inside[0].trim().split(/\s+/);
  const rx=length(radii[0],circle?Math.hypot(w,h)/Math.SQRT2:w),ry=circle?rx:length(radii[1],h);
  return [{kind:'ellipse',center:[length(center[0],w)/w,length(center[1],h)/h],radius:[rx/w,ry/h]}];
 }
 throw Error('Unsupported clip-path: '+value);
}
export function objectCrop(boxW,boxH,sourceW,sourceH,fit,position) {
 if(fit==='fill') return [0,0,1,1];
 if(fit!=='cover'&&fit!=='contain') throw Error('Unsupported object-fit: '+fit);
 const parts=position.trim().split(/\s+/);
 if(parts.length!==2||parts.some(p=>!/^\d+(\.\d+)?%$/.test(p))) throw Error('Unsupported object-position: '+position);
 const scale=(fit==='contain'?Math.min:Math.max)(boxW/sourceW,boxH/sourceH),u=boxW/(sourceW*scale),v=boxH/(sourceH*scale);
 return [(1-u)*parseFloat(parts[0])/100,(1-v)*parseFloat(parts[1])/100,u,v];
}
export function compareStack(a,b) {
 const n=Math.max(a.length,b.length);
 for(let i=0;i<n;i++){const d=(a[i]??0)-(b[i]??0);if(d)return d;}
 return 0;
}

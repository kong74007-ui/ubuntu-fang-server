import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import puppeteer from 'puppeteer-core';
import {inverseQuad,rectPoints,cssClip,objectCrop,compareStack} from './geometry.mjs';
import {compile} from './compile.mjs';

export class BrowserScene {
 static async open(project,executable,variables={}) {
  const scene=new BrowserScene();scene.project=fs.realpathSync(project);scene.nodes=new Map();scene.lastState=new Map();
  scene.browser=await puppeteer.launch({executablePath:executable,headless:true,args:['--allow-file-access-from-files','--disable-gpu']});
  try {
   scene.page=await scene.browser.newPage();scene.errors=[];
   scene.page.on('pageerror',e=>scene.errors.push(e.message));
   scene.page.on('console',message=>{if(message.type()==='error'&&message.text().includes('composition script error'))scene.errors.push(message.text());});
   const compiled=compile(scene.project);
   await scene.page.setRequestInterception(true);
   scene.page.on('request',request=>{
    try {
     const url=new URL(request.url());
     if(url.protocol==='data:')return request.continue();
     if(url.protocol!=='file:')return request.abort();
     const file=fs.realpathSync(fileURLToPath(url)),relative=path.relative(scene.project,file);
     if(relative.startsWith('..')||path.isAbsolute(relative))return request.abort();
     if(file===path.join(scene.project,'index.html')&&request.resourceType()==='document')return request.respond({status:200,contentType:'text/html; charset=utf-8',body:compiled});
     if(request.resourceType()==='media')return request.abort();
     return request.continue();
    }catch{return request.abort();}
   });
   await scene.page.setViewport({width:1080,height:1920,deviceScaleFactor:1});
   await scene.page.evaluateOnNewDocument(values=>{
    window.__timelines={};window.__hyperframes={getVariables:()=>{
     const definitions=JSON.parse(document.documentElement.dataset.compositionVariables||'[]');
     return {...Object.fromEntries(definitions.map(d=>[d.id,d.default])),...values};
    }};
   },variables);
   await scene.page.goto(pathToFileURL(path.join(scene.project,'index.html')).href,{waitUntil:'load'});
   scene.description=await scene.page.evaluate(async supplied=>{
    const definitions=JSON.parse(document.documentElement.dataset.compositionVariables||'[]');
    const values={...Object.fromEntries(definitions.map(d=>[d.id,d.default])),...supplied};
    for(const [name,value]of Object.entries(supplied))if(definitions.length&&!definitions.some(d=>d.id===name))throw Error('Unknown variable '+name);
    for(const el of document.querySelectorAll('[data-var-text]'))if(values[el.dataset.varText]!==undefined)el.textContent=String(values[el.dataset.varText]);
    for(const el of document.querySelectorAll('[data-var-src]'))if(values[el.dataset.varSrc]!==undefined)el.setAttribute('src',String(values[el.dataset.varSrc]));
    await document.fonts.ready;
    if([...document.fonts].some(f=>f.status==='error'))throw Error('Font load failed');
    const root=document.querySelector('[data-composition-id]');
    if(!root)throw Error('Missing root');
    const duration=Number(root.dataset.duration);
    if(!(duration>0&&duration<=180))throw Error('Invalid duration');
    const globalWindow=el=>{
     let start=Number(el.dataset.start||0),duration=Number(el.dataset.duration||root.dataset.duration);
     for(let p=el.parentElement;p;p=p.parentElement){if(p.hasAttribute('data-composition-file')){const offset=Number(p.dataset.start||0);duration=Math.min(duration,Number(p.dataset.duration)-start);start+=offset;}}
     if(!Number.isFinite(start)||!Number.isFinite(duration)||start<0||duration<=0)throw Error('Invalid media timing');
     return{start,duration};
    };
    const media=[...document.querySelectorAll('video,img[src]')].map((el,i)=>{
     el.dataset.matrixGpuMedia=String(i);if(el.tagName==='VIDEO')el.pause();
     const window=globalWindow(el);el.dataset.matrixGpuStart=String(window.start);el.dataset.matrixGpuDuration=String(window.duration);
     return {id:i,elementId:el.id,src:el.getAttribute('src'),image:el.tagName==='IMG',...window,mediaStart:Number(el.dataset.mediaStart||0),playbackRate:Number(el.dataset.playbackRate||1)};
    });
    if(!media.length)throw Error('No visual media resolved; refusing text-only success');
    for(const canvas of document.querySelectorAll('canvas')){
     if(root.dataset.compositionId!=='yellow-banner-zoom'||canvas.id!=='media-canvas'||typeof window.__yellowBannerPoseAt!=='function')throw Error('Unsupported canvas compositor');
    }
    const audio=[...document.querySelectorAll('audio[src]')].map(el=>({src:el.getAttribute('src'),start:Number(el.dataset.start||0),duration:Number(el.dataset.duration||duration),mediaStart:Number(el.dataset.mediaStart||0),volume:Number(el.dataset.volume??1)}));
    for(const el of document.querySelectorAll('body *'))if(getComputedStyle(el).backgroundImage.includes('url(')&&!['VIDEO','IMG'].includes(el.tagName))throw Error('Unadapted CSS background image');
    let index=0;for(const el of document.querySelectorAll('body *'))el.dataset.matrixGpuNode=String(index++);
    const overlays=new Set();
    for(const text of document.querySelectorAll('[data-var-text]')){
     let el=text;while(el.parentElement&&el.parentElement!==root&&!el.parentElement.querySelector('[data-matrix-gpu-media]'))el=el.parentElement;
     overlays.add(el);
    }
    for(const el of overlays)el.dataset.matrixGpuOverlay='1';
    return {duration,width:Number(root.dataset.width||1080),height:Number(root.dataset.height||1920),media,audio,composition:root.dataset.compositionId};
   },variables);
   if(scene.errors.length)throw Error(scene.errors.join('; '));
   scene.cdp=await scene.page.createCDPSession();
   const {root}=await scene.cdp.send('DOM.getDocument',{depth:-1});
   const visit=node=>{
    const attrs=node.attributes||[];const at=attrs.indexOf('data-matrix-gpu-node');
    if(at>=0)scene.nodes.set(attrs[at+1],node.nodeId);
    for(const child of node.children||[])visit(child);
   };visit(root);
   // Initialize lazy GSAP fromTo tweens before sampling frame zero.
   await scene.seek(0.000001);
   await scene.seek(0);
   return scene;
  }catch(error){await scene.close();throw error;}
 }
 async seek(time) {
  return this.page.evaluate(t=>{
   for(const [id,timeline] of Object.entries(window.__timelines)){
    const host=[...document.querySelectorAll('[data-composition-file]')].find(el=>el.dataset.compositionId===id);
    let offset=0;for(let p=host;p;p=p.parentElement)if(p.hasAttribute('data-composition-file'))offset+=Number(p.dataset.start||0);
    timeline.seek(Math.max(0,t-offset),false);
   }
  },time);
 }
 async captureLayer(foreground,filename) {
   await this.page.evaluate(mode=>{
    window.__gpuStyleRestore=[];
    const all=[document.documentElement,document.body,...document.querySelectorAll('body *')];
    for(const el of all)window.__gpuStyleRestore.push([el,el.getAttribute('style')]);
    const text=[...document.querySelectorAll('[data-matrix-gpu-overlay]')];
    if(mode){
     const show=new Set();for(const el of text){for(let p=el;p;p=p.parentElement)show.add(p);for(const child of el.querySelectorAll('*'))show.add(child);}
     for(const el of document.querySelectorAll('body *'))el.style.setProperty('visibility',show.has(el)?'visible':'hidden','important');
     for(const el of [document.documentElement,document.body,document.querySelector('[data-composition-id]')])el.style.setProperty('background','transparent','important');
    }else{
     for(const el of text){el.style.setProperty('visibility','hidden','important');for(const child of el.querySelectorAll('*'))child.style.setProperty('visibility','hidden','important');}
     const typography=document.querySelector('.text-layer');if(typography)typography.style.setProperty('visibility','hidden','important');
    }
    for(const el of document.querySelectorAll('video,img[src],canvas'))el.style.setProperty('visibility','hidden','important');
   },foreground);
   try{await this.page.screenshot({path:filename,omitBackground:foreground});}
   finally{await this.page.evaluate(()=>{for(const[el,style]of window.__gpuStyleRestore){if(style===null)el.removeAttribute('style');else el.setAttribute('style',style);}delete window.__gpuStyleRestore;});}
 }
 async staticLayers(output) {
  await this.seek(0);
  await this.captureLayer(false,path.join(output,'background.png'));
  await this.captureLayer(true,path.join(output,'foreground.png'));
 }
 async foregroundKey() {
  return this.page.evaluate(()=>[...document.querySelectorAll('[data-matrix-gpu-overlay]')].map(root=>[root,...root.querySelectorAll('*')].map(el=>[el.getAttribute('style'),el.getAttribute('transform'),el.getAttribute('points'),el.getAttribute('d'),getComputedStyle(el).opacity,getComputedStyle(el).visibility].join('|')).join('~')).join('^'));
 }
 async geometry(node,padding=false) {
  const nodeId=this.nodes.get(node);if(!nodeId)throw Error('Unknown DOM geometry node');
  const {model}=await this.cdp.send('DOM.getBoxModel',{nodeId});
  return inverseQuad(padding?model.padding:model.content);
 }
 async frame(time,metadata) {
  await this.seek(time);
  const state=await this.page.evaluate(t=>{
   const root=document.querySelector('[data-composition-id]');
   const chainFor=el=>{const result=[];for(let p=el;p&&p!==document.body;p=p.parentElement)result.unshift(p);return result;};
   const svgShapes=(value,w,h)=>{
    const match=value.match(/#([^"')]+)/);if(!match)throw Error('Invalid SVG clip');
    const clip=document.getElementById(decodeURIComponent(match[1]));if(!clip)throw Error('Missing SVG clip');
    const box=clip.getAttribute('clipPathUnits')==='objectBoundingBox';const sx=box?1:w,sy=box?1:h;
    return [...clip.children].map(shape=>{
     const attr=name=>Number(shape.getAttribute(name)||0);
     let points;
     const tag=shape.tagName.toLowerCase();
     if(tag==='polygon')points=Array.from(shape.points,p=>[p.x,p.y]);
     else if(tag==='rect'){
      if(attr('rx')||attr('ry'))throw Error('Rounded SVG rect unsupported');
      const x=attr('x'),y=attr('y'),rw=attr('width'),rh=attr('height');points=[[x,y],[x+rw,y],[x+rw,y+rh],[x,y+rh]];
     }else if(tag==='circle'||tag==='ellipse'){
      if(shape.getAttribute('transform'))throw Error('Transformed SVG ellipse unsupported');
      return {kind:'ellipse',center:[attr('cx')/sx,attr('cy')/sy],radius:[attr(tag==='circle'?'r':'rx')/sx,attr(tag==='circle'?'r':'ry')/sy]};
     }else throw Error('Unsupported SVG clip shape '+tag);
     const matrix=shape.transform.baseVal.consolidate()?.matrix;
     if(matrix)points=points.map(([x,y])=>{const p=new DOMPoint(x,y).matrixTransform(matrix);return[p.x,p.y];});
     return {kind:'polygon',points:points.map(([x,y])=>[x/sx,y/sy])};
    });
   };
   const result=[];
   for(const el of document.querySelectorAll('[data-matrix-gpu-media],canvas')){
    const radial=el.tagName==='CANVAS'?window.__yellowBannerPoseAt(t):null;
    const media=radial?document.getElementById('media'+radial.slot):el;
    if(!media||!media.hasAttribute('data-matrix-gpu-media'))throw Error('Canvas source is missing');
    const start=Number(media.dataset.matrixGpuStart),duration=Number(media.dataset.matrixGpuDuration);
    if(t+1e-7<start||t>=start+duration-1e-7)continue;
    const chain=chainFor(el);let opacity=1,blur=0,hidden=false;const stack=[],clips=[];
    for(const p of chain){
     const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'){hidden=true;break;}
     opacity*=Number(s.opacity);
     if(s.mixBlendMode!=='normal')throw Error('Unsupported mix-blend-mode');
     if(s.filter!=='none'){
      const match=s.filter.match(/^blur\(([\d.]+)px\)$/);if(!match)throw Error('Unsupported video filter '+s.filter);blur+=Number(match[1]);
     }
     if(s.transform!=='none'||s.opacity!=='1'||s.zIndex!=='auto'||s.isolation==='isolate'||p===root)stack.push(s.zIndex==='auto'?0:Number(s.zIndex),Number(p.dataset.matrixGpuNode));
     const node=p.dataset.matrixGpuNode,w=p.clientWidth,h=p.clientHeight;
     if(w<=0||h<=0){hidden=true;break;}
     if(p!==el&&((s.overflowX==='hidden'||s.overflowX==='clip')&&(s.overflowY==='hidden'||s.overflowY==='clip')))clips.push({node,padding:true,rect:true,w,h});
     else if(p!==el&&((s.overflowX==='hidden'||s.overflowX==='clip')!==(s.overflowY==='hidden'||s.overflowY==='clip')))throw Error('One-axis clipping unsupported');
     if(s.clipPath!=='none')clips.push({node,w,h,css:s.clipPath,shapes:s.clipPath.startsWith('url(')?svgShapes(s.clipPath,w,h):null});
     if([s.borderTopLeftRadius,s.borderTopRightRadius,s.borderBottomLeftRadius,s.borderBottomRightRadius].some(v=>v!=='0px'))throw Error('Native media rounded corners need adapter');
    }
    if(hidden||opacity<.00001)continue;
    const s=getComputedStyle(el);stack.push(Number(el.dataset.matrixGpuNode));
    result.push({id:Number(media.dataset.matrixGpuMedia),node:el.dataset.matrixGpuNode,opacity,blur,stack,clips,width:el.clientWidth,height:el.clientHeight,fit:s.objectFit,position:s.objectPosition,radial});
   }
   return result;
  },time);
  const geometryCache=new Map();
  const geometry=async(node,padding=false)=>{const key=node+':'+padding;if(!geometryCache.has(key))geometryCache.set(key,await this.geometry(node,padding));return geometryCache.get(key);};
  const layers=[];
  for(const layer of state){
   layer.inverse=await geometry(layer.node);if(!layer.inverse)continue;
   const groups=[];
   for(const clip of layer.clips){
    const inverse=await geometry(clip.node,clip.padding);if(!inverse){layer.opacity=0;break;}
    const shapes=clip.rect?[{kind:'polygon',points:rectPoints(0,0,1,1)}]:clip.shapes||cssClip(clip.css,clip.w,clip.h);
    groups.push({inverse,shapes});
   }
   if(!layer.opacity)continue;
   const source=metadata.get(layer.id);if(!source)throw Error('Missing source metadata');
   layer.crop=layer.radial?[0,0,1,1]:objectCrop(layer.width,layer.height,source.width,source.height,layer.fit,layer.position);
   layer.clips=groups;layers.push(layer);
  }
  return layers.sort((a,b)=>compareStack(a.stack,b.stack));
 }
 async close(){if(this.browser)await this.browser.close();}
}

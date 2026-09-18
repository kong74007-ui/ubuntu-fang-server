import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawn,spawnSync} from 'node:child_process';
import {BrowserScene} from './browser.mjs';
import {Compositor} from './compositor.mjs';

function argumentsOf(argv) {
 const result={};for(let i=0;i<argv.length;i+=2){if(!argv[i].startsWith('--')||argv[i+1]===undefined)throw Error('Expected named option and value');result[argv[i].slice(2)]=argv[i+1];}return result;
}
async function fileHash(file){const hash=crypto.createHash('sha256');for await(const chunk of fs.createReadStream(file))hash.update(chunk);return hash.digest('hex');}
export async function render(options) {
 const project=fs.realpathSync(options.project),output=path.resolve(options.output),work=path.dirname(output);
 const timeout=Number(options.timeout||900);if(!Number.isFinite(timeout)||timeout<=0||timeout>3600)throw Error('Invalid timeout');
 const deadline=Date.now()+timeout*1000;
 const remaining=()=>{const ms=deadline-Date.now();if(!(ms>0))throw Error('Render deadline exceeded');return ms;};
 if(fs.existsSync(output))throw Error('Refusing to overwrite output');
 fs.mkdirSync(work,{recursive:true});
 const children=new Set(),handles=[];
 let scene,compositor,encoder,encoderDone;
 const errors=[];let cancelled=false;let ownedCache=null;
 function stop(child){if(child.exitCode!==null)return;if(process.platform==='win32')spawnSync('taskkill',['/PID',String(child.pid),'/T','/F'],{stdio:'ignore',windowsHide:true,timeout:10000});else child.kill('SIGKILL');}
 const abort=()=>{cancelled=true;for(const child of children)stop(child);};
 process.once('SIGINT',abort);process.once('SIGTERM',abort);
 const run=(name,exe,args)=>new Promise((resolve,reject)=>{
  const log=fs.openSync(path.join(work,name+'.log'),'w');
  if(cancelled){fs.closeSync(log);return reject(Error('Render cancelled'));}
  const child=spawn(exe,args,{stdio:['ignore',log,log],windowsHide:true,detached:false});children.add(child);
  const timer=setTimeout(()=>stop(child),remaining());
  let settled=false;
  const finish=(error)=>{if(settled)return;settled=true;clearTimeout(timer);children.delete(child);fs.closeSync(log);error?reject(error):resolve();};
  child.once('error',error=>finish(error));
  child.once('close',code=>finish(code===0?null:Error(`${name} failed; inspect stage log`)));
 });
 const local=relative=>{
  const resolved=fs.realpathSync(path.resolve(project,relative));
  const rel=path.relative(project,resolved);if(rel.startsWith('..')||path.isAbsolute(rel))throw Error('Out-of-project asset');return resolved;
 };
 const probe=file=>{
  const r=spawnSync(options.ffprobe||'ffprobe',['-v','error','-protocol_whitelist','file,pipe','-show_streams','-show_format','-of','json',file],{encoding:'utf8',windowsHide:true,timeout:Math.min(15000,remaining())});
  if(r.status!==0)throw Error('Media probe failed');return JSON.parse(r.stdout);
 };
 try {
  const variables=options.variables?JSON.parse(fs.readFileSync(options.variables,'utf8')):{};
  scene=await BrowserScene.open(project,options.browser,variables);
  const {width,height,duration,media,audio}=scene.description;
  if(width!==1080||height!==1920)throw Error('Only the production portrait canvas is enabled');
  const sources=new Map(),metadata=new Map();
  for(const item of media){
   const file=local(item.src);let source=sources.get(file);
   if(!source){
    const info=probe(file),v=info.streams.find(s=>s.codec_type==='video');if(!v)throw Error('Missing video/image stream');
    const rotation=v.side_data_list?.find(s=>s.side_data_type==='Display Matrix')?.rotation||0;
    source={file,info: v,width:v.width,height:v.height,image:item.image,seconds:0};
    if(Math.abs(rotation)%180===90)[source.width,source.height]=[source.height,source.width];
    sources.set(file,source);
   }
   if(item.playbackRate!==1)throw Error('Retimed source needs explicit adapter');
   source.seconds=Math.max(source.seconds,item.mediaStart+Math.min(item.duration,duration-item.start));
   metadata.set(item.id,source);
  }
  const transfers=[...sources.values()].map(s=>s.info.color_transfer);
  const mode=transfers.includes('arib-std-b67')?'hlg':transfers.includes('smpte2084')?'pq':'sdr';
  const transfer={hlg:'arib-std-b67',pq:'smpte2084',sdr:'bt709'}[mode],primaries=mode==='sdr'?'bt709':'bt2020';
  const frames=Math.round(duration*30);
  const cache=path.resolve(options.cache||path.join(work,'raw-cache'));fs.mkdirSync(cache,{recursive:true});
  if(!options.cache)ownedCache=cache;
  let counter=0;
  for(const source of sources.values()) {
   if(cancelled)throw Error('Render cancelled');
   const digest=await fileHash(source.file);source.sha256=digest;
   const count=source.image?1:Math.ceil(source.seconds*30);
   const key=crypto.createHash('sha256').update(JSON.stringify([digest,mode,count,'rgba16-v1'])).digest('hex');
   const raw=path.join(cache,key+'.rgba16'),bytes=source.width*source.height*8;
   if(source.info.color_transfer&&['arib-std-b67','smpte2084'].includes(source.info.color_transfer)){
    if(source.info.color_primaries!=='bt2020')throw Error('Unsupported HDR primaries');
    if(!/(10|12|16|48|64|f32)/.test(source.info.pix_fmt||''))throw Error('Eight-bit input cannot be advertised as HDR');
   }
   if(!source.info.color_transfer&&/(10|12|16|48|64|f32)/.test(source.info.pix_fmt||''))throw Error('High-bit-depth input needs explicit transfer metadata');
   if(bytes*count>32*1024**3)throw Error('Decoded asset exceeds memory/disk contract');
   if(!fs.existsSync(raw)||fs.statSync(raw).size!==bytes*count){
    const stats=fs.statfsSync(cache);if(stats.bavail*stats.bsize<bytes*count+4*1024**3)throw Error('Insufficient scratch disk');
    let vf='fps=30,format=rgba64le';
    if((source.info.color_primaries&&source.info.color_primaries!==primaries)
        ||(source.info.color_transfer!==transfer&&!(mode==='sdr'&&!source.info.color_transfer))){
     const from=source.info.color_transfer||(source.image?'iec61966-2-1':'bt709');
     vf=`fps=30,format=gbrap16le,zscale=pin=${source.info.color_primaries||'bt709'}:tin=${from}:min=gbr:rin=full:p=${primaries}:t=${transfer}:m=gbr:r=full:npl=203,format=rgba64le`;
    }
    const temporary=raw+'.'+crypto.randomUUID()+'.part';
    await run('decode-'+counter++,options.ffmpeg||'ffmpeg',['-hide_banner','-loglevel','error','-nostdin','-y','-protocol_whitelist','file,pipe','-i',source.file,'-vf',vf,'-frames:v',String(count),'-f','rawvideo',temporary]);
    if(fs.statSync(temporary).size!==bytes*count)throw Error('Source does not cover authored timeline');
    fs.renameSync(temporary,raw);
   }
   source.handle=fs.openSync(raw,'r');handles.push(source.handle);source.bytes=bytes;source.count=count;source.buffer=Buffer.alloc(bytes);source.key=key;
  }
  await scene.staticLayers(work);
  compositor=await Compositor.create(width,height,options.adapter||'');
  console.log(JSON.stringify({phase:'gpu_ready',adapter:compositor.adapter,mode,frames}));
  const staticLayers=[];
  const staticFilter=mode==='sdr'?'format=rgba64le':`format=gbrap16le,zscale=pin=bt709:tin=iec61966-2-1:min=gbr:rin=full:p=${primaries}:t=${transfer}:m=gbr:r=full:npl=203,format=rgba64le`;
  for(const name of ['background','foreground']){
   const target=path.join(work,name+'.rgba16');
   await run(name+'-color',options.ffmpeg||'ffmpeg',['-hide_banner','-loglevel','error','-nostdin','-y','-i',path.join(work,name+'.png'),'-vf',staticFilter,'-frames:v','1','-f','rawvideo',target]);
   const texture=compositor.upload(name,fs.readFileSync(target),width,height,0);
   staticLayers.push({texture,inverse:[1/width,0,0,0,1/height,0,0,0,1],crop:[0,0,1,1],opacity:1,clips:[]});
  }
  const keyOf=async()=>crypto.createHash('sha256').update(await scene.foregroundKey()).digest('hex');
  let previousForeground=await keyOf();
  const foregroundCache=new Map([[previousForeground,fs.readFileSync(path.join(work,'foreground.rgba16'))]]);
  let foregroundCaptures=1;
  const encoded=path.join(work,'video.part.mp4');
  const args=['-hide_banner','-loglevel','verbose','-nostdin','-y','-f','rawvideo','-pix_fmt','rgb48le','-s',`${width}x${height}`,'-framerate','30',
   '-color_primaries',primaries,'-color_trc',transfer,'-colorspace','0','-color_range','pc','-i','pipe:0','-an','-c:v',mode==='sdr'?'h264_nvenc':'hevc_nvenc','-preset','p5','-cq','15',
   '-pix_fmt',mode==='sdr'?'yuv420p':'yuv420p10le','-color_primaries',primaries,'-color_trc',transfer,'-colorspace',mode==='sdr'?'bt709':'bt2020nc','-color_range','tv',
   ...(mode==='sdr'?[]:['-tag:v','hvc1']),'-movflags','+faststart',encoded];
  const encodingLog=fs.openSync(path.join(work,'encode.log'),'w');
  encoder=spawn(options.ffmpeg||'ffmpeg',args,{stdio:['pipe','ignore',encodingLog],windowsHide:true,detached:false});children.add(encoder);
  encoder.stdin.on('error',error=>errors.push(error.message));
  encoderDone=new Promise(resolve=>{encoder.once('error',error=>errors.push(error.message));encoder.once('close',code=>{children.delete(encoder);fs.closeSync(encodingLog);resolve(code);});});
  const timer=setTimeout(abort,remaining());
  const started=Date.now();
  try{
   for(let n=0;n<frames;n++){
    remaining();if(cancelled)throw Error('Render cancelled');if(errors.length)throw Error(errors.join('; '));
    const time=n/30,states=await scene.frame(time,metadata),layers=[staticLayers[0]];
    const foregroundKey=await keyOf();
    if(foregroundKey!==previousForeground){
     let bytes=foregroundCache.get(foregroundKey);
     if(!bytes){
      const png=path.join(work,'foreground-current.png'),raw=path.join(work,'foreground-current.rgba16');
      await scene.captureLayer(true,png);
      await run('foreground-refresh',options.ffmpeg||'ffmpeg',['-hide_banner','-loglevel','error','-nostdin','-y','-i',png,'-vf',staticFilter,'-frames:v','1','-f','rawvideo',raw]);
      bytes=fs.readFileSync(raw);foregroundCaptures++;
      if(foregroundCache.size>=32)foregroundCache.delete(foregroundCache.keys().next().value);
      foregroundCache.set(foregroundKey,bytes);
     }
     staticLayers[1].texture=compositor.upload('foreground',bytes,width,height,foregroundKey);
     previousForeground=foregroundKey;
    }
    for(const state of states){
     const item=media[state.id],source=metadata.get(state.id);
     const frame=source.image?0:Math.min(source.count-1,Math.max(0,Math.floor((time-item.start+item.mediaStart)*30+1e-6)));
     if(source.lastFrame!==frame){const size=fs.readSync(source.handle,source.buffer,0,source.bytes,frame*source.bytes);if(size!==source.bytes)throw Error('Truncated raw media');source.lastFrame=frame;}
     const key=source.image?source.key:`${source.key}:media:${state.id}`;
     let texture=compositor.upload(key,source.buffer,source.width,source.height,frame);
     if(state.blur)texture=compositor.blur(texture,state.blur*source.width/state.width,state.blur*source.height/state.height,key);
     layers.push({...state,texture});
    }
    layers.push(staticLayers[1]);
    const frame=await compositor.frame(layers);
    await new Promise((resolve,reject)=>encoder.stdin.write(frame,error=>error?reject(error):resolve()));
    if(n%30===0)console.log(JSON.stringify({phase:'render',frame:n,frames,seconds:(Date.now()-started)/1000}));
   }
   encoder.stdin.end();if(await encoderDone!==0)throw Error('NVENC encode failed');
  }finally{clearTimeout(timer);}
  const activeAudio=audio.filter(a=>a.volume>0);
  if(activeAudio.length>1)throw Error('Multiple audio tracks need explicit mix adapter');
  const pending=output+'.part.mp4';
  if(activeAudio.length){const a=activeAudio[0];await run('mux',options.ffmpeg||'ffmpeg',['-hide_banner','-loglevel','error','-nostdin','-y','-i',encoded,'-ss',String(a.mediaStart),'-i',local(a.src),
    '-filter_complex',`[1:a]atrim=duration=${Math.min(a.duration,duration)},asetpts=PTS-STARTPTS,volume=${a.volume},adelay=${Math.round(a.start*1000)}:all=1,apad[a]`,
    '-map','0:v:0','-map','[a]','-c:v','copy','-c:a','aac','-t',String(duration),'-movflags','+faststart',pending]);
  }else await run('mux',options.ffmpeg||'ffmpeg',['-hide_banner','-loglevel','error','-nostdin','-y','-i',encoded,'-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-c:v','copy','-c:a','aac','-t',String(duration),'-movflags','+faststart',pending]);
  const final=probe(pending),v=final.streams.find(s=>s.codec_type==='video');
  if(v.color_transfer!==transfer||v.width!==width||v.height!==height||Number(v.nb_frames)!==frames||(mode!=='sdr'&&v.pix_fmt!=='yuv420p10le'))throw Error('Output contract failed');
  fs.renameSync(pending,output);
  const report={output,adapter:compositor.adapter,compositor:'webgpu-native',mode,frames,renderSeconds:(Date.now()-started)/1000,media:media.length,foregroundCaptures,
   sources:[...sources.values()].map(s=>({sha256:s.sha256,transfer:s.info.color_transfer,width:s.width,height:s.height})),probe:final};
  fs.writeFileSync(output+'.json',JSON.stringify(report,null,2));return report;
 }finally{
  abort();if(encoderDone)await encoderDone.catch(()=>{});
  for(const handle of handles)fs.closeSync(handle);
  if(scene)await scene.close();if(compositor)compositor.close();
  if(ownedCache&&path.dirname(ownedCache)===work&&path.basename(ownedCache)==='raw-cache')fs.rmSync(ownedCache,{recursive:true,force:true});
  process.removeListener('SIGINT',abort);process.removeListener('SIGTERM',abort);
 }
}
if(import.meta.url===new URL('file:'+process.argv[1].replaceAll('\\','/')).href||process.argv[1]?.endsWith('render.mjs')){
 render(argumentsOf(process.argv.slice(2))).then(report=>console.log(JSON.stringify(report))).catch(error=>{console.error(error.stack);process.exitCode=1;});
}

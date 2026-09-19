import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {Compositor} from './compositor.mjs';
import './browser.mjs';

const adapterIndex=process.argv.indexOf('--adapter');
const adapter=adapterIndex>=0?process.argv[adapterIndex+1]:process.env.MATRIX_GPU_ADAPTER||'';
const compositor=await Compositor.create(64,64,adapter);
const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'matrix-gpu-probe-'));
try {
 if(compositor.adapter.vendor!=='nvidia'||compositor.adapter.isFallbackAdapter!==false)throw Error('Verified NVIDIA hardware required for this release');
 const input=Buffer.alloc(64*64*8);
 for(let p=0;p<64*64;p++){input.writeUInt16LE(32768,p*8);input.writeUInt16LE(16384,p*8+2);input.writeUInt16LE(8192,p*8+4);input.writeUInt16LE(65535,p*8+6);}
 const texture=compositor.upload('probe',input,64,64,0);
 const frame=await compositor.frame([{texture,inverse:[1/64,0,0,0,1/64,0,0,0,1],crop:[0,0,1,1],opacity:1,clips:[]}]);
 if([32768,16384,8192].some((v,i)=>Math.abs(frame.readUInt16LE(i*2)-v)>20))throw Error('16-bit composition probe failed');
 const file=path.join(temporary,'sample.mp4');
 const encoded=spawnSync('ffmpeg',['-hide_banner','-loglevel','error','-nostdin','-f','lavfi','-i','color=c=blue:s=256x256:r=30:d=0.1',
  '-vf','format=p010le,setparams=range=tv:color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc',
  '-c:v','hevc_nvenc','-pix_fmt','p010le','-color_primaries','bt2020','-color_trc','arib-std-b67','-colorspace','bt2020nc','-tag:v','hvc1','-y',file],
  {windowsHide:true,encoding:'utf8',timeout:15000});
 if(encoded.status!==0)throw Error('Ten-bit NVENC probe failed');
 const checked=spawnSync('ffprobe',['-v','error','-select_streams','v:0','-show_entries','stream=codec_name,pix_fmt,color_transfer,color_primaries','-of','json',file],{windowsHide:true,encoding:'utf8',timeout:10000});
 if(checked.status!==0)throw Error('GPU probe artifact unavailable');
 const stream=JSON.parse(checked.stdout).streams[0];
 if(stream.codec_name!=='hevc'||stream.pix_fmt!=='yuv420p10le'||stream.color_transfer!=='arib-std-b67'||stream.color_primaries!=='bt2020')throw Error('GPU output format probe failed');
 console.log(JSON.stringify({ok:true,contract_version:1,compositor:'webgpu-native',encoder:'hevc_nvenc',adapter:compositor.adapter}));
}finally{
 compositor.close();
 if(path.dirname(temporary)===path.resolve(os.tmpdir())&&path.basename(temporary).startsWith('matrix-gpu-probe-'))fs.rmSync(temporary,{recursive:true,force:true});
}

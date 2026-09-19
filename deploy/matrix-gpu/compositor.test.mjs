import assert from 'node:assert/strict';
import {Compositor} from './compositor.mjs';
const compositor=await Compositor.create(64,64,process.env.MATRIX_GPU_ADAPTER||'');
try {
 console.log(JSON.stringify({adapter:compositor.adapter}));
 const source=Buffer.alloc(64*64*8);
 for(let p=0;p<64*64;p++){source.writeUInt16LE(32768,p*8);source.writeUInt16LE(16384,p*8+2);source.writeUInt16LE(8192,p*8+4);source.writeUInt16LE(65535,p*8+6);}
 const texture=compositor.upload('test',source,64,64,0);
 const frame=await compositor.frame([{texture,inverse:[1/64,0,0,0,1/64,0,0,0,1],crop:[0,0,1,1],opacity:1,clips:[]}]);
 assert.equal(frame.length,64*64*6);
 for(const [i,value] of [32768,16384,8192].entries()) assert.ok(Math.abs(frame.readUInt16LE(0+i*2)-value)<=20);
 const layer={texture,inverse:[1/64,0,0,0,1/64,0,0,0,1],crop:[0,0,1,1],opacity:1,clips:[]};
 const band=await compositor.frame([{...layer,radial:{band:1,zoom:1,strength:0.5,portrait:0}}]);
 assert.equal(band.readUInt16LE(0),0);
 assert.ok(Math.abs(band.readUInt16LE((32*64+32)*6)-32768)<=20);
 const portrait=await compositor.frame([{...layer,radial:{band:1,zoom:1,strength:0,portrait:1}}]);
 assert.ok(Math.abs(portrait.readUInt16LE(0)-32768)<=20);
 console.log(JSON.stringify({ok:true,bytes:frame.length}));
} finally {compositor.close();}

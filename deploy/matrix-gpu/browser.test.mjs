import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {BrowserScene} from './browser.mjs';
const executable=process.env.MATRIX_GPU_TEST_BROWSER||(process.platform==='win32'?'C:/Program Files/Google/Chrome/Application/chrome.exe':'/usr/bin/google-chrome');

test('timed text and a nested decoration do not paint over native footage',{skip:!fs.existsSync(executable)},async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'matrix-gpu-browser-'));let scene;
 try{
  fs.writeFileSync(path.join(root,'video.mp4'),'');
  fs.writeFileSync(path.join(root,'index.html'),`<html><head><style>
  html,body{margin:0;background:black}#root{position:relative;width:1080px;height:1920px;background:black}
  #stage{position:absolute;top:656px;width:1080px;height:608px;background:black}
  video{position:absolute;width:1080px;height:608px;object-fit:cover}
  #frame{position:absolute;inset:120px 120px;border:2px solid black}
  #title{position:absolute;left:20px;top:30px;width:120px;height:80px;background:red}
  </style></head><body><div id="root" data-composition-id="test" data-duration="2" data-width="1080" data-height="1920">
  <div id="stage" data-matrix-gpu-overlay-host="1"><video src="video.mp4" data-start="0" data-duration="2"></video><div id="frame" data-matrix-gpu-overlay="1"></div></div>
  <div id="title" class="clip" data-start="1" data-duration="1" data-var-text="title">Title</div></div></body></html>`);
  scene=await BrowserScene.open(root,executable);
  async function sample(at){
   await scene.seek(at);const file=path.join(root,'layer.png');await scene.captureLayer(true,file);
   return scene.page.evaluate(async data=>{const img=new Image();img.src=data;await img.decode();const c=document.createElement('canvas');c.width=1080;c.height=1920;const x=c.getContext('2d');x.drawImage(img,0,0);return{media:x.getImageData(500,900,1,1).data[3],title:x.getImageData(30,40,1,1).data[3]};},'data:image/png;base64,'+fs.readFileSync(file).toString('base64'));
  }
  const before=await sample(.2),after=await sample(1.2);
  assert.equal(before.media,0);assert.equal(after.media,0);
  assert.equal(before.title,0);assert.equal(after.title,255);
 }finally{if(scene)await scene.close();if(path.dirname(root)===path.resolve(os.tmpdir()))fs.rmSync(root,{recursive:true,force:true});}
});

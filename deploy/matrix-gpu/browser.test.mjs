import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {BrowserScene} from './browser.mjs';
const executable=process.env.MATRIX_GPU_TEST_BROWSER||(process.platform==='win32'?'C:/Program Files/Google/Chrome/Application/chrome.exe':'/usr/bin/google-chrome');

test('per-job typography preserves animations and rejects added overlap',{skip:!fs.existsSync(executable)},async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'matrix-text-guard-'));let scene;
 try{
  const python=spawnSync(process.env.PYTHON||'python',['-c','from server.matrix_text_controls import LAYOUT_GUARD; print(LAYOUT_GUARD)'],{cwd:path.resolve(import.meta.dirname,'../..'),encoding:'utf8',timeout:15000});
  assert.equal(python.status,0,python.stderr);
  fs.writeFileSync(path.join(root,'video.mp4'),'');
  fs.writeFileSync(path.join(root,'index.html'),`<html><head><style>html,body{margin:0}#root{position:relative;width:1080px;height:1920px}.text{position:absolute;left:100px;top:100px;font:40px Arial;white-space:nowrap}#second{top:300px}</style></head><body><div id="root" data-composition-id="test" data-duration="1" data-width="1080" data-height="1920"><video src="video.mp4" data-start="0" data-duration="1"></video><div id="first" class="text" data-var-text="first">Alpha</div><div id="second" class="text" data-var-text="second">Bravo</div></div><script>(function(){var selectors=['#first','#second'],rows=[{selector:'#first',properties:{'font-size':'52px','color':'#12ABCD','translate':'12px -8px'}}];${python.stdout}})();</script></body></html>`);
  scene=await BrowserScene.open(root,executable);
  assert.deepEqual(await scene.page.evaluate(()=>window.__matrixValidateTextStyles()),[]);
  const style=await scene.page.evaluate(()=>{const s=getComputedStyle(document.getElementById('first'));return [s.fontSize,s.color,s.translate]});
  assert.deepEqual(style,['52px','rgb(18, 171, 205)','12px -8px']);
  await scene.page.evaluate(()=>document.getElementById('first').style.setProperty('font-size','72px','important'));
  await scene.seek(.1);
  assert.equal(await scene.page.evaluate(()=>getComputedStyle(document.getElementById('first')).fontSize),'52px');
  await scene.page.evaluate(()=>document.getElementById('first').style.setProperty('translate','0px 240px','important'));
  const errors=await scene.page.evaluate(()=>window.__matrixValidateTextStyles());
  assert.ok(errors.some(e=>e.includes('Text layers overlap')));
  await scene.page.evaluate(()=>document.getElementById('first').style.opacity='0');
  assert.deepEqual(await scene.page.evaluate(()=>window.__matrixValidateTextStyles()),[]);
 }finally{if(scene)await scene.close();if(path.dirname(root)===path.resolve(os.tmpdir()))fs.rmSync(root,{recursive:true,force:true});}
});

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

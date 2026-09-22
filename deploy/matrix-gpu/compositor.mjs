import {create, globals} from 'webgpu';
Object.assign(globalThis, globals);

const VERTEX = `
@vertex fn vs(@builtin(vertex_index) i:u32)->@builtin(position) vec4f {
  let p=array<vec2f,3>(vec2f(-1,-1),vec2f(3,-1),vec2f(-1,3));
  return vec4f(p[i],0,1);
}`;
const FRAGMENT = `
struct Params { rows:array<vec4f,3>, crop:vec4f, opacity:vec4f, radial:vec4f }
@group(0) @binding(0) var image:texture_2d<f32>;
@group(0) @binding(1) var sampleImage:sampler;
@group(0) @binding(2) var<uniform> params:Params;
@group(0) @binding(3) var<storage,read> clips:array<f32>;
fn project(p:vec2f,a:vec3f,b:vec3f,c:vec3f)->vec2f {
 let v=vec3f(p,1); return vec2f(dot(a,v),dot(b,v))/dot(c,v);
}
fn allowed(p:vec2f)->bool {
 var o=1u;
 for(var g=0u;g<u32(clips[0]);g++) {
  let q=project(p,vec3f(clips[o],clips[o+1],clips[o+2]),vec3f(clips[o+3],clips[o+4],clips[o+5]),vec3f(clips[o+6],clips[o+7],clips[o+8]));
  let count=u32(clips[o+9]); o+=10u; var anyShape=false;
  for(var s=0u;s<count;s++) {
   let kind=u32(clips[o]); let n=u32(clips[o+1]); o+=2u;
   var inside=false;
   if(kind==1u) {
    let center=vec2f(clips[o],clips[o+1]); let radius=vec2f(clips[o+2],clips[o+3]);
    if(all(radius>vec2f(0))) {inside=dot((q-center)/radius,(q-center)/radius)<=1;}
    o+=4u;
   } else {
    if(n>=3u) {
     var j=n-1u;
     for(var i=0u;i<n;i++) {
      let a=vec2f(clips[o+2u*i],clips[o+2u*i+1u]); let b=vec2f(clips[o+2u*j],clips[o+2u*j+1u]);
      if((a.y>q.y)!=(b.y>q.y)) {if(q.x<(b.x-a.x)*(q.y-a.y)/(b.y-a.y)+a.x){inside=!inside;}}
      j=i;
     }
    }
    o+=n*2u;
   }
   anyShape=anyShape||inside;
  }
  if(!anyShape){return false;}
 }
 return true;
}
fn radialSample(p:vec2f)->vec4f {
 let band=params.radial.x; let zoom=params.radial.y; let portrait=params.radial.w;
 let q=(p-vec2f(.5))/zoom+vec2f(.5);
 let h=mix(.31640625*band,1.0,portrait);
 if(q.y<.5-h*.5||q.y>.5+h*.5){return vec4f(0);}
 var sourceUV=vec2f(q.x,.5+(q.y-.5)/band);
 if(portrait>.5){sourceUV=q;}
 return textureSampleLevel(image,sampleImage,clamp(sourceUV,vec2f(.001),vec2f(.999)),0);
}
@fragment fn fs(@builtin(position) position:vec4f)->@location(0) vec4f {
 let uv=project(position.xy,params.rows[0].xyz,params.rows[1].xyz,params.rows[2].xyz);
 if(any(uv<vec2f(0))||any(uv>vec2f(1))||!allowed(position.xy)){discard;}
 let sourceUV=params.crop.xy+uv*params.crop.zw;
 if(any(sourceUV<vec2f(0))||any(sourceUV>vec2f(1))){discard;}
 var tex=textureSampleLevel(image,sampleImage,sourceUV,0);
 if(params.opacity.y>0.5){
  let height=mix(.31640625*params.radial.x*params.radial.y,1.0,params.radial.w);
  if(abs(uv.y-.5)>height*.5){discard;}
  var color=vec4f(0);
  for(var i=0u;i<24u;i++){
   let c=radialSample(uv-(uv-vec2f(.5))*params.radial.z*f32(i)/23.0);
   color+=vec4f(c.rgb*c.a,c.a);
  }
  color/=24.0;
  if(color.a>0){color=vec4f(color.rgb/color.a,color.a);}
  tex=color;
 }
 let a=clamp(tex.a*params.opacity.x,0,1);
 return vec4f(tex.rgb*a,a);
}`;
const UPLOAD = `
@group(0) @binding(0) var<storage,read> pixels:array<u32>;
@group(0) @binding(1) var destination:texture_storage_2d<rgba16float,write>;
@compute @workgroup_size(8,8) fn main(@builtin(global_invocation_id) id:vec3u) {
 let size=textureDimensions(destination); if(any(id.xy>=size)){return;}
 let n=(id.y*size.x+id.x)*2u; let a=pixels[n]; let b=pixels[n+1u];
 textureStore(destination,id.xy,vec4f(f32(a&65535u),f32(a>>16),f32(b&65535u),f32(b>>16))/65535.0);
}`;
const PACK = `
@group(0) @binding(0) var image:texture_2d<f32>;
@group(0) @binding(1) var<storage,read_write> pixels:array<u32>;
@compute @workgroup_size(128) fn main(@builtin(global_invocation_id) id:vec3u) {
 let size=textureDimensions(image); let index=id.x*2u; if(index>=size.x*size.y){return;}
 let a=vec3u(round(clamp(textureLoad(image,vec2u(index%size.x,index/size.x),0).rgb,vec3f(0),vec3f(1))*65535));
 let j=index+1u; let b=vec3u(round(clamp(textureLoad(image,vec2u(j%size.x,j/size.x),0).rgb,vec3f(0),vec3f(1))*65535));
 pixels[id.x*3u]=a.r|(a.g<<16); pixels[id.x*3u+1u]=a.b|(b.r<<16); pixels[id.x*3u+2u]=b.g|(b.b<<16);
}`;
const BLUR = `
@group(0) @binding(0) var image:texture_2d<f32>;
@group(0) @binding(1) var sampleImage:sampler;
@group(0) @binding(2) var destination:texture_storage_2d<rgba16float,write>;
@group(0) @binding(3) var<uniform> setting:vec4f;
@compute @workgroup_size(8,8) fn main(@builtin(global_invocation_id) id:vec3u) {
 let size=textureDimensions(destination); if(any(id.xy>=size)){return;}
 let uv=(vec2f(id.xy)+.5)/vec2f(size); var total=vec4f(0); var weight=0.0;
 for(var i=-12;i<=12;i++) {
  let x=f32(i)/4; let w=exp(-.5*x*x); let delta=setting.xy*x/vec2f(size);
  total+=textureSampleLevel(image,sampleImage,uv+delta,0)*w; weight+=w;
 }
 textureStore(destination,id.xy,total/weight);
}`;

export function clipData(groups=[]) {
  if(groups.length>24) throw Error('Too many clip ancestors');
  const values=[groups.length];
  for(const group of groups) {
    if(group.inverse.length!==9||group.shapes.length>128) throw Error('Invalid clip group');
    values.push(...group.inverse,group.shapes.length);
    for(const shape of group.shapes) {
      if(shape.kind==='ellipse') values.push(1,4,...shape.center,...shape.radius);
      else {
        if(shape.points.length>256) throw Error('Clip polygon too complex');
        values.push(0,shape.points.length,...shape.points.flat());
      }
    }
  }
  if(values.some(n=>!Number.isFinite(n))) throw Error('Non-finite clip geometry');
  return new Float32Array(values.length<4?[...values,0,0,0]:values);
}

export class Compositor {
  static async create(width,height,adapterName='') {
    if(width%2||!Number.isInteger(width)||!Number.isInteger(height)||width<2||height<2||width>4096||height>4096) throw Error('Invalid canvas');
    const native=create([process.platform==='win32'?'backend=d3d12':'backend=vulkan',...(adapterName?[`adapter=${adapterName}`]:[])]);
    const adapter=await native.requestAdapter({powerPreference:'high-performance'});
    if(!adapter||adapter.info.isFallbackAdapter) throw Error('Hardware WebGPU adapter unavailable');
    const device=await adapter.requestDevice();
    const result=new Compositor(device,width,height);
    result.native=native;
    result.adapter={vendor:adapter.info.vendor,architecture:adapter.info.architecture,device:adapter.info.device,description:adapter.info.description,isFallbackAdapter:adapter.info.isFallbackAdapter};
    return result;
  }
  constructor(device,width,height) {
    this.device=device; this.width=width; this.height=height; this.sources=new Map(); this.layers=[];
    this.errors=[];
    device.addEventListener('uncapturederror',e=>this.errors.push(e.error.message));
    this.output=this.texture(width,height,GPUTextureUsage.RENDER_ATTACHMENT|GPUTextureUsage.TEXTURE_BINDING);
    this.sample=device.createSampler({minFilter:'linear',magFilter:'linear',addressModeU:'clamp-to-edge',addressModeV:'clamp-to-edge'});
    this.pipeline=device.createRenderPipeline({layout:'auto',vertex:{module:device.createShaderModule({code:VERTEX}),entryPoint:'vs'},
      fragment:{module:device.createShaderModule({code:FRAGMENT}),entryPoint:'fs',targets:[{format:'rgba16float',blend:{color:{srcFactor:'one',dstFactor:'one-minus-src-alpha'},alpha:{srcFactor:'one',dstFactor:'one-minus-src-alpha'}}}]},primitive:{topology:'triangle-list'}});
    this.uploadPipeline=device.createComputePipeline({layout:'auto',compute:{module:device.createShaderModule({code:UPLOAD}),entryPoint:'main'}});
    this.packPipeline=device.createComputePipeline({layout:'auto',compute:{module:device.createShaderModule({code:PACK}),entryPoint:'main'}});
    this.blurPipeline=device.createComputePipeline({layout:'auto',compute:{module:device.createShaderModule({code:BLUR}),entryPoint:'main'}});
    const size=width*height*6;
    this.packed=device.createBuffer({size,usage:GPUBufferUsage.STORAGE|GPUBufferUsage.COPY_SRC});
    this.readback=device.createBuffer({size,usage:GPUBufferUsage.COPY_DST|GPUBufferUsage.MAP_READ});
    this.packBinding=device.createBindGroup({layout:this.packPipeline.getBindGroupLayout(0),entries:[{binding:0,resource:this.output.createView()},{binding:1,resource:{buffer:this.packed}}]});
  }
  texture(w,h,extra=0) {return this.device.createTexture({size:[w,h],format:'rgba16float',usage:GPUTextureUsage.TEXTURE_BINDING|GPUTextureUsage.STORAGE_BINDING|extra});}
  upload(key,bytes,width,height,revision) {
    const d=this.device;
    if(bytes.length!==width*height*8) throw Error('Invalid RGBA16 byte length');
    let value=this.sources.get(key);
    if(value&&(value.width!==width||value.height!==height)) throw Error('Source dimensions changed');
    if(!value) {
      value={width,height,texture:this.texture(width,height),buffer:d.createBuffer({size:bytes.length,usage:GPUBufferUsage.STORAGE|GPUBufferUsage.COPY_DST})};
      value.binding=d.createBindGroup({layout:this.uploadPipeline.getBindGroupLayout(0),entries:[{binding:0,resource:{buffer:value.buffer}},{binding:1,resource:value.texture.createView()}]});
      this.sources.set(key,value);
    }
    if(value.revision===revision) return value.texture;
    d.queue.writeBuffer(value.buffer,0,bytes);
    const encoder=d.createCommandEncoder();const pass=encoder.beginComputePass();
    pass.setPipeline(this.uploadPipeline);pass.setBindGroup(0,value.binding);pass.dispatchWorkgroups(Math.ceil(width/8),Math.ceil(height/8));pass.end();
    d.queue.submit([encoder.finish()]);value.revision=revision;
    return value.texture;
  }
  blur(texture,sigmaX,sigmaY,key) {
    if(sigmaX<=.01&&sigmaY<=.01) return texture;
    let cache=this.sources.get('blur:'+key);
    if(!cache) {
      cache={a:this.texture(texture.width,texture.height),b:this.texture(texture.width,texture.height),params:[0,1].map(()=>this.device.createBuffer({size:16,usage:GPUBufferUsage.UNIFORM|GPUBufferUsage.COPY_DST}))};
      this.sources.set('blur:'+key,cache);
    }
    const encoder=this.device.createCommandEncoder();
    for(let i=0;i<2;i++) {
      this.device.queue.writeBuffer(cache.params[i],0,new Float32Array(i?[0,sigmaY,0,0]:[sigmaX,0,0,0]));
      const binding=this.device.createBindGroup({layout:this.blurPipeline.getBindGroupLayout(0),entries:[{binding:0,resource:(i?cache.a:texture).createView()},{binding:1,resource:this.sample},{binding:2,resource:(i?cache.b:cache.a).createView()},{binding:3,resource:{buffer:cache.params[i]}}]});
      const pass=encoder.beginComputePass();pass.setPipeline(this.blurPipeline);pass.setBindGroup(0,binding);pass.dispatchWorkgroups(Math.ceil(texture.width/8),Math.ceil(texture.height/8));pass.end();
    }
    this.device.queue.submit([encoder.finish()]);return cache.b;
  }
  async frame(layers) {
    if(layers.length>96) throw Error('Too many GPU layers');
    const d=this.device, encoder=d.createCommandEncoder();
    const pass=encoder.beginRenderPass({colorAttachments:[{view:this.output.createView(),clearValue:{r:0,g:0,b:0,a:1},loadOp:'clear',storeOp:'store'}]});
    pass.setPipeline(this.pipeline);
    for(let i=0;i<layers.length;i++) {
      const layer=layers[i];
      if(layer.inverse.length!==9||layer.crop.length!==4||!Number.isFinite(layer.opacity)) throw Error('Invalid layer');
      let buffers=this.layers[i];
      if(!buffers) {
        buffers={uniform:d.createBuffer({size:96,usage:GPUBufferUsage.UNIFORM|GPUBufferUsage.COPY_DST}),clip:d.createBuffer({size:262144,usage:GPUBufferUsage.STORAGE|GPUBufferUsage.COPY_DST})};
        this.layers[i]=buffers;
      }
      const effect=layer.radial;
      if(effect&&(![effect.band,effect.zoom,effect.strength,effect.portrait].every(Number.isFinite)||effect.band<=0||effect.zoom<=0))throw Error('Invalid radial pose');
      const matrix=layer.inverse, data=new Float32Array([...matrix.slice(0,3),0,...matrix.slice(3,6),0,...matrix.slice(6),0,...layer.crop,layer.opacity,effect?1:0,0,0,...(effect?[effect.band,effect.zoom,effect.strength,effect.portrait]:[0,0,0,0])]);
      const clips=clipData(layer.clips);
      if(clips.byteLength>262144) throw Error('Clip data too large');
      d.queue.writeBuffer(buffers.uniform,0,data);d.queue.writeBuffer(buffers.clip,0,clips);
      const binding=d.createBindGroup({layout:this.pipeline.getBindGroupLayout(0),entries:[{binding:0,resource:layer.texture.createView()},{binding:1,resource:this.sample},{binding:2,resource:{buffer:buffers.uniform}},{binding:3,resource:{buffer:buffers.clip}}]});
      pass.setBindGroup(0,binding);pass.draw(3);
    }
    pass.end();
    const pack=encoder.beginComputePass();pack.setPipeline(this.packPipeline);pack.setBindGroup(0,this.packBinding);pack.dispatchWorkgroups(Math.ceil(this.width*this.height/256));pack.end();
    encoder.copyBufferToBuffer(this.packed,0,this.readback,0,this.width*this.height*6);
    d.queue.submit([encoder.finish()]);
    await this.readback.mapAsync(GPUMapMode.READ);
    const output=Buffer.from(new Uint8Array(this.readback.getMappedRange()));this.readback.unmap();
    if(this.errors.length) throw Error(this.errors.join('; '));
    return output;
  }
  close() {this.device.destroy();this.native=null;this.sources.clear();this.layers=[];}
}

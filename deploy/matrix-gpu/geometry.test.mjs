import test from 'node:test';
import assert from 'node:assert/strict';
import {inverseQuad,cssClip,objectCrop,compareStack} from './geometry.mjs';
const project=(m,x,y)=>[(m[0]*x+m[1]*y+m[2])/(m[6]*x+m[7]*y+m[8]),(m[3]*x+m[4]*y+m[5])/(m[6]*x+m[7]*y+m[8])];
test('projective inverse preserves all four image corners',()=>{
 const q=[10,20,120,10,100,220,0,190],m=inverseQuad(q);
 for(let i=0;i<4;i++){const expected=[[0,0],[1,0],[1,1],[0,1]][i],actual=project(m,q[i*2],q[i*2+1]);assert.ok(actual.every((v,j)=>Math.abs(v-expected[j])<1e-8));}
});
test('collapsed geometry is not rendered',()=>{assert.equal(inverseQuad([0,0,0,0,0,0,0,0]),null);});
test('cover crop preserves source aspect and authored position',()=>{
 assert.deepEqual(objectCrop(100,100,200,100,'cover','50% 50%'),[.25,0,.5,1]);
 assert.deepEqual(objectCrop(200,100,100,200,'cover','50% 45%'),[0,.3375,1,.25]);
 assert.deepEqual(objectCrop(100,100,200,100,'contain','50% 50%'),[0,-.5,1,2]);
 assert.throws(()=>objectCrop(100,100,200,100,'scale-down','50% 50%'));
});
test('insets and polygons retain percentages',()=>{
 const rect=cssClip('inset(10% 20%)',200,100)[0].points;
 assert.ok(Math.abs(rect[2][0]-.8)<1e-10&&Math.abs(rect[2][1]-.9)<1e-10);
 assert.deepEqual(cssClip('polygon(0 0, 100% 0, 0 100%)',200,100)[0].points,[[0,0],[1,0],[0,1]]);
});
test('circle uses CSS normalized diagonal for percentage radius',()=>{
 const shape=cssClip('circle(50% at 50% 50%)',100,100)[0];
 assert.equal(shape.radius[0],.5);assert.equal(shape.radius[1],.5);
 assert.throws(()=>cssClip('path("unsupported")',100,100));
});
test('stack ordering compares contexts before child document order',()=>{
 assert.ok(compareStack([0,1,10,3],[0,1,9,99])>0);
});

import assert from 'node:assert/strict';
import test from 'node:test';
import {frameWindow, mergeWindows, sourceFrame} from './decode-plan.mjs';

test('late 4K three-second selection decodes 90 frames, not 690', () => {
  assert.deepEqual(frameWindow({start: 0, duration: 3, mediaStart: 20}, 3), {start: 600, end: 690});
});
test('disjoint selections do not decode the gap and overlaps are reused', () => {
  assert.deepEqual(mergeWindows([{start:600,end:690},{start:60,end:90},{start:615,end:700}]),
    [{start:60,end:90},{start:600,end:700}]);
});
test('fractional composition starts cover exactly the sampled source frames', () => {
  const item={start:0.017,duration:0.14,mediaStart:20.013};
  const window=frameWindow(item,1);
  const frames=[1,2,3,4].map(n=>sourceFrame(item,n/30));
  assert.equal(window.start,frames[0]);
  assert.equal(window.end,frames.at(-1)+1);
});
test('off-timeline sources consume no cache; stills consume one frame', () => {
  assert.equal(frameWindow({start:5,duration:3,mediaStart:10},3),null);
  assert.deepEqual(frameWindow({start:0,duration:3,mediaStart:20,image:true},3),{start:0,end:1});
  assert.throws(()=>frameWindow({start:0,duration:3,mediaStart:NaN},3));
});

import assert from 'node:assert/strict';
import test from 'node:test';
import {timeline} from '../src/performance_state.ts';

test('parallel operations use a shared wall-time axis', () => {
  const llm = timeline({start_s: 0, end_s: 4}, 6);
  const tts = timeline({start_s: 2, end_s: 6}, 6);
  assert.equal(llm.seconds, 4); assert.equal(tts.seconds, 4);
  assert.equal(llm.left, 0); assert.equal(tts.left, 100/3);
  assert.equal(tts.left + tts.width, 100);
});
test('only a running interval advances with backend elapsed time', () => {
  const active = {start_s: 2, end_s: null}, done = {start_s: 1, end_s: 3};
  assert.equal(timeline(active, 5).seconds, 3);
  assert.equal(timeline(active, 6).seconds, 4);
  assert.equal(timeline(done, 10).seconds, 2);
});
test('zero-time local operations do not create invalid CSS values', () => {
  const shape = timeline({start_s:0,end_s:0},0);
  assert.ok(Number.isFinite(shape.width)); assert.equal(shape.seconds, 0);
});

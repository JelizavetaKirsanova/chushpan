import assert from 'node:assert/strict';
import test from 'node:test';
import {defaultHomeActions, defaultHomeWidgets, moveItem, parseHomeLayout} from '../src/home_layout.ts';

test('home layout defaults provide a compact set of everyday widgets', () => {
  assert.deepEqual(parseHomeLayout(null), {widgets: defaultHomeWidgets, sizes: {}, actions: defaultHomeActions});
});

test('home layout preserves order and size while removing unknown and duplicate entries', () => {
  const layout = parseHomeLayout(JSON.stringify({
    widgets: ['weather', 'resume', 'weather', 'unsupported'],
    sizes: {weather: 'wide', resume: 'huge'},
    actions: ['calendar', 'lighting', 'calendar', 'unsupported'],
  }));
  assert.deepEqual(layout, {
    widgets: ['weather', 'resume'],
    sizes: {weather: 'wide'},
    actions: ['calendar', 'lighting'],
  });
});

test('home layout recovers from invalid saved JSON and moves items safely', () => {
  assert.deepEqual(parseHomeLayout('{'), {widgets: defaultHomeWidgets, sizes: {}, actions: defaultHomeActions});
  assert.deepEqual(moveItem(['a', 'b', 'c'], 2, 0), ['c', 'a', 'b']);
  assert.deepEqual(moveItem(['a', 'b'], 4, 0), ['a', 'b']);
});

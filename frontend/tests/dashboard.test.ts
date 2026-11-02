import test from 'node:test';
import assert from 'node:assert/strict';
import {dayKey, eventTime, measured, monthDays, safeColor, shouldAmbient} from '../src/dashboard_state.ts';

test('calendar uses Monday weeks and includes leap day without duplicate dates', () => {
  const days = monthDays(2028, 1);
  assert.equal(days.length % 7, 0);
  assert.equal(days.filter(day => day.current).length, 29);
  assert.equal(new Set(days.map(day => day.date)).size, days.length);
  assert.equal(days[0].date, '2028-01-31');
});
test('local day and all-day event are not shifted into UTC presentation', () => {
  assert.equal(dayKey(new Date('2026-11-01T22:30:00Z'), 'Europe/Tallinn'), '2026-11-02');
  assert.equal(eventTime({id:'x',title:'Real',start:'2026-11-01T22:00:00Z',end:'2026-11-02T22:00:00Z',all_day:true,calendar:'x',color:'#ffffff'}, 'Europe/Tallinn'), 'Весь день');
});
test('ambient requires actual idle signal and respects active work, visibility and minimization', () => {
  const settings = {ambient_enabled:true,ambient_minutes:5,ambient_display:'primary' as const,ambient_fullscreen:false,reduce_on_battery:true};
  assert.equal(shouldAmbient(settings, 301, true, false, true), true);
  assert.equal(shouldAmbient(settings, null, true, false, true), false);
  assert.equal(shouldAmbient(settings, 500, true, true, true), false);
  assert.equal(shouldAmbient(settings, 500, false, false, true), false);
  assert.equal(shouldAmbient(settings, 500, true, false, true, true), false);
});
test('missing statistics and calendar color input never become fabricated values or CSS', () => {
  assert.equal(measured(null), 'Нет данных');
  assert.equal(measured(0), '0.00 с');
  assert.equal(safeColor('url(https://example.com)'), '#a990da');
  assert.equal(safeColor('#a1B2c3'), '#a1B2c3');
});

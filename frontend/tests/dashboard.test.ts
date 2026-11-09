import test from 'node:test';
import assert from 'node:assert/strict';
import {continueWatchingItems,dayKey, eventTime, measured, monthDays, safeColor, shouldAmbient} from '../src/dashboard_state.ts';

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
test('home resume rail sorts saved progress, skips completed movies and advances completed series',()=>{
  const episode=(id:string,watched:number,last_viewed:string|null,position=122)=>({id,title_id:'t',season:1,episode:1,position,duration:4324,watched,last_viewed,last_link_id:null,links:[]});
  const title=(id:string,kind:'movie'|'series',current:ReturnType<typeof episode>|null,episodes:ReturnType<typeof episode>[],status:'watching'|'planned'|'watched'='watching')=>({id,title:id,kind,year:0,poster_url:'',description:'',status,favorite:0,episodes,current});
  const old=episode('old',0,'2026-11-07T10:00:00Z');
  const finished=episode('finished',1,'2026-11-08T10:00:00Z',4324);
  const next={...episode('next',0,null,0),season:1,episode:2};
  const recent=episode('recent',0,'2026-11-09T10:00:00Z');
  const rows=continueWatchingItems([title('old-title','movie',old,[old]),title('completed','movie',finished,[finished],'watched'),title('series','series',finished,[finished,next]),title('recent-title','movie',recent,[recent])] as any[]);
  assert.deepEqual(rows.map(row=>row.item.id),['recent-title','series','old-title']);
  assert.equal(rows[1].episode.id,'next');
});

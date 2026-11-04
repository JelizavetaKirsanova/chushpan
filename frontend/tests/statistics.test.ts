import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {Window} from 'happy-dom';
import {seconds} from '../src/workspace_state.ts';

function fixture() {
  const window=new Window({settings:{disableJavaScriptEvaluation:true,disableCSSFileLoading:true,disableJavaScriptFileLoading:true}});
  window.document.write(readFileSync(new URL('../index.html',import.meta.url),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,''));
  const code=stripTypeScriptTypes(readFileSync(new URL('../src/statistics.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'')+'\nexports.StatisticsDashboard=StatisticsDashboard;';
  const sandbox={exports:{} as Record<string,any>,document:window.document,window,icon:()=>'<svg></svg>',seconds,URL:window.URL,Blob:window.Blob,setTimeout:()=>0};
  vm.runInNewContext(code,sandbox);
  const queries:any[]=[];const traces:string[]=[];
  const view=new sandbox.exports.StatisticsDashboard((...v:any[])=>queries.push(v),(id:string)=>traces.push(id));
  const click=(name:string)=>{const b=[...window.document.querySelectorAll('#statistics-root button')].find(b=>b.textContent===name);assert.ok(b,`Missing button: ${name}`);b.click();};
  return {window,view,queries,traces,click};
}
function data(count=2):any {
  const times={stt:.3,llm:.7,tools:null,tts:1,total:3};
  return {start:'2026-11-05T00:00:00+02:00',end:'2026-11-05T12:00:00+02:00',timezone:'Europe/Tallinn',unit:'hour',
    metrics:{requests:count,response_s:count?1.2:null,executed:count?1:0,success_pct:count?100:null,known_operations:count?1:0,unconfirmed_operations:0},
    previous:{requests:0,response_s:null,executed:0,success_pct:null,known_operations:0,unconfirmed_operations:0},
    buckets:[{key:'2026-11-05T10',label:'10:00',requests:count,latency:count?times:{stt:null,llm:null,tools:null,tts:null,total:null}}],
    distribution:{conversations:count?1:0,devices:count?1:0,scenarios:0,search:0,other:0},
    requests:count?[{request_id:'talk',created_at:'2026-11-05T08:00:00Z',kind:'voice',category:'conversations',model:'Local',status:'completed',times,errors:[]},{request_id:'device',created_at:'2026-11-05T09:00:00Z',kind:'text',category:'devices',model:null,status:'completed',times:{...times,total:10},errors:[]}]:[],
    scenarios:[],apps:[]};
}

test('overview shows four honest KPIs, real charts and compact empty states',()=>{
  const f=fixture();try{
    f.view.connection(true);f.view.receive(data(0));
    assert.equal(f.window.document.querySelectorAll('.statistics-kpi').length,4);
    assert.equal(f.window.document.querySelectorAll('.statistics-bars,.statistics-line-chart,.statistics-donut').length,0);
    assert.equal(f.window.document.querySelectorAll('.statistics-empty').length,3);
    assert.equal(f.window.document.getElementById('statistics-pane-performance')!.hidden,true);
    f.view.receive(data());
    assert.equal(f.window.document.querySelectorAll('.statistics-bars').length,1);
    assert.equal(f.window.document.querySelectorAll('.statistics-line-chart').length,1);
    assert.equal(f.window.document.querySelectorAll('.statistics-donut').length,1);
    assert.doesNotMatch(f.window.document.querySelector('.statistics-kpis')!.textContent,/Infinity|NaN|\+100/);
    assert.match(f.window.document.querySelector('.statistics-kpis')!.textContent,/Первые события/);
    const series=[...f.window.document.querySelectorAll('.statistics-series button')];
    for(const b of series){const current=[...f.window.document.querySelectorAll('.statistics-series button')].find(n=>n.textContent===b.textContent)!;current.click();}
    assert.match(f.window.document.querySelector('.statistics-latency')!.textContent,/Все серии скрыты/);
  }finally{f.window.happyDOM.abort();}
});

test('every tab is reachable, traces open and diagnostics remain disclosed',()=>{
  const f=fixture();try{
    const snapshot=data();snapshot.scenarios=[{id:'run',name:'Вечер <script>',status:'failed',started_at:snapshot.start,error:'Тестовая ошибка',steps:[{path:'1',status:'failed',duration_s:2,result:{sent:false}}]}];
    f.view.receive(snapshot);
    assert.equal(f.window.document.querySelectorAll('[role=tab]').length,7);
    f.click('Разговоры');assert.match(f.window.document.querySelector('.statistics-content')!.textContent,/Local/);
    f.window.document.querySelector<HTMLButtonElement>('.statistics-request')!.click();assert.deepEqual(f.traces,['talk']);
    f.click('Команды');assert.equal(f.window.document.querySelectorAll('.statistics-request').length,1);
    f.click('Сценарии');assert.match(f.window.document.querySelector('.statistics-content')!.textContent,/Вечер <script>/);assert.equal(f.window.document.querySelectorAll('.statistics-content script').length,0);
    assert.equal(f.window.document.querySelectorAll('.statistics-receipt').length,1);
    f.click('Приложения');assert.match(f.window.document.querySelector('.statistics-content')!.textContent,/Учёт приложений пока пуст/);
    f.click('Отчёты');assert.match(f.window.document.querySelector('.statistics-content')!.textContent,/Скачать CSV/);
    f.click('Производительность');assert.equal(f.window.document.getElementById('statistics-pane-performance')!.hidden,false);assert.equal(f.window.document.querySelector('.statistics-content')!.hidden,true);
    f.click('Обзор');assert.equal(f.window.document.getElementById('statistics-pane-performance')!.hidden,true);
  }finally{f.window.happyDOM.abort();}
});

test('global periods include validated custom dates and reset stale visible numbers',()=>{
  const f=fixture();try{
    f.view.connection(true);f.view.receive(data());f.click('Год');assert.equal(f.queries.at(-1)[0],365);
    assert.doesNotMatch(f.window.document.querySelector('.statistics-kpis')!.textContent,/1[,.]20/);
    f.click('Произвольный период');
    const start=f.window.document.querySelector<HTMLInputElement>('[aria-label="Начало периода"]')!;const end=f.window.document.querySelector<HTMLInputElement>('[aria-label="Конец периода"]')!;
    start.value='2026-11-05';end.value='2026-11-01';f.click('Применить');assert.equal(f.queries.length,1);assert.match(f.window.document.querySelector('.statistics-status')!.textContent,/раньше конца/);
    start.value='2026-11-01';end.value='2026-11-05';f.click('Применить');assert.deepEqual(f.queries.at(-1),[-1,'2026-11-01','2026-11-05']);
    f.view.connection(false);assert.ok([...f.window.document.querySelectorAll<HTMLButtonElement>('.statistics-periods button')].every(b=>b.disabled));
  }finally{f.window.happyDOM.abort();}
});

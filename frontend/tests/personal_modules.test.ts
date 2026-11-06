import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import vm from 'node:vm';
import {Window} from 'happy-dom';

function fixture(){
  const window=new Window({settings:{disableJavaScriptEvaluation:true,disableCSSFileLoading:true,disableJavaScriptFileLoading:true}});
  window.document.write('<div class="hud" data-view="memory"><section id="module-memory"></section><section id="module-mac-control"></section><section id="module-my-devices"></section><section id="module-analytics"></section></div>');
  window.confirm=()=>true;
  const code=['module_ui','memory_library','mac_center','device_sync','analytics'].map(name=>stripTypeScriptTypes(readFileSync(new URL('../src/'+name+'.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'')).join('\n')+'\nexports.views=[MemoryLibraryView,MacCenterView,DeviceSyncView,AnalyticsView];exports.barChart=barChart;exports.donut=donut;';
  const sandbox={exports:{} as Record<string,any>,document:window.document,window,HTMLInputElement:window.HTMLInputElement,HTMLTextAreaElement:window.HTMLTextAreaElement,CustomEvent:window.CustomEvent,confirm:()=>true,setTimeout:()=>0,clearTimeout:()=>{},Blob:window.Blob,URL:window.URL,atob};
  vm.runInNewContext(code,sandbox);
  const sent:any[]=[];const destinations:string[]=[];const views=sandbox.exports.views.map((View:any)=>new View((channel:string,value:any)=>sent.push({channel,...value}),(view:string)=>destinations.push(view)));
  return {window,views,sent,destinations,sandbox};
}
function memory(){return {items:[{id:7,content:'Тестовый факт',category:'preference',source:'explicit_user',confidence:1,confirmed:1,sensitive:0,created_at:'2026-11-05T00:00:00Z',updated_at:'2026-11-05T00:00:00Z',revision:3,project_id:null,expires_at:null,status:'active'}],projects:[],total:1,used_today:0,categories:{preference:'Предпочтения'},pending:[],access:[],fts:true,policy:{automatic:'ask'},conversations:[],bindings:[]};}
function analytics(){return {summary:{count:0,voice:0,text:0,mean_s:null,p50_s:null,p95_s:null,failed:0,active_s:0,development_s:0,study_s:0,switches:0,apps:[],categories:{},daily:[],providers:{},commands:[],stages:[],latencies:[],events:0},previous:{},policy:{enabled:false,categories:[],retention_days:30,excluded_apps:[],development_apps:[],study_apps:[]},insights:[],dropped:0,error:null,query:{period:'today'},timezone:'Europe/Tallinn',devices:[],categories:{assistant:'Чушпан',mac:'Приложения'}};}

test('four modules request real channels and preserve honest offline state',()=>{const f=fixture();try{for(const view of f.views)view.connection(true);assert.deepEqual(f.sent.map(v=>v.channel),['memory_library','mac_center','device_sync','analytics']);f.views[0].receive({type:'memory_library',data:memory(),busy:false});f.views[0].connection(false);assert.match(f.window.document.getElementById('module-memory')!.textContent,/устаревшими/);assert.equal((f.window.document.querySelector('#module-memory button[data-write]') as HTMLButtonElement).disabled,true);}finally{f.window.happyDOM.abort();}});
test('memory details keep revision on deletion and projects navigate independently',()=>{const f=fixture();try{const view=f.views[0];view.connection(true);view.receive({type:'memory_library',data:memory(),busy:false});const detail=[...f.window.document.querySelectorAll('#module-memory button')].find(b=>b.textContent==='Подробнее')!;detail.click();const remove=[...f.window.document.querySelectorAll('dialog button')].find(b=>b.textContent==='Удалить')!;remove.click();assert.equal(f.sent.at(-1).op,'delete');assert.equal(f.sent.at(-1).revision,3);assert.equal(f.sent.at(-1).confirmed,true);for(const tab of f.window.document.querySelectorAll('#module-memory [role=tab]')){tab.click();assert.equal(tab.getAttribute('aria-selected'),'true');}}finally{f.window.happyDOM.abort();}});
test('screen preview and OCR are never advertised as visual model understanding',()=>{const f=fixture();try{const view=f.views[1];view.connection(true);view.receive({type:'mac_center',busy:false,data:{apps:[],windows:[],permissions:{},preview:null,captured_at:null,analysis:null,actions:[],history:[],confirmation:null}});assert.match(f.window.document.getElementById('module-mac-control')!.textContent,/ещё не захватывался/);const button=[...f.window.document.querySelectorAll('#module-mac-control button')].find(b=>b.textContent==='Анализировать текст экрана')!;button.click();assert.equal(f.sent.at(-1).op,'capture');assert.equal(f.sent.at(-1).analyze,true);view.receive({type:'mac_center',busy:false,error:'Нет разрешения Screen Recording'});assert.match(f.window.document.querySelector('#module-mac-control .module-feedback')!.textContent,/Нет разрешения/);}finally{f.window.happyDOM.abort();}});
test('unpaired devices display no invented phone, connection or active sync',()=>{const f=fixture();try{const view=f.views[2];view.connection(true);view.receive({type:'device_sync',busy:false,data:{running:false,url:'',fingerprint:'',invite:null,pending:[],peers:[],categories:{},capabilities:[],history:[],conflicts:[]}});const text=f.window.document.getElementById('module-my-devices')!.textContent!;assert.match(text,/Сервер выключен/);assert.match(text,/Рядом пока только Mac/);assert.doesNotMatch(text,/Онлайн/);}finally{f.window.happyDOM.abort();}});
test('analytics has empty measurements, opt-in policy controls and accessible interactive bars',()=>{const f=fixture();try{const view=f.views[3];view.connection(true);view.receive({type:'analytics',busy:false,data:analytics()});const root=f.window.document.getElementById('module-analytics')!;assert.match(root.textContent,/Сбор данных выключен/);assert.match(root.textContent,/Нет измерений/);assert.doesNotMatch(root.textContent,/NaN|Infinity/);([...root.querySelectorAll('[role=tab]')].find(b=>b.textContent==='Сбор данных')!).click();const enabled=root.querySelector<HTMLInputElement>('input[name=enabled]')!;enabled.checked=true;([...root.querySelectorAll('button')].find(b=>b.textContent==='Сохранить настройки')!).click();assert.equal(f.sent.at(-1).op,'policy');assert.equal(f.sent.at(-1).value.enabled,true);const chart=f.sandbox.exports.barChart([{label:'Пн',value:2,detail:'2 обращения'}],'Обращения');chart.querySelector('button').click();assert.equal(chart.querySelector('button').getAttribute('aria-pressed'),'true');assert.match(chart.querySelector('.chart-detail').textContent,/2 обращения/);}finally{f.window.happyDOM.abort();}});

function syncData(){return {running:false,url:'',fingerprint:'',invite:null,pending:[],peers:[],categories:{memory:'Память'},capabilities:['chat'],history:[],conflicts:[],endpoint:{host:'127.0.0.1',port:8766}};}
function findButton(scope:ParentNode,label:string){const button=[...scope.querySelectorAll('button')].find(b=>b.textContent===label);assert.ok(button,label);return button;}

test('module errors distinguish failed initial load and retry, and late responses cannot replace fresh state',()=>{
  const f=fixture();try{
    const view=f.views[3],root=f.window.document.getElementById('module-analytics')!;
    view.connection(true);const first=f.sent.at(-1).request_id;
    view.receive({type:'analytics',operation_id:first,busy:false,error:'Нет доступа к базе'});
    assert.match(root.textContent!,/Не удалось загрузить данные/);
    findButton(root,'Повторить загрузку').click();const second=f.sent.at(-1).request_id;
    assert.notEqual(first,second);
    view.receive({type:'analytics',operation_id:first,busy:false,data:{...analytics(),summary:{...analytics().summary,count:999}}});
    assert.doesNotMatch(root.textContent!,/999/);
    view.receive({type:'analytics',operation_id:second,busy:false,data:analytics()});
    assert.match(root.textContent!,/За выбранный период данных пока нет/);
    assert.doesNotMatch(root.textContent!,/Не удалось загрузить данные/);
  }finally{f.window.happyDOM.abort();}
});

test('rapid analytics date and device filters merge into the latest queued query and custom dates retain device',()=>{
  const f=fixture();try{
    const view=f.views[3],root=f.window.document.getElementById('module-analytics')!;
    const data={...analytics(),devices:['phone'],query:{period:'today',device:''}};
    view.connection(true);view.receive({type:'analytics',busy:false,data});
    const period=root.querySelector<HTMLSelectElement>('[aria-label="Период аналитики"]')!;
    period.value='week';period.dispatchEvent(new f.window.Event('change'));const week=f.sent.at(-1);
    const device=root.querySelector<HTMLSelectElement>('[aria-label="Устройство аналитики"]')!;
    device.value='phone';device.dispatchEvent(new f.window.Event('change'));
    assert.equal(f.sent.at(-1),week);
    view.receive({type:'analytics',operation_id:week.request_id,busy:false,data:{...data,query:{period:'week'}}});
    assert.equal(f.sent.at(-1).period,'week');assert.equal(f.sent.at(-1).device,'phone');
    const filtered=f.sent.at(-1);view.receive({type:'analytics',operation_id:filtered.request_id,busy:false,data:{...data,query:{period:'week',device:'phone'}}});
    const custom=root.querySelector<HTMLSelectElement>('[aria-label="Период аналитики"]')!;custom.value='custom';custom.dispatchEvent(new f.window.Event('change'));
    const modal=f.window.document.querySelector('dialog')!;
    modal.querySelector<HTMLInputElement>('[name=start]')!.value='2026-11-01';modal.querySelector<HTMLInputElement>('[name=end]')!.value='2026-11-06';
    findButton(modal,'Выбрать').click();assert.equal(f.sent.at(-1).device,'phone');assert.equal(f.sent.at(-1).period,'custom');
  }finally{f.window.happyDOM.abort();}
});

test('module dialogs disable their fields and writes on disconnect and expose backend errors without false success',()=>{
  const f=fixture();try{
    const view=f.views[2],root=f.window.document.getElementById('module-my-devices')!;
    view.connection(true);view.receive({type:'device_sync',busy:false,data:syncData()});
    findButton(root,'Запустить сервер').click();const modal=f.window.document.querySelector('dialog')!;
    view.connection(false);
    assert.ok([...modal.querySelectorAll('input')].every(i=>i.disabled));
    assert.ok([...modal.querySelectorAll<HTMLButtonElement>('button[data-write]')].every(b=>b.disabled));
    assert.equal(findButton(modal,'Закрыть').disabled,false);
    view.connection(true);const refresh=f.sent.at(-1);view.receive({type:'device_sync',operation_id:refresh.request_id,busy:false,data:syncData()});
    const host=modal.querySelector<HTMLInputElement>('[name=host]')!;host.value='127.0.0.1';
    findButton(modal,'Включить защищённое соединение').click();const start=f.sent.at(-1);
    view.receive({type:'device_sync',operation_id:start.request_id,busy:false,error:'Порт занят'});
    assert.match(modal.querySelector('.module-dialog-feedback')!.textContent!,/Порт занят/);
    assert.equal(host.value,'127.0.0.1');assert.doesNotMatch(modal.textContent!,/Сервер запущен/);
  }finally{f.window.happyDOM.abort();}
});

test('pairing wizard updates to show generated QR and stop preserves saved peers',()=>{
  const f=fixture();try{
    const view=f.views[2],root=f.window.document.getElementById('module-my-devices')!;
    view.connection(true);view.receive({type:'device_sync',busy:false,data:syncData()});
    findButton(root,'Запустить сервер').click();const modal=f.window.document.querySelector('dialog')!;
    findButton(modal,'Включить защищённое соединение').click();const start=f.sent.at(-1);
    const running={...syncData(),running:true,url:'https://127.0.0.1:8766',fingerprint:'test-fingerprint'};
    view.receive({type:'device_sync',operation_id:start.request_id,busy:false,data:running});
    findButton(modal,'Создать QR на 2 минуты').click();const invite=f.sent.at(-1);
    const qr={url:'https://127.0.0.1:8766/#pair=fixture-only',qr:'data:image/png;base64,AA==',expires:Date.now()/1000+120};
    view.receive({type:'device_sync',operation_id:invite.request_id,busy:false,data:{...running,invite:qr}});
    assert.equal(modal.open,true);assert.ok(modal.querySelector('img.pair-qr'));
    assert.match(modal.textContent!,/pair=fixture-only/);
    findButton(modal,'Выключить сервер').click();const stop=f.sent.at(-1);
    const peer={id:'phone',name:'Test phone',kind:'iphone',online:false,revoked_at:null,disconnected:0,last_seen:null,last_sync:null,categories:['memory'],capabilities:[]};
    view.receive({type:'device_sync',operation_id:stop.request_id,busy:false,data:{...syncData(),peers:[peer]}});
    assert.match(root.textContent!,/Test phone/);assert.match(root.textContent!,/Сервер выключен/);
    assert.ok(modal.querySelector('[name=host]'));
  }finally{f.window.happyDOM.abort();}
});

test('capture stop remains enabled during work and offline server cannot show online peers',()=>{
  const f=fixture();try{
    const mac=f.views[1];mac.connection(true);mac.receive({type:'mac_center',busy:false,data:{apps:[],windows:[],permissions:{},preview:null,captured_at:null,analysis:null,actions:[],history:[],confirmation:null}});
    const root=f.window.document.getElementById('module-mac-control')!;findButton(root,'Сделать скриншот').click();
    assert.equal(findButton(root,'Остановить захват').disabled,false);findButton(root,'Остановить захват').click();
    assert.equal(f.sent.at(-1).op,'cancel');
    const sync=f.views[2];sync.connection(true);sync.receive({type:'device_sync',busy:false,data:{...syncData(),peers:[{id:'phone',name:'Fixture phone',kind:'ipad',online:true,revoked_at:null,disconnected:0,last_seen:Date.now()/1000,last_sync:null,categories:[],capabilities:[]}]}});
    assert.doesNotMatch(f.window.document.getElementById('module-my-devices')!.textContent!,/iPad · На связи/);
  }finally{f.window.happyDOM.abort();}
});

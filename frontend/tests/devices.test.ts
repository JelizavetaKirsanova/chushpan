/** Exercises standalone controls. No scenario editor, model, audio or device transport. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {Window} from 'happy-dom';
import {DeviceWriter} from '../src/devices_state.ts';
import type {DevicesEvent} from '../src/devices.ts';

const html=readFileSync(new URL('../index.html',import.meta.url),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,'');
const code=stripTypeScriptTypes(readFileSync(new URL('../src/devices.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace('export class DevicesView','class DevicesView')+'\nexports.View=DevicesView;';
function fixture() {
  const window=new Window({settings:{disableJavaScriptEvaluation:true,disableCSSFileLoading:true,disableJavaScriptFileLoading:true}});window.document.write(html);window.confirm=()=>true;
  const requests:Record<string,unknown>[]=[];
  const sandbox={exports:{} as Record<string,unknown>,document:window.document,window,crypto,DeviceWriter,setTimeout,clearTimeout,icon:()=>'<svg></svg>'};vm.runInNewContext(code,sandbox);
  const View=sandbox.exports.View as new(...args:unknown[])=>{connection:(v:boolean)=>void;receive:(v:unknown)=>void};
  const view=new View((v:Record<string,unknown>)=>requests.push(structuredClone(v)));
  const state:DevicesEvent={type:'devices',places:[{id:'general',name:'Общие устройства'},{id:'dorm',name:'Общага'},{id:'home',name:'Дом'}],location:{place_id:'dorm',name:'Общага',mode:'manual',status:'ready'},kinds:[{id:'led_strip',name:'LED-лента',protocol:'ble'},{id:'mac',name:'MacBook',protocol:'local'},{id:'external_display',name:'Внешний дисплей',protocol:'betterdisplay'}],items:[{id:'led-test',revision:1,name:'Свет у стола',kind:'led_strip',protocol:'ble',endpoint:'11111111-1111-4111-8111-111111111111',place_id:'dorm',room:'Спальня',state:{},sent:{},last_contact:null,state_at:null,result_status:null,favorites:[],features:{power_on:true,power_off:true,brightness:true,color:true,colors:['rgb','on','off','brightness'],warm_white:'rgb_approximation',probe:true},connection:'unknown',usable:true,reason:'',busy:false},{id:'mac-local',revision:1,name:'MacBook',kind:'mac',protocol:'local',endpoint:'local-mac',place_id:'general',room:'',state:{brightness:55,volume_percent:30,muted:false,battery:{percent:80,on_power_adapter:true}},sent:{},last_contact:null,state_at:null,result_status:null,favorites:[],features:{volume:true,mute:true,brightness:true,playback:true,battery:true,lock:true},connection:'unknown',usable:true,reason:'',busy:false}]};
  view.connection(true);view.receive(state);
  const button=(label:string,scope:ParentNode=window.document)=>{const b=[...scope.querySelectorAll('button')].find(n=>n.textContent===label);assert.ok(b,label);b.click();return b;};
  const open=(name:string)=>{const b=window.document.querySelector<HTMLButtonElement>(`button[aria-label="Открыть ${name}"]`);assert.ok(b);b.click();};
  return {window,requests,view,state,button,open,close:async()=>{window.document.querySelectorAll('dialog[open]').forEach(d=>(d as unknown as HTMLDialogElement).close());view.connection(false);await window.happyDOM.close();}};
}

test('devices render independently with filters and no raw identifiers on cards',async()=>{
  const f=fixture();try {
    assert.equal(f.window.document.querySelectorAll('.device-tile').length,2);
    assert.doesNotMatch(f.window.document.querySelector('.device-tile')!.textContent!,/11111111|UUID/);
    assert.match(f.window.document.querySelector('.device-tile')!.textContent!,/Подключение не проверено/);
    assert.equal(f.requests.some(r=>r.op==='discover'||r.op==='inspect'),false);
    const filter=f.window.document.getElementById('devices-place-filter') as unknown as HTMLSelectElement;filter.value='dorm';filter.dispatchEvent(new f.window.Event('change'));
    assert.equal(f.window.document.querySelectorAll('.device-tile').length,1);
    const search=f.window.document.querySelector<HTMLInputElement>('[aria-label="Поиск устройств"]')!;search.value='Нет такого';search.dispatchEvent(new f.window.Event('input'));assert.equal(f.window.document.querySelectorAll('.device-tile').length,0);
  } finally {await f.close();}
});

test('absent operation timing is never rendered as undefined milliseconds',async()=>{
  const f=fixture();try{
    f.view.receive({...f.state,metrics:{}});f.open('Свет у стола');
    assert.doesNotMatch(f.window.document.querySelector('.device-panel')!.textContent!,/undefined|NaN/);
    f.view.receive({...f.state,metrics:{total_ms:0}});
    assert.match(f.window.document.querySelector('.device-panel')!.textContent!,/0 мс/);
  }finally{await f.close();}
});

test('LED colour and power are direct typed operations without chat or scenarios',async()=>{
  const f=fixture();try {
    f.open('Свет у стола');const panel=f.window.document.querySelector('.device-panel')!;
    const pink=[...panel.querySelectorAll('button')].find(b=>b.textContent==='Розовый')!;pink.focus();pink.click();const request=f.requests.at(-1)!;
    assert.equal(request.op,'act');assert.equal(request.id,'led-test');assert.equal(request.action,'led');assert.deepEqual(request.parameters,{color:'#FF0020'});
    assert.equal(f.requests.some(r=>'text' in r||r.op==='run'||r.op==='save'),false);
    f.view.receive({...f.state,operation_id:request.request_id as string,result:{result_status:'unverified',message:'Команда отправлена. Физическое состояние неизвестно.'},items:[{...f.state.items[0],sent:{color:'#FF0020'},result_status:'unverified'},f.state.items[1]]});
    assert.equal(f.window.document.querySelector<HTMLInputElement>('[aria-label="Выбрать цвет ленты"]')!.value,'#ff0020');
    assert.match(f.window.document.querySelector('.device-panel .device-last-state')!.textContent!,/Розовый.*не подтверждён/);
    f.button('Выключить',f.window.document.querySelector('.device-panel')!);assert.deepEqual(f.requests.at(-1)!.parameters,{power:false});
    assert.match(f.window.document.querySelector('.device-panel')!.textContent!,/RGB-имитация/);
  } finally {await f.close();}
});

test('sliders coalesce input and send final value when released',async()=>{
  const f=fixture();try {
    f.open('Свет у стола');const range=f.window.document.querySelector<HTMLInputElement>('[aria-label="Яркость ленты"]')!;
    for(let i=0;i<=100;i++){range.value=String(i);range.dispatchEvent(new f.window.Event('input'));}
    assert.equal(f.requests.filter(r=>r.op==='act').length,0);
    range.dispatchEvent(new f.window.Event('change'));assert.equal(f.requests.filter(r=>r.op==='act').length,1);assert.deepEqual(f.requests.at(-1)!.parameters,{brightness:100});
    range.value='37';range.dispatchEvent(new f.window.Event('input'));range.dispatchEvent(new f.window.Event('change'));assert.equal(f.requests.filter(r=>r.op==='act').length,1);
    const first=f.requests.at(-1)!;f.view.receive({...f.state,operation_id:first.request_id as string,result:{result_status:'unverified',message:'Отправлено'}});
    assert.equal(f.requests.filter(r=>r.op==='act').length,2);assert.deepEqual(f.requests.at(-1)!.parameters,{brightness:37});
  } finally {await f.close();}
});

test('metadata and favorite colors persist with optimistic revision',async()=>{
  const f=fixture();try {
    f.open('Свет у стола');f.button('В любимые',f.window.document.querySelector('.device-panel')!);assert.equal(f.requests.at(-1)!.op,'favorites');assert.deepEqual(f.requests.at(-1)!.colors,['#FFFFFF']);
    const form=f.window.document.querySelector('.device-advanced form')!;const input=form.querySelector('input')!;input.value='Свет в спальне';form.querySelector('select')!.value='home';form.dispatchEvent(new f.window.Event('submit',{cancelable:true}));
    assert.equal(f.requests.at(-1)!.op,'update');assert.equal(f.requests.at(-1)!.name,'Свет в спальне');assert.equal(f.requests.at(-1)!.place_id,'home');assert.equal(f.requests.at(-1)!.revision,1);
    f.button('Удалить устройство…');assert.equal(f.requests.at(-1)!.op,'delete');
  } finally {await f.close();}
});

test('wizard scans only by explicit click and allows honest manual addition',async()=>{
  const f=fixture();try {
    f.button('Добавить устройство');f.button('LED-лента',f.window.document.querySelector('dialog')!);
    assert.equal(f.requests.some(r=>r.op==='discover'),false);f.button('Найти устройства',f.window.document.querySelector('dialog')!);assert.equal(f.requests.at(-1)!.op,'discover');
    f.view.receive({...f.state,candidates:[{token:'found-token',name:'ELK-BLEDOB'}]});f.button('ELK-BLEDOB');f.button('Проверить подключение');assert.equal(f.requests.at(-1)!.op,'check');assert.equal(f.requests.at(-1)!.token,'found-token');
    f.view.receive({...f.state,checked:{message:'GATT ответил. Функции не подтверждены.'}});f.button('Далее');
    const form=f.window.document.querySelector('dialog form')!;form.querySelector('input')!.value='Свет у окна';form.querySelector('select')!.value='dorm';form.dispatchEvent(new f.window.Event('submit',{cancelable:true}));
    assert.equal(f.requests.at(-1)!.op,'add');assert.equal(f.requests.at(-1)!.name,'Свет у окна');assert.equal(f.requests.at(-1)!.place_id,'dorm');
  } finally {await f.close();}
});

test('unknown physical states, unavailable functions and disconnect remain honest',async()=>{
  const f=fixture();try {
    f.view.receive({...f.state,items:[{...f.state.items[0],features:{power_on:false,power_off:false,brightness:false,color:false,colors:[],probe:true},usable:false,reason:'Устройство в другом городе'}]});f.open('Свет у стола');
    assert.equal(f.window.document.querySelector<HTMLInputElement>('[aria-label="Выбрать цвет ленты"]')!.disabled,true);
    assert.equal(f.window.document.querySelector('[aria-label="Яркость ленты"]'),null);
    assert.match(f.window.document.querySelector('.device-panel')!.textContent!,/другом городе/);
    f.view.connection(false);assert.ok([...f.window.document.querySelectorAll('button')].filter(b=>b.closest('#devices-root')).every(b=>b.disabled));
  } finally {await f.close();}
});

test('Mac controls include real lock confirmation without sleep or shutdown buttons',async()=>{
  const f=fixture();try {
    f.open('MacBook');const panel=f.window.document.querySelector('.device-panel')!;assert.match(panel.textContent!,/Батарея 80%/);assert.doesNotMatch(panel.textContent!,/Выключить Mac|Сон компьютера/);
    f.button('Заблокировать экран…',panel);const request=f.requests.at(-1)!;assert.equal(request.action,'lock');
    f.view.receive({...f.state,operation_id:request.request_id as string,result:{status:'confirmation_required',result_status:'requires_confirmation',message:'Подтверждаю 1234'}});
    const code=f.window.document.querySelector<HTMLInputElement>('.device-panel input[inputmode="numeric"]')!;code.value='1234';f.button('Подтвердить код');assert.equal(f.requests.at(-1)!.op,'confirm');assert.equal(f.requests.at(-1)!.code,'1234');
  } finally {await f.close();}
});

test('writer cancels queued changes on errors and disconnect, without automatic replay',async()=>{
  const requests:Record<string,unknown>[]=[];const writer=new DeviceWriter(v=>requests.push(v as Record<string,unknown>),()=>{},5);
  writer.offer('led','led',{brightness:20},true);writer.offer('led','led',{brightness:30},true);assert.equal(requests.length,1);
  writer.complete(requests[0].request_id as string,false);assert.equal(requests.length,1);
  writer.offer('led','led',{brightness:40});writer.disconnect();await new Promise(resolve=>setTimeout(resolve,15));assert.equal(requests.length,1);
});

test('favorites and room groups derive solely from registered records',async()=>{
  const f=fixture();try {
    f.view.receive({...f.state,items:[{...f.state.items[0],favorite:true},f.state.items[1]]});
    assert.equal(f.window.document.querySelectorAll('.device-favorite-tile').length,1);
    assert.equal(f.window.document.querySelectorAll('.device-tile').length,2);
    assert.match(f.window.document.querySelector('.device-group summary')!.textContent!,/Спальня.*Общага/);
    const star=f.window.document.querySelector<HTMLButtonElement>('[aria-label="Убрать из избранного: Свет у стола"]')!;star.click();
    assert.equal(f.requests.at(-1)!.op,'update');assert.equal(f.requests.at(-1)!.favorite,false);
  }finally{await f.close();}
});

test('AirPlay detail replaces LED controls and never implies confirmed playback',async()=>{
  const f=fixture();try{
    const receiver={...f.state.items[0],id:'airplay-test',kind:'airplay' as const,name:'Приёмник',protocol:'bonjour',features:{airplay_audio:true,airplay_video:true,playback:false,volume:false},favorite:false};
    f.view.receive({...f.state,items:[f.state.items[0],receiver]});f.open('Свет у стола');f.open('Приёмник');
    const panel=f.window.document.querySelector('.device-panel')!;assert.equal(panel.querySelector('input[type=color]'),null);assert.equal(panel.querySelector('input[type=range]'),null);
    f.button('Выбрать AirPlay-аудио в macOS',panel);assert.equal(f.requests.at(-1)!.action,'airplay_audio');
    assert.match(panel.textContent!,/подключение не подтверждено/);assert.doesNotMatch(panel.textContent!,/Трансляция запущена/);
  }finally{await f.close();}
});

test('audio menu waits for system state before marking a selection',async()=>{
  const f=fixture();try{
    f.view.receive({...f.state,outputs:[{uid:'mac',name:'Динамики Mac',active:true,transport:'builtin'},{uid:'headphones',name:'Наушники',active:false,transport:'bluetooth'}]});
    f.window.document.getElementById('devices-audio-output')!.click();assert.equal(f.requests.at(-1)!.op,'audio_refresh');
    const choices=f.window.document.querySelectorAll<HTMLButtonElement>('.device-audio-choice');choices[1].click();assert.equal(f.requests.at(-1)!.uid,'headphones');
    assert.equal(choices[0].getAttribute('aria-pressed'),'true');assert.equal(choices[1].getAttribute('aria-pressed'),'false');
    f.view.receive({...f.state,outputs:[{uid:'mac',name:'Динамики Mac',active:false,transport:'builtin'},{uid:'headphones',name:'Наушники',active:true,transport:'bluetooth'}]});
    assert.equal(f.window.document.querySelectorAll('.device-audio-choice')[1].getAttribute('aria-pressed'),'true');
  }finally{await f.close();}
});

test('groups issue one registry operation and unknown power stays unknown',async()=>{
  const f=fixture();try{
    f.view.receive({...f.state,groups:[{id:'lights',revision:1,name:'Весь свет',place_id:'dorm',device_ids:['led-test']}]});
    assert.equal(f.window.document.querySelector('.device-tile [role=switch]'),null);
    f.button('Группы');f.button('Выключить');assert.equal(f.requests.at(-1)!.op,'act_group');assert.equal(f.requests.at(-1)!.id,'lights');assert.deepEqual(f.requests.at(-1)!.parameters,{power:false});
  }finally{await f.close();}
});

test('device history is requested on demand and displays factual outcomes',async()=>{
  const f=fixture();try{
    f.open('Свет у стола');f.button('История',f.window.document.querySelector('.device-panel')!);assert.equal(f.requests.at(-1)!.op,'history');
    f.view.receive({...f.state,history:{id:'led-test',items:[{at:'2026-11-04 12:00:00',action:'led',result_status:'unverified',elapsed_ms:250}]}});
    assert.match(f.window.document.querySelector('.device-history')!.textContent!,/Физический результат не подтверждён/);
  }finally{await f.close();}
});

test('opening location settings and search performs no discovery',async()=>{
  const f=fixture();try{
    f.window.document.querySelector<HTMLButtonElement>('.device-location-settings')!.click();assert.match(f.window.document.querySelector('dialog[open]')!.textContent!,/Ручной выбор имеет приоритет/);
    f.button('×',f.window.document.querySelector('dialog[open]')!);
    f.window.document.querySelector<HTMLButtonElement>('[aria-label="Открыть поиск устройств"]')!.click();assert.match(f.window.document.querySelector('.device-search-results')!.textContent!,/Свет у стола/);
    assert.equal(f.requests.some(r=>r.op==='discover'||r.op==='inspect'||r.op==='system_audio'),false);
  }finally{await f.close();}
});

test('BLE discovery shows signal and unknown group, connects explicitly and ignores late results',async()=>{
  const f=fixture();try{
    f.button('Найти устройства');const scan=f.requests.at(-1)!;
    assert.equal(scan.op,'discover');assert.equal(scan.kind,'led_strip');
    let dialog=f.window.document.querySelector('dialog')!;
    assert.match(dialog.querySelector('[role=status]')!.textContent!,/Сканирование Bluetooth LE/);
    assert.equal(dialog.querySelector('[aria-busy=true]')!.getAttribute('aria-busy'),'true');
    f.view.receive({...f.state,operation_id:scan.request_id as string,candidates:[
      {token:'elk',name:'ELK-BLEDDM 39',kind:'led_strip',protocol:'ble',rssi:-58,compatibility:'candidate',compatibility_reason:'Семейство ELK BLE; протокол требует проверки.'},
      {token:'unknown',name:'Sensor',kind:'led_strip',protocol:'ble',rssi:-82,compatibility:'unknown',compatibility_reason:'Совместимость не определена.'}
    ]});
    assert.equal(dialog.querySelectorAll('.device-discovery-row').length,2);
    assert.match(dialog.textContent!,/−?\-58 dBm/);assert.match(dialog.textContent!,/Bluetooth \(BLE\)/);
    assert.match(dialog.querySelectorAll('.device-discovery-group')[1].textContent!,/Неизвестные.*Sensor.*не определена/s);
    assert.equal(f.requests.some(r=>r.op==='check'||r.op==='probe_led'||r.op==='act'),false);
    f.button('Повторить поиск',dialog);const second=f.requests.at(-1)!;
    f.view.receive({...f.state,operation_id:scan.request_id as string,candidates:[]});
    assert.ok(dialog.querySelector('[aria-busy=true]'));
    f.view.receive({...f.state,operation_id:second.request_id as string,candidates:[{token:'elk2',name:'ELK-BLEDDM 39',kind:'led_strip',rssi:-58,compatibility:'candidate'}]});
    dialog.querySelector<HTMLButtonElement>('[aria-label="Подключить ELK-BLEDDM 39"]')!.click();
    const check=f.requests.at(-1)!;assert.equal(check.op,'check');assert.equal(check.token,'elk2');
    f.view.receive({...f.state,operation_id:check.request_id as string,error:'FFF3 не найден. Команды не отправлял.'});
    assert.match(dialog.textContent!,/FFF3 не найден/);assert.equal(f.requests.some(r=>r.op==='act'||r.op==='probe_led'),false);
    f.button('Проверить подключение',dialog);assert.equal(f.requests.at(-1)!.op,'check');
    assert.doesNotMatch(dialog.textContent!,/FFF3 не найден/);
  }finally{await f.close();}
});

test('scan errors remain visible and cancelling rejects stale scan responses',async()=>{
  const f=fixture();try{
    f.button('Найти устройства');const first=f.requests.at(-1)!;const dialog=f.window.document.querySelector('dialog')!;
    f.view.receive({...f.state,operation_id:first.request_id as string,error:'Bluetooth выключен. Включи его.'});
    assert.match(dialog.textContent!,/Bluetooth выключен/);assert.equal(dialog.querySelector('[aria-busy=true]'),null);
    f.button('Найти устройства',dialog);const second=f.requests.at(-1)!;f.button('Остановить поиск',dialog);
    assert.equal(f.requests.at(-1)!.op,'cancel');assert.equal(f.requests.at(-1)!.operation_id,second.request_id);
    f.view.receive({...f.state,operation_id:second.request_id as string,candidates:[{token:'late',name:'Late sensor'}]});
    assert.doesNotMatch(dialog.textContent!,/Late sensor/);
  }finally{await f.close();}
});

test('automatic connection checks restore opted-in local strips sequentially without control writes',async()=>{
  const f=fixture();try{
    const first={...f.state.items[0],auto_connect:true};const second={...first,id:'led-second',name:'Вторая лента'};
    const remote={...first,id:'remote',usable:false,place_id:'home'};
    const state={...f.state,items:[first,second,remote]};f.view.receive(state);
    const a=f.requests.at(-1)!;assert.equal(a.op,'inspect');assert.equal(a.id,first.id);
    f.view.receive(state);assert.equal(f.requests.filter(r=>r.op==='inspect').length,1);
    f.view.receive({...state,operation_id:a.request_id as string,result:{message:'GATT ответил.',result_status:'unverified'}});
    const b=f.requests.at(-1)!;assert.equal(b.op,'inspect');assert.equal(b.id,second.id);
    f.view.receive({...state,operation_id:b.request_id as string,error:'Нет соединения.'});
    f.view.receive(state);assert.equal(f.requests.filter(r=>r.op==='inspect').length,2);
    assert.equal(f.requests.some(r=>r.op==='act'||r.op==='probe_led'),false);
    f.view.connection(false);f.view.connection(true);f.view.receive(state);
    assert.equal(f.requests.filter(r=>r.op==='inspect').length,3);
    assert.equal(f.requests.at(-1)!.id,first.id);
  }finally{await f.close();}
});

test('control diagnostic only sends one chosen frame after explicit consent',async()=>{
  const f=fixture();try{
    f.view.receive({...f.state,items:[{...f.state.items[0],features:{probe:true,colors:[],color:false,power_on:false,power_off:false,brightness:false}}]});
    f.open('Свет у стола');f.button('Проверить управление');assert.equal(f.requests.some(r=>r.op==='probe_led'),false);
    assert.equal(f.window.document.querySelector<HTMLInputElement>('[aria-label="Выбрать цвет ленты"]')!.disabled,true);
    f.button('Тест: Красный');const request=f.requests.at(-1)!;
    assert.equal(request.op,'probe_led');assert.deepEqual(request.parameters,{color:'#FF0000'});assert.equal(request.consent,true);
    f.view.receive({...f.state,operation_id:request.request_id as string,result:{probe_id:'observation',device_id:'led-test',message:'Проверь ленту.'}});
    f.button('Да, вижу результат');assert.equal(f.requests.at(-1)!.op,'confirm_probe');assert.equal(f.requests.at(-1)!.probe_id,'observation');
    assert.equal(f.requests.filter(r=>r.op==='probe_led').length,1);
  }finally{await f.close();}
});

test('all BLE diagnostic mode renders every candidate across place and text filters with fresh scan IDs',async()=>{
  const f=fixture();try{
    const filter=f.window.document.getElementById('devices-place-filter') as unknown as HTMLSelectElement;filter.value='home';filter.dispatchEvent(new f.window.Event('change'));
    const search=f.window.document.querySelector<HTMLInputElement>('[aria-label="Поиск устройств"]')!;search.value='unrelated';search.dispatchEvent(new f.window.Event('input'));
    f.button('Найти устройства');const first=f.requests.at(-1)!;const dialog=f.window.document.querySelector('dialog')!;
    f.view.receive({...f.state,operation_id:first.request_id as string,candidates:[]});
    const all=dialog.querySelector<HTMLInputElement>('input[type=checkbox]')!;assert.equal(all.disabled,false);all.checked=true;all.dispatchEvent(new f.window.Event('change'));
    f.button('Найти устройства',dialog);const next=f.requests.at(-1)!;
    assert.equal(next.show_all_ble,true);assert.notEqual(first.request_id,next.request_id);
    assert.equal(next.kind,'led_strip');
    const discovery={scan_id:next.request_id as string,started_at:'2026-11-06T19:00:00Z',finished_at:'2026-11-06T19:00:04Z',show_all_ble:true,scanner_count:2,candidate_count:2,excluded_count:0};
    f.view.receive({...f.state,operation_id:next.request_id as string,discovery,candidates:[{token:'elk',name:'ELK-BLEDDM 39',compatibility:'unknown',rssi:-62},{token:'sensor',name:'Sensor',compatibility:'unknown',rssi:-95}]});
    assert.equal(dialog.querySelectorAll('.device-discovery-row').length,2);
    assert.match(dialog.textContent!,/Все Bluetooth LE устройства.*ELK-BLEDDM 39.*Sensor/s);
    assert.match(dialog.textContent!,/BLE: 2 → передано: 2 · исключено: 0/);
    assert.deepEqual(f.requests.slice(-2).map(r=>[r.op,r.stage,r.count]),[['scan_trace','frontend_received',2],['scan_trace','frontend_rendered',2]]);
    f.button('Повторить поиск',dialog);const repeat=f.requests.at(-1)!;assert.notEqual(repeat.request_id,next.request_id);
    assert.equal(dialog.querySelectorAll('.device-discovery-row').length,0);
    f.view.receive({...f.state,operation_id:next.request_id as string,discovery,candidates:[{token:'late',name:'Stale result'}]});
    assert.equal(f.requests.at(-1)!.stage,'frontend_ignored');assert.doesNotMatch(dialog.textContent!,/Stale result/);
  }finally{await f.close();}
});

test('BLE diagnostics expose driver and GATT while sending no control command',async()=>{
  const f=fixture();try {
    f.state.items[0].ble={driver:'ELK-BLEDDM · кандидат',driver_id:'elk-bleddm-candidate',compatibility:'gatt_match',confirmed_functions:[],diagnostic:{report_id:'report',connection:'disconnected',services:[{uuid:'service-gatt',characteristics:[{uuid:'write-gatt',properties:['write'],write_modes:['response'],read:{error:'TimeoutError'}}]}],errors:[{stage:'read',type:'TimeoutError'}],notifications:[]}};
    f.view.receive(f.state);f.open('Свет у стола');const panel=f.window.document.querySelector('.device-panel')!;
    assert.match(panel.textContent!,/Драйвер: ELK-BLEDDM/);assert.match(panel.textContent!,/GATT совпал; функции требуют наблюдения/);assert.match(panel.textContent!,/write-gatt/);assert.match(panel.textContent!,/TimeoutError/);
    f.button('Диагностика',panel);assert.equal(f.requests.at(-1)!.op,'diagnose_ble');assert.equal(f.requests.at(-1)!.id,'led-test');
    assert.equal(f.requests.some(r=>r.op==='act'||r.op==='probe_led'),false);
    f.button('Выбрать профиль',panel);assert.equal(f.requests.at(-1)!.op,'ble_profile');assert.equal(f.requests.at(-1)!.consent,true);
  } finally {await f.close();}
});

test('a probe response stays bound to its device when selection changes',async()=>{
  const f=fixture();try {
    f.open('Свет у стола');f.open('MacBook');
    f.view.receive({...f.state,result:{probe_id:'old-led-probe',device_id:'led-test',message:'Проверь ленту.'}});
    assert.equal([...f.window.document.querySelectorAll('.device-panel button')].some(b=>b.textContent==='Да, вижу результат'),false);
    f.open('Свет у стола');f.button('Да, вижу результат');assert.equal(f.requests.at(-1)!.id,'led-test');assert.equal(f.requests.at(-1)!.probe_id,'old-led-probe');
  } finally {await f.close();}
});

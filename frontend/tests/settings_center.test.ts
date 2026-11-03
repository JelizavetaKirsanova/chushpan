import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {Window} from 'happy-dom';
import type {SettingsState} from '../src/settings_center.ts';

const html=readFileSync(new URL('../index.html',import.meta.url),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,'');
const code=stripTypeScriptTypes(readFileSync(new URL('../src/settings_center.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace('export class SettingsCenterUI','class SettingsCenterUI')+'\nexports.View=SettingsCenterUI;';
function fixture() {
  const window=new Window({settings:{disableJavaScriptEvaluation:true,disableCSSFileLoading:true,disableJavaScriptFileLoading:true}});
  window.document.write(html);window.confirm=()=>true;
  const requests:Record<string,unknown>[]=[];
  const sandbox={exports:{} as Record<string,unknown>,document:window.document,window,structuredClone,crypto,Blob,URL,setTimeout:()=>0,icon:()=>'<svg aria-hidden="true"></svg>'};
  vm.runInNewContext(code,sandbox);
  const View=sandbox.exports.View as new (...args:unknown[])=>{connection:(v:boolean)=>void;receive:(v:unknown)=>void;open:(v:string)=>void};
  const view=new View((v:Record<string,unknown>)=>requests.push(structuredClone(v)),(v:Record<string,unknown>)=>requests.push(structuredClone(v)),()=>{});
  const traitEntries=[['sarcasm','Сарказм','Стиль'],['profanity','Мат','Стиль'],['humor','Юмор','Стиль'],['boldness','Дерзость','Стиль'],['emotionality','Эмоциональность','Общение'],['verbosity','Болтливость','Общение'],['care','Заботливость','Общение'],['formality','Формальность','Общение'],['initiative','Инициативность','Поведение'],['energy','Энергичность','Поведение'],['directness','Прямолинейность','Поведение'],['curiosity','Любознательность','Поведение']];
  const profile={id:'mine',revision:1,name:'Мой характер',description:'Локальный профиль',builtin:false,traits:Object.fromEntries(traitEntries.map(([key])=>[key,50])),communication:{address:'ты',preferred_addresses:[],unwanted_addresses:[],favorite_expressions:[],forbidden_expressions:[],jokes:'normal',allow_profanity:true,response_style:'adaptive',use_context:true,errors:'direct',irritation:'calm',serious:'supportive'},instructions:'',technical_detail:70};
  const state:SettingsState={type:'settings_center',version:1,profiles:[profile,{...structuredClone(profile),id:'calm',name:'Спокойный помощник',builtin:true}],active:{id:'mine',reason:'Выбран вручную',locked:false},traits:Object.fromEntries(traitEntries.map(([key,name,group])=>[key,{name,group}])),values:{
    studio:{voice:'eugene',rate:'medium',pitch:'medium',volume:100,pause_ms:0,pronunciations:{},normalize_numbers:true,normalize_english:true,wake_phrase:'эй чушпан',silence_ms:650,adaptive_endpoint:true,text_only:false},
    memory:{automatic:'auto',excluded:[],temporary_days:30},models:{provider:'ollama',local_model:'qwen3:8b',cloud_model:'gemini-3.8-flash',cloud_fallback:'gemini-3.5-flash',fallback:true,mode:'balanced'},access:{disabled_tools:[],blocked_apps:[]},quiet:{enabled:false,start:'23:00',end:'07:00',weekdays:[0,1,2,3,4,5,6],mute:true,pause_initiative:true},notifications:{reminders:true,scenarios:false},about:{name:'Пользовательница',address:'',preferences:''}},
    memory:[],memory_pending:[],habits_opt_in:false,engine:'silero',voices:['eugene','aidar'],voice_support:{rate:true,pitch:true,expression:false},permissions:null,models:{installed:['qwen3:8b']},location:{name:'Общий',mode:'auto'},places:[{id:'home',revision:1,name:'Дом',cities:[]}],signals:{signals:[{kind:'wifi',key:'a'.repeat(64),name:'Тестовая сеть'}]},rules:[],integrations:[],actions:[],recent_actions:[],last_errors:[]};
  view.connection(true);view.receive(state);
  const button=(label:string)=>{const found=[...window.document.querySelectorAll('button')].find(b=>b.textContent===label);assert.ok(found,label);found.click();return found;};
  return {window,view,state,requests,button};
}

test('all eight categories open separately and back preserves legacy forms',async()=>{
  const f=fixture();try {
    const pages=['personality','voice','memory','models','access','places','integrations','advanced'];
    for(const page of pages){f.view.open(page);assert.equal(f.window.document.getElementById('settings-home')!.hidden,true);assert.ok(f.window.document.getElementById('settings-content')!.childElementCount);f.button('‹ Все настройки');assert.equal(f.window.document.getElementById('settings-home')!.hidden,false);}
    f.view.open('voice');assert.ok(f.window.document.getElementById('settings-content')!.contains(f.window.document.getElementById('voice-settings')));
    f.view.open('advanced');assert.ok(f.window.document.getElementById('settings-content')!.contains(f.window.document.getElementById('ambient-settings')));
    assert.equal(f.window.document.querySelectorAll('#voice-settings').length,1);
  } finally {await f.window.happyDOM.abort();}
});

test('character sliders are grouped and save complete independent profile without activating it',async()=>{
  const f=fixture();try {
    f.view.open('personality');assert.equal(f.window.document.querySelectorAll('.personality-sliders:not([hidden]) input').length,4);
    const slider=f.window.document.getElementById('personality-sarcasm') as unknown as HTMLInputElement;slider.value='83';slider.dispatchEvent(new f.window.Event('input'));
    const instructions=f.window.document.getElementById('personality-instructions') as unknown as HTMLTextAreaElement;instructions.value='Кратко и с юмором.';instructions.dispatchEvent(new f.window.Event('input'));
    f.window.document.getElementById('personality-editor')!.dispatchEvent(new f.window.Event('submit',{cancelable:true}));
    const request=f.requests.at(-1)!;assert.equal(request.op,'profile_save');const p=request.profile as typeof f.state.profiles[0];assert.equal(p.traits.sarcasm,83);assert.equal(p.instructions,'Кратко и с юмором.');assert.equal(f.state.profiles[0].traits.sarcasm,50);assert.equal(f.state.active.id,'mine');
    assert.equal(f.requests.some(r=>r.op==='profile_select'),false);
  } finally {await f.window.happyDOM.abort();}
});

test('unsaved profile survives an error and preview compares drafts without changing selection',async()=>{
  const f=fixture();try {
    f.view.open('personality');const text=f.window.document.getElementById('personality-instructions') as unknown as HTMLTextAreaElement;text.value='Моя инструкция';text.dispatchEvent(new f.window.Event('input'));
    f.view.receive({...f.state,error:'Хранилище недоступно'});assert.equal((f.window.document.getElementById('personality-instructions') as unknown as HTMLTextAreaElement).value,'Моя инструкция');
    const labels=[...f.window.document.querySelectorAll('#settings-content label')];const compare=labels.find(l=>l.firstElementChild?.textContent==='Сравнить с профилем')!.querySelector('select')!;compare.value='calm';f.button('Проверить характер');const request=f.requests.at(-1)!;assert.equal(request.op,'preview');assert.equal((request.drafts as unknown[]).length,2);assert.equal(f.state.active.id,'mine');
    f.view.receive({type:'settings_preview',id:'mine',name:'Мой характер',text:'Настоящий потоковый фрагмент.',model:'test',done:false});assert.match(f.window.document.getElementById('settings-preview-results')!.textContent,/потоковый фрагмент/);
  } finally {await f.window.happyDOM.abort();}
});

test('voice studio saves actual prosody and pronunciation without replacing engine',async()=>{
  const f=fixture();try {
    f.view.open('voice');const form=f.window.document.querySelector('form[data-category="studio"]')!;
    (form.querySelector('[data-field="rate"]') as unknown as HTMLSelectElement).value='fast';
    const dictionaries=form.querySelectorAll('textarea');dictionaries[0].value='Safari = Сафари';dictionaries[1].value='Чушпан = Чушпа́н';
    form.dispatchEvent(new f.window.Event('submit',{cancelable:true}));const request=f.requests.at(-1)!;assert.equal(request.op,'save');assert.equal(request.category,'studio');assert.equal((request.value as Record<string,unknown>).rate,'fast');assert.equal((request.value as {pronunciations:Record<string,string>}).pronunciations.Safari,'Сафари');assert.equal(f.state.engine,'silero');
  } finally {await f.window.happyDOM.abort();}
});

test('place binding requires explicit save and keeps manual mode separate',async()=>{
  const f=fixture();try {
    f.view.open('places');const wifi=[...f.window.document.querySelectorAll('#settings-content label')].find(l=>l.textContent.includes('Wi-Fi: Тестовая сеть'))!.querySelector('input')!;wifi.checked=true;wifi.dispatchEvent(new f.window.Event('change'));
    assert.equal(f.requests.some(r=>r.op==='place_save'),false);f.button('Сохранить место и привязки');const request=f.requests.at(-1)!;assert.equal(request.op,'place_save');assert.equal((request.place as {signals:unknown[]}).signals.length,1);assert.equal(f.state.places[0].signals,undefined);
  } finally {await f.window.happyDOM.abort();}
});

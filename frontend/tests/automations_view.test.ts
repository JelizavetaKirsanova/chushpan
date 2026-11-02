/** Exercises the production editor's DOM event handlers; no browser, network or devices. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {Window} from 'happy-dom';
import * as state from '../src/automation_state.ts';

const source=readFileSync(new URL('../src/automations.ts',import.meta.url),'utf8');
const html=readFileSync(new URL('../index.html',import.meta.url),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,'');
const code=stripTypeScriptTypes(source,{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export \{.*\} from .*;\n/gm,'').replace('export class AutomationsView','class AutomationsView')+'\nexports.AutomationsView=AutomationsView;';
function fixture() {
  const window=new Window({url:'http://127.0.0.1:8879', settings:{disableJavaScriptEvaluation:true,disableCSSFileLoading:true,disableJavaScriptFileLoading:true}});
  window.document.write(html); window.confirm=()=>true;
  window.HTMLElement.prototype.scrollIntoView=()=>{};
  const requests: Record<string,unknown>[]=[];
  const sandbox={...state,exports:{} as Record<string,unknown>,document:window.document,window,structuredClone,crypto,icon:()=>'<svg aria-hidden="true"></svg>'};
  vm.runInNewContext(code,sandbox);
  const View=sandbox.exports.AutomationsView as new (send:(v:Record<string,unknown>)=>void,...args:unknown[])=>{connection:(v:boolean)=>void;receive:(v:unknown)=>void};
  const view=new View(value=>requests.push(structuredClone(value)),()=>{},()=>{},()=>{});
  view.connection(true);
  const data={type:'automations',scenarios:[] as state.Scenario[],capabilities:[{id:'mac.brightness',name:'Яркость MacBook',device:'mac',description:'Test transport',parameters:{level:{type:'integer',minimum:0,maximum:100}},required:['level'],permissions:[],risk:'state_change',availability:'not_checked'}],runs:[],training:[],devices:[],places:[{id:'dorm',revision:1,name:'Общага',cities:[]},{id:'home',revision:1,name:'Дом',cities:[]}],location:{place_id:'dorm',name:'Общага',mode:'manual'}};
  view.receive(data);
  function button(text:string, root=window.document as unknown as ParentNode) {
    const found=[...root.querySelectorAll('button')].find(node=>node.textContent===text);
    assert.ok(found,`Button ${text} exists`); assert.equal((found as HTMLButtonElement).disabled,false,`Button ${text} enabled`);
    (found as HTMLButtonElement).click();
  }
  function field(text:string) {
    const found=[...window.document.querySelectorAll('label')].find(label=>label.firstElementChild?.textContent===text)?.querySelector('input,select,textarea');
    assert.ok(found,`Field ${text} exists`);return found as unknown as HTMLInputElement;
  }
  function fill(text:string,value:string) {const input=field(text);input.value=value;input.dispatchEvent(new window.Event('input',{bubbles:true}) as unknown as Event);}
  function submit() {window.document.querySelector('.scenario-editor form')!.dispatchEvent(new window.Event('submit',{cancelable:true,bubbles:true}));}
  return {window,view,data,requests,button,field,fill,submit};
}

test('real editor creates one command, common action and independently editable location copies with safe preview', async()=>{
  const f=fixture();try {
    f.button('Новая команда'); f.fill('Название','Я спать');f.button('Добавить фразу');
    const phrase=f.window.document.querySelector('[aria-label="Фраза 1"]')! as unknown as HTMLInputElement;
    phrase.value='Пора ложиться';phrase.dispatchEvent(new f.window.Event('input',{bubbles:true}) as unknown as Event);
    f.button('+ Действие');f.fill('Уровень, % *','10');
    f.button('Добавить пустую версию');
    f.button('+ Действие');f.fill('Уровень, % *','25');
    f.button('Скопировать версию в другое место');f.fill('Уровень, % *','40');
    f.button('Безопасный тест черновика');
    const preview=f.requests.at(-1)!;assert.equal(preview.op,'preview');
    const draft=preview.scenario as state.Scenario;
    assert.equal(draft.name,'Я спать');assert.equal(draft.phrases.length,1);assert.equal(draft.variants.length,2);
    assert.equal(draft.actions[0].parameters.level,10);
    assert.equal(draft.variants.find(v=>v.place_id==='dorm')!.actions[0].parameters.level,25);
    assert.equal(draft.variants.find(v=>v.place_id==='home')!.actions[0].parameters.level,40);
    assert.equal(preview.variant,draft.variants[1].id);
    assert.equal(f.requests.some(r=>r.op==='run'),false);
    f.submit();assert.equal(f.requests.at(-1)!.op,'save');
    const saved={...(f.requests.at(-1)!.scenario as state.Scenario),id:'a'.repeat(32),revision:1};
    f.view.receive({...f.data,scenarios:[saved],saved:saved.id});
    f.fill('Название','Черновое имя');f.button('Отменить изменения');assert.equal(f.field('Название').value,'Я спать');
    f.button('Дом');f.button('Удалить версию');f.submit();
    const edited=f.requests.at(-1)!.scenario as state.Scenario;
    assert.equal(edited.variants.length,1);assert.equal(edited.variants[0].place_id,'dorm');
  }finally{await f.window.happyDOM.close();}
});

test('applied command is searchable, deletable and recoverable using backend revision contract', async()=>{
  const f=fixture();try {
    f.button('Новая команда');f.fill('Название','Тихий вечер');f.submit();
    const saved={...(f.requests.at(-1)!.scenario as state.Scenario),id:'b'.repeat(32),revision:7};
    f.view.receive({...f.data,scenarios:[saved],saved:saved.id});
    f.button('Удалить команду');assert.deepEqual(f.requests.at(-1),{op:'delete',id:saved.id,revision:7});
    f.view.receive({...f.data,deleted:saved.id});f.button('Отменить удаление команды');
    assert.deepEqual(f.requests.at(-1),{op:'restore',id:saved.id,from_revision:7,expected_revision:0});
    f.view.receive({...f.data,scenarios:[saved],saved:saved.id});
    const search=f.window.document.querySelector('[aria-label="Поиск моих команд"]')! as unknown as HTMLInputElement;
    search.value='Нет такой';search.dispatchEvent(new f.window.Event('input') as unknown as Event);
    assert.match(f.window.document.querySelector('.scenario-list')!.textContent,/Команд не найдено/);
  }finally{await f.window.happyDOM.close();}
});

test('step disabling, optional device and retained settings are submitted through actual controls', async()=>{
  const f=fixture();try {
    f.button('Новая команда');f.button('+ Действие');f.fill('Уровень, % *','35');
    const optional=f.field('Доступность устройства');optional.value='optional';optional.dispatchEvent(new f.window.Event('change') as unknown as Event);
    const enabled=f.field('Шаг включён');enabled.checked=false;enabled.dispatchEvent(new f.window.Event('change') as unknown as Event);
    f.submit();const doc=f.requests.at(-1)!.scenario as state.Scenario;
    assert.equal(doc.actions[0].enabled,false);assert.equal(doc.actions[0].required,false);assert.equal(doc.actions[0].parameters.level,35);
    assert.equal(f.field('Уровень, % *').required,false);
  }finally{await f.window.happyDOM.close();}
});

test('actual place forms create custom location and editor can add a previously absent version', async()=>{
  const f=fixture();try {
    f.button('Добавить своё место');
    const card=[...f.window.document.querySelectorAll('.place-card')].at(-1)!;
    const name=card.querySelector('input')!;name.value='Дача';name.dispatchEvent(new f.window.Event('input'));
    const cities=card.querySelector('textarea')!;cities.value='Новый город';cities.dispatchEvent(new f.window.Event('input'));
    f.button('Сохранить место',card as unknown as ParentNode);
    const request=f.requests.at(-1)!;assert.equal(request.op,'place_save');
    const place={...(request.place as state.Place),revision:1};assert.equal(place.name,'Дача');assert.deepEqual(place.cities,['Новый город']);
    const snapshot={...f.data,places:[...f.data.places,place]};f.view.receive({...snapshot,place_saved:place.id});
    f.button('Новая команда');const target=f.field('Место для новой версии');target.value=place.id;target.dispatchEvent(new f.window.Event('change') as unknown as Event);
    f.button('Добавить пустую версию');f.button('Безопасный тест черновика');
    const draft=f.requests.at(-1)!.scenario as state.Scenario;
    assert.equal(draft.variants.length,1);assert.equal(draft.variants[0].place_id,place.id);assert.equal(draft.variants[0].name,'Дача');
    const current=f.field('Выбор места для голосовых команд');current.value=place.id;current.dispatchEvent(new f.window.Event('change') as unknown as Event);
    assert.deepEqual(f.requests.at(-1),{op:'location',mode:'manual',place_id:place.id});
  }finally{await f.window.happyDOM.close();}
});

test('revision history restores through optimistic backend contract and applies restored editor data', async()=>{
  const f=fixture();try {
    f.button('Новая команда');f.fill('Название','Сон');f.submit();
    const saved={...(f.requests.at(-1)!.scenario as state.Scenario),id:'c'.repeat(32),revision:7};
    f.view.receive({...f.data,scenarios:[saved],saved:saved.id});
    f.button('История редакций');assert.deepEqual(f.requests.at(-1),{op:'revisions',id:saved.id});
    f.view.receive({...f.data,scenarios:[saved],revisions:{id:saved.id,items:[{revision:5,name:'Старая редакция',created_at:'2026-11-02T12:00:00Z'}]}});
    f.fill('Название','Не сохранять');f.button('Восстановить редакцию');
    assert.deepEqual(f.requests.at(-1),{op:'restore',id:saved.id,from_revision:5,expected_revision:7});
    const restored={...saved,name:'Старая редакция',revision:8};
    f.view.receive({...f.data,scenarios:[restored],saved:restored.id});
    assert.equal(f.field('Название').value,'Старая редакция');
  }finally{await f.window.happyDOM.close();}
});

test('new interface explains one-time backend upgrade instead of crashing against older app process',async()=>{
  const f=fixture();try {
    const legacy={...f.data} as Partial<typeof f.data>;delete legacy.places;delete legacy.location;
    assert.doesNotThrow(()=>f.view.receive(legacy));
    assert.match(f.window.document.querySelector('.scenario-feedback')!.textContent,/один раз перезапусти/);
    assert.equal((f.window.document.querySelector('.automations-page .page-toolbar button') as unknown as HTMLButtonElement).disabled,true);
  }finally{await f.window.happyDOM.close();}
});

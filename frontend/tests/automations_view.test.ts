/** Exercises the production editor's DOM event handlers; no browser, network or devices. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {Window} from 'happy-dom';
import * as state from '../src/automation_state.ts';
import * as workspaceState from '../src/workspace_state.ts';

const source=readFileSync(new URL('../src/automations.ts',import.meta.url),'utf8');
const html=readFileSync(new URL('../index.html',import.meta.url),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,'');
const code=stripTypeScriptTypes(source,{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export \{.*\} from .*;\n/gm,'').replace('export class AutomationsView','class AutomationsView')+'\nexports.AutomationsView=AutomationsView;';
const workspaceCode=stripTypeScriptTypes(readFileSync(new URL('../src/workspace.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace('export class Workspace','class Workspace')+'\nexports.Workspace=Workspace;';
function fixture() {
  const window=new Window({url:'http://127.0.0.1:8879', settings:{disableJavaScriptEvaluation:true,disableCSSFileLoading:true,disableJavaScriptFileLoading:true}});
  window.document.write(html); window.confirm=()=>true;
  window.HTMLElement.prototype.scrollIntoView=()=>{};
  const requests: Record<string,unknown>[]=[];
  const sandbox={...state,...workspaceState,exports:{} as Record<string,unknown>,document:window.document,window,CustomEvent:window.CustomEvent,HTMLButtonElement:window.HTMLButtonElement,structuredClone,crypto,icon:()=>'<svg aria-hidden="true"></svg>'};
  vm.runInNewContext(workspaceCode+code,sandbox);
  const Workspace=sandbox.exports.Workspace as new (...args:unknown[])=>{navigate:(view:string)=>void};
  const workspace=new Workspace(()=>{},()=>{},()=>{},()=>{},()=>{});
  const View=sandbox.exports.AutomationsView as new (send:(v:Record<string,unknown>)=>void,...args:unknown[])=>{connection:(v:boolean)=>void;receive:(v:unknown)=>void};
  const view=new View(value=>requests.push(structuredClone(value)),(view:string)=>workspace.navigate(view),()=>{},()=>{});
  view.connection(true);
  const data={type:'automations',editor_version:3,scenarios:[] as state.Scenario[],capabilities:[{id:'mac.brightness',name:'Яркость MacBook',device:'mac',description:'Test transport',parameters:{level:{type:'integer',minimum:0,maximum:100}},required:['level'],permissions:[],risk:'state_change',availability:'not_checked'}],runs:[],training:[],devices:[],places:[{id:'dorm',revision:1,name:'Общага',cities:[]},{id:'home',revision:1,name:'Дом',cities:[]}],location:{place_id:'dorm',name:'Общага',mode:'manual'}};
  view.receive(data);
  function button(text:string, root=window.document as unknown as ParentNode) {
    const found=[...root.querySelectorAll('button')].find(node=>node.textContent===text || node.getAttribute('aria-label')===text);
    assert.ok(found,`Button ${text} exists`); assert.equal((found as HTMLButtonElement).disabled,false,`Button ${text} enabled`);
    (found as HTMLButtonElement).click();
  }
  function field(text:string) {
    const found=[...window.document.querySelectorAll('label')].find(label=>label.firstElementChild?.textContent===text)?.querySelector('input,select,textarea');
    assert.ok(found,`Field ${text} exists`);return found as unknown as HTMLInputElement;
  }
  function fill(text:string,value:string) {const input=field(text);input.value=value;input.dispatchEvent(new window.Event('input',{bubbles:true}) as unknown as Event);}
  function phrase(value:string) {const entry=window.document.querySelector('.phrase-entry')! as unknown as HTMLInputElement;entry.value=value;entry.dispatchEvent(new window.Event('input',{bubbles:true}) as unknown as Event);entry.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true}) as unknown as Event);}
  function submit() {window.document.querySelector('.scenario-editor form')!.dispatchEvent(new window.Event('submit',{cancelable:true,bubbles:true}));}
  return {window,view,workspace,data,requests,button,field,fill,submit,phrase};
}

test('real editor creates one command, common action and independently editable location copies with safe preview', async()=>{
  const f=fixture();try {
    f.button('+ Создать сценарий'); f.fill('Название','Я спать');f.phrase('Пора ложиться');
    f.button('Яркость MacBook');f.fill('Уровень, % *','10');
    f.button('+ Общага');f.button('Создать пустую версию');
    f.button('Яркость MacBook');f.fill('Уровень, % *','25');
    f.button('+ Дом');f.button('Скопировать блоки из общей');f.fill('Уровень, % *','40');
    f.button('Проверить сценарий');
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
    f.button('+ Создать сценарий');f.fill('Название','Тихий вечер');f.submit();
    const saved={...(f.requests.at(-1)!.scenario as state.Scenario),id:'b'.repeat(32),revision:7};
    f.view.receive({...f.data,scenarios:[saved],saved:saved.id});
    f.button('Удалить сценарий');assert.deepEqual(f.requests.at(-1),{op:'delete',id:saved.id,revision:7});
    f.view.receive({...f.data,deleted:saved.id});f.button('Отменить удаление');
    assert.deepEqual(f.requests.at(-1),{op:'restore',id:saved.id,from_revision:7,expected_revision:0});
    f.view.receive({...f.data,scenarios:[saved],saved:saved.id});
    const search=f.window.document.querySelector('[aria-label="Поиск моих команд"]')! as unknown as HTMLInputElement;
    search.value='Нет такой';search.dispatchEvent(new f.window.Event('input') as unknown as Event);
    assert.match(f.window.document.querySelector('.scenario-list')!.textContent,/Сценариев не найдено/);
  }finally{await f.window.happyDOM.close();}
});

test('step disabling, optional device and retained settings are submitted through actual controls', async()=>{
  const f=fixture();try {
    f.button('+ Создать сценарий');f.fill('Название','Тест');f.button('Яркость MacBook');f.fill('Уровень, % *','35');
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
    f.button('+ Создать сценарий');f.fill('Название','Дача');const target=f.field('Другое сохранённое место');target.value=place.id;target.dispatchEvent(new f.window.Event('change') as unknown as Event);
    f.button('Создать версию другого места');f.button('Создать пустую версию');f.button('Проверить сценарий');
    const draft=f.requests.at(-1)!.scenario as state.Scenario;
    assert.equal(draft.variants.length,1);assert.equal(draft.variants[0].place_id,place.id);assert.equal(draft.variants[0].name,'Дача');
    const current=f.field('Выбор места для голосовых команд');current.value=place.id;current.dispatchEvent(new f.window.Event('change') as unknown as Event);
    assert.deepEqual(f.requests.at(-1),{op:'location',mode:'manual',place_id:place.id});
  }finally{await f.window.happyDOM.close();}
});

test('revision history restores through optimistic backend contract and applies restored editor data', async()=>{
  const f=fixture();try {
    f.button('+ Создать сценарий');f.fill('Название','Сон');f.submit();
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

function disclosure(f: ReturnType<typeof fixture>, selector: string) {
  const details = f.window.document.querySelector(selector)!;
  assert.ok(details, selector); (details as unknown as HTMLDetailsElement).open = true;
  details.dispatchEvent(new f.window.Event('toggle'));
}

test('reference editor keeps navigation, inline controls, tags and advanced disclosure', async()=>{
  const f=fixture();try {
    assert.deepEqual([...f.window.document.querySelectorAll('.workspace-nav > button[data-view]')].map(n=>n.getAttribute('data-view')),['home','automations','devices','performance','settings']);
    assert.equal(f.window.document.querySelector('.permissions-page'),null);
    assert.ok(f.window.document.querySelector('.settings-page .permission-settings'));
    f.button('+ Создать сценарий');
    assert.deepEqual([...f.window.document.querySelectorAll('.editor-step h2')].map(n=>n.textContent),['Блоки сценария','Дополнительно']);
    assert.equal((f.window.document.querySelector('.editor-advanced') as unknown as HTMLDetailsElement).open,false);
    assert.equal(f.window.document.querySelector('.scenario-layout')!.getAttribute('data-editing'),'true');
    assert.ok(f.window.document.querySelector('.editor-advanced .variant-editor'));
    assert.equal(f.window.document.querySelector('form form'),null);
    f.fill('Название','Уют');
    for (let i=0;i<40;i++) f.phrase(`Уютная фраза ${i}`);
    disclosure(f,'.action-picker');f.button('Яркость MacBook');f.fill('Уровень, % *','27');
    const field=f.field('Уровень, % *');assert.equal(field.min,'0');assert.equal(field.max,'100');
    f.button('Сохранить',f.window.document.querySelector('.scenario-editor')! as unknown as ParentNode);
    const draft=f.requests.at(-1)!.scenario as state.Scenario;
    assert.equal(draft.phrases.length,40);assert.equal(draft.confirmation,'auto');assert.ok(draft.triggers.some(t=>t.kind==='voice'));
    assert.equal(draft.actions[0].capability,'mac.brightness');assert.equal(draft.actions[0].parameters.level,27);
    assert.equal(f.requests.some(r=>r.op==='run'||r.op==='capture'),false);
  }finally{await f.window.happyDOM.close();}
});

test('list switch preserves revisions without opening the editor; menu duplicates the saved command', async()=>{
  const f=fixture();try {
    const saved={...state.newScenario(),id:'d'.repeat(32),revision:4,name:'Тихий вечер',phrases:['Давай отдыхать'],actions:[{...state.newBlock('action','mac.brightness'),parameters:{level:20}}]};
    f.view.receive({...f.data,scenarios:[saved]});
    const toggle=f.window.document.querySelector('.scenario-list [role="switch"]')! as unknown as HTMLButtonElement;toggle.click();
    const changed=f.requests.at(-1)!.scenario as state.Scenario;assert.equal(changed.enabled,false);assert.equal(changed.revision,4);assert.equal(changed.phrases[0],'Давай отдыхать');
    f.view.receive({...f.data,scenarios:[{...changed,revision:5}],saved:saved.id});
    assert.equal(f.window.document.querySelector('.scenario-layout')!.getAttribute('data-editing'),'false');
    assert.equal(f.window.document.querySelector('.scenario-list [role="switch"]')!.getAttribute('aria-checked'),'false');
    disclosure(f,'.scenario-menu');f.button('Дублировать');assert.deepEqual(f.requests.at(-1),{op:'copy',id:saved.id});
    const copy={...changed,id:'e'.repeat(32),revision:1,name:'Тихий вечер (копия)',phrases:[]};
    f.view.receive({...f.data,scenarios:[saved,copy],saved:copy.id});assert.equal(f.field('Название').value,copy.name);
  }finally{await f.window.happyDOM.close();}
});

test('training belongs to the draft and conflicts are explained without discarding edits', async()=>{
  const f=fixture();try {
    f.button('+ Создать сценарий');f.fill('Название','Домой');disclosure(f,'.phrase-training');
    const input=f.field('Новая фраза');input.value='Я вернулась';input.dispatchEvent(new f.window.Event('input') as unknown as Event);
    f.button('Проверить понимание');assert.deepEqual(f.requests.at(-1),{op:'recognize',text:'Я вернулась'});
    f.view.receive({...f.data,recognition:{scenario_id:null,confidence:.3,method:'semantic',candidates:[]}});
    assert.match(f.window.document.querySelector('.scenario-recognition')!.textContent,/Добавь её к сценарию/);
    assert.doesNotMatch(f.window.document.querySelector('.scenario-recognition')!.textContent,/confidence|semantic|%|embedding/i);
    f.button('Добавить к фразам');f.submit();const draft=f.requests.at(-1)!.scenario as state.Scenario;assert.deepEqual(draft.phrases,['Я вернулась']);
    assert.equal(f.requests.some(r=>r.op==='teach'||r.op==='capture'),false);
    f.view.receive({...f.data,error:'Фраза уже используется другой командой: internal-id'});
    const feedback=f.window.document.querySelector('.automations-page > .scenario-feedback')!;assert.match(feedback.firstElementChild!.textContent,/Измени её/);
    assert.equal(feedback.querySelector('details')!.open,false);assert.equal(f.field('Название').value,'Домой');
    f.submit();assert.equal(f.requests.at(-1)!.op,'save');
  }finally{await f.window.happyDOM.close();}
});

test('permission statuses hide technical diagnostics and devices remain independent', async()=>{
  const f=fixture();try {
    f.view.receive({...f.data,devices:[{id:'mac',name:'MacBook',type:'system',status:'not_checked',actions:['mac.brightness'],permissions:[],note:'Описание подключения',details:{uuid:'test-device-uuid'}}],permissions:{scope:'test scope',items:[{id:'automation',name:'Управление приложениями',status:'unknown',note:'OSStatus -1744 test diagnostic',url:'x-apple.systempreferences:test'},{id:'bluetooth',name:'Bluetooth',status:'granted',note:'',url:''},{id:'other',name:'Другое',status:'unsupported',note:'',url:''}]}});
    assert.equal(f.window.document.querySelector('#devices-root')!.childElementCount,0);
    const permissions=f.window.document.querySelector('.permission-list')!;
    assert.deepEqual([...permissions.querySelectorAll('small')].map(n=>n.textContent),['Требуется доступ','Разрешено','Недоступно']);
    assert.equal(permissions.querySelector('details')!.open,false);
    assert.equal(permissions.querySelectorAll('a').length,1);
    assert.ok(f.window.document.querySelector('.performance-page .scenario-history'));
    assert.equal(f.window.document.querySelector('.automations-page .scenario-history'),null);
  }finally{await f.window.happyDOM.close();}
});

test('reopening saved actions keeps rows compact, supports reorder and preserves nested settings', async()=>{
  const f=fixture();try {
    const saved={...state.newScenario(),id:'f'.repeat(32),revision:2,name:'Вечер',actions:[{...state.newBlock('action','mac.brightness'),parameters:{level:20}},{...state.newBlock('delay'),seconds:5}],variables:[{name:'level',kind:'number' as const,default:20}]};
    f.view.receive({...f.data,scenarios:[saved]});(f.window.document.querySelector('.scenario-open')! as unknown as HTMLButtonElement).click();
    const rows=[...f.window.document.querySelectorAll('.scenario-block')];assert.equal(rows.length,2);assert.ok(rows.every(row=>!row.open));
    disclosure(f,'.scenario-block');f.button('↓',rows[0] as unknown as ParentNode);f.submit();
    const draft=f.requests.at(-1)!.scenario as state.Scenario;assert.equal(draft.actions[0].kind,'delay');assert.equal(draft.actions[1].parameters.level,20);assert.equal(draft.variables[0].name,'level');
    f.view.connection(false);assert.equal((f.window.document.querySelector('.editor-heading .primary') as unknown as HTMLButtonElement).disabled,true);
    f.view.connection(true);assert.equal((f.window.document.querySelector('.scenario-block .action-controls button') as unknown as HTMLButtonElement).disabled,true);
  }finally{await f.window.happyDOM.close();}
});

test('primary navigation selects its parent and keeps chat history and memory reachable',async()=>{
  const f=fixture();try {
    const destinations=['home','automations','devices','performance','settings'];
    for(const view of destinations){
      (f.window.document.querySelector(`.workspace-nav [data-view="${view}"]`)! as unknown as HTMLButtonElement).click();
      assert.equal(f.window.document.querySelector('.hud')!.getAttribute('data-view'),view);
      assert.equal(f.window.document.querySelector('.workspace-nav [aria-current]')!.getAttribute('data-view'),view);
    }
    for(const [view,parent] of [['history','home'],['calendar','home'],['day','home'],['memory','settings'],['diagnostics','performance']]) {
      f.workspace.navigate(view);assert.equal(f.window.document.querySelector('.workspace-nav [aria-current]')!.getAttribute('data-view'),parent);
    }
    assert.ok(f.window.document.querySelector('.hud > .chat-navigation [data-go="history"]'));
    assert.ok(f.window.document.querySelector('.settings-page [data-go="memory"]'));
    assert.equal(f.window.document.querySelector('.talk .chat-navigation'),null);
  }finally{await f.window.happyDOM.close();}
});

test('disabled copies explain voice availability and can be enabled in advanced settings',async()=>{
  const f=fixture();try {
    const copy={...state.newScenario(),id:'a'.repeat(32),revision:1,enabled:false,name:'Копия'};
    f.view.receive({...f.data,scenarios:[copy],saved:copy.id});
    assert.match(f.window.document.querySelector('.editor-save > p')!.textContent,/Сценарий выключен/);
    disclosure(f,'.editor-advanced');
    const enabled=f.field('Сценарий включён');enabled.checked=true;enabled.dispatchEvent(new f.window.Event('change') as unknown as Event);
    f.submit();assert.equal((f.requests.at(-1)!.scenario as state.Scenario).enabled,true);
    assert.ok(f.window.document.querySelector('.settings-page #settings-items')!.closest('details')!.querySelector('#autostart'));
    assert.equal(f.window.document.querySelector('.hud > .bottom'),null);
  }finally{await f.window.happyDOM.close();}
});

test('LED colour and approved Shortcut use typed controls and retain the real Shortcut name',async()=>{
  const f=fixture();try {
    const data={...f.data,capabilities:[...f.data.capabilities,{id:'led.set',name:'Питание, цвет и яркость ленты',device:'led',description:'',parameters:{color:{type:'string',pattern:'#[0-9A-F]{6}'},brightness:{type:'integer',minimum:0,maximum:100}},required:[],permissions:['bluetooth'],risk:'state_change',availability:'not_checked'},{id:'shortcut.run',name:'Запустить разрешённую Shortcut',device:'shortcuts',description:'',parameters:{name:{type:'string',enum:['Youtube sleep']}},required:['name'],permissions:['shortcuts'],risk:'depends_on_parameters',availability:'not_checked'}]};
    f.view.receive(data);f.button('+ Создать сценарий');f.fill('Название','Свет');f.button('Питание, цвет и яркость ленты');
    const color=f.field('Цвет');assert.equal(color.type,'color');color.value='#ff3040';color.dispatchEvent(new f.window.Event('input') as unknown as Event);
    f.fill('Яркость, %','15');f.button('Запустить разрешённую Shortcut');
    const name=f.field('Название *');assert.equal(name.tagName,'SELECT');name.value='Youtube sleep';name.dispatchEvent(new f.window.Event('change') as unknown as Event);
    f.submit();const draft=f.requests.at(-1)!.scenario as state.Scenario;assert.deepEqual(draft.actions[0].parameters,{color:'#FF3040',brightness:15});assert.equal(draft.actions[1].parameters.name,'Youtube sleep');
    const saved={...draft,id:'b'.repeat(32),revision:1,actions:[draft.actions[0],{...draft.actions[1],parameters:{name:'youtube sleep'}}]};
    f.view.receive({...data,scenarios:[saved],saved:saved.id});f.button('Проверить сценарий');
    assert.equal((f.requests.at(-1)!.scenario as state.Scenario).actions[1].parameters.name,'Youtube sleep');
    assert.equal(f.requests.some(r=>r.op==='run'),false);
  }finally{await f.window.happyDOM.close();}
});

test('tags use Enter, reject normalized duplicates, and save a pending phrase with all options',async()=>{
  const f=fixture();try {
    f.button('+ Создать сценарий'); f.fill('Название','Сон'); f.phrase('Пора спать'); f.phrase('Пора спать!');
    assert.equal(f.window.document.querySelectorAll('.phrase-tag').length,1);
    assert.match(f.window.document.querySelector('.automations-page > .scenario-feedback')!.textContent,/уже добавлена/);
    const entry=f.window.document.querySelector('.phrase-entry')! as unknown as HTMLInputElement;
    entry.value='Отбой';entry.dispatchEvent(new f.window.Event('input',{bubbles:true}) as unknown as Event);
    f.fill('Задержка между блоками, с','0.5');f.fill('Ограничение времени, с','90');
    const confirmation=f.field('Всегда требовать подтверждение');confirmation.checked=true;confirmation.dispatchEvent(new f.window.Event('change') as unknown as Event);
    const visible=f.field('Показывать статус выполнения в диалоге');visible.checked=false;visible.dispatchEvent(new f.window.Event('change') as unknown as Event);
    f.submit(); const saved=f.requests.at(-1)!.scenario as state.Scenario;
    assert.deepEqual(saved.phrases,['Пора спать','Отбой']);assert.equal(saved.version_policy,'fallback');
    assert.equal(saved.inter_block_delay_s,.5);assert.equal(saved.timeout_s,90);assert.equal(saved.confirmation,'always');assert.equal(saved.show_status,false);
  }finally{await f.window.happyDOM.close();}
});

test('mandatory General, optional tabs and copy edit independent action lists without changing current place',async()=>{
  const f=fixture();try {
    f.button('+ Создать сценарий');f.fill('Название','Сон');
    f.button('Яркость MacBook');f.fill('Уровень, % *','10');
    f.button('+ Общага');f.button('Создать пустую версию');f.button('Яркость MacBook');f.fill('Уровень, % *','20');
    f.button('+ Дом');f.button('Создать пустую версию');f.button('Яркость MacBook');f.fill('Уровень, % *','30');
    const target=f.window.document.querySelector('[aria-label="Куда скопировать блоки"]')! as unknown as HTMLSelectElement;
    const dormId=Array.from(target.options).find(n=>n.textContent==='Общага')!.value;
    target.value=dormId;target.dispatchEvent(new f.window.Event('change') as unknown as Event);f.button('Копировать блоки');
    f.button('Общага',f.window.document.querySelector('.variant-tabs')! as unknown as ParentNode);f.fill('Уровень, % *','25');f.button('Проверить сценарий');
    const draft=f.requests.at(-1)!.scenario as state.Scenario;
    assert.equal(draft.actions[0].parameters.level,10);assert.equal(draft.variants.find(v=>v.place_id==='home')!.actions[0].parameters.level,30);
    assert.equal(draft.variants.find(v=>v.place_id==='dorm')!.actions[0].parameters.level,25);
    assert.equal(f.requests.some(r=>r.op==='location'||r.op==='run'),false);
    f.button('Общий',f.window.document.querySelector('.variant-tabs')! as unknown as ParentNode);f.button('Проверить сценарий');assert.equal(f.requests.at(-1)!.variant,'general');
    assert.match(f.window.document.querySelector('.editor-location-current')!.textContent,/Сейчас: Общага/);
  }finally{await f.window.happyDOM.close();}
});

test('dragging the handle moves a block instead of swapping endpoints; duplicate is a deep copy',async()=>{
  const f=fixture();try {
    const saved={...state.newScenario(),id:'c'.repeat(32),revision:1,name:'Порядок',actions:[10,20,30].map(level=>({...state.newBlock('action','mac.brightness'),parameters:{level}}))};
    f.view.receive({...f.data,scenarios:[saved],saved:saved.id});
    const rows=[...f.window.document.querySelectorAll('.scenario-block')];
    const dataTransfer=new f.window.DataTransfer();
    rows[0].querySelector('.block-drag-handle')!.dispatchEvent(new f.window.DragEvent('dragstart',{bubbles:true,dataTransfer}));
    rows[2].dispatchEvent(new f.window.DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer}));
    const first=f.window.document.querySelector('.scenario-block')!;
    f.button('Дублировать блок',first as unknown as ParentNode);
    const numbers=f.window.document.querySelectorAll('.slider-number');
    (numbers[1] as unknown as HTMLInputElement).value='50';numbers[1].dispatchEvent(new f.window.Event('input',{bubbles:true}));
    f.submit();const result=f.requests.at(-1)!.scenario as state.Scenario;
    assert.deepEqual(result.actions.map(b=>b.parameters.level),[20,50,30,10]);
    assert.deepEqual(saved.actions.map(b=>b.parameters.level),[10,20,30]);
  }finally{await f.window.happyDOM.close();}
});

test('searchable catalogue offers delay and nested logic but never invents unsupported tools',async()=>{
  const f=fixture();try {
    f.button('+ Создать сценарий');f.fill('Название','Пауза');
    const search=f.window.document.querySelector('[aria-label="Поиск блоков"]')! as unknown as HTMLInputElement;
    search.value='подождать';search.dispatchEvent(new f.window.Event('input'));
    assert.equal(f.window.document.querySelectorAll('.action-group button').length,1);f.button('Подождать');
    let unit=f.window.document.querySelector('[aria-label="Единица времени"]')! as unknown as HTMLSelectElement;
    unit.value='60';unit.dispatchEvent(new f.window.Event('change'));f.fill('Длительность','2');
    f.button('Условие: если / иначе');
    assert.ok(f.window.document.querySelector('.condition-fields'));
    assert.match(f.window.document.querySelector('.scenario-blocks')!.textContent,/Да.*Нет/);
    assert.doesNotMatch(f.window.document.querySelector('.action-choices')!.textContent,/Произнести текст|Вызвать другой сценарий|Уведомление/);
    f.submit();const draft=f.requests.at(-1)!.scenario as state.Scenario;
    assert.equal(draft.actions[0].seconds,120);assert.equal(draft.actions[1].condition!.source,'time');
  }finally{await f.window.happyDOM.close();}
});

test('unsaved edits guard navigation and close; a failed save preserves editable draft',async()=>{
  const f=fixture();try {
    f.workspace.navigate('automations');f.button('+ Создать сценарий');f.fill('Название','Сохранить меня');
    f.window.confirm=()=>false;f.workspace.navigate('home');assert.equal(f.window.document.querySelector('.hud')!.getAttribute('data-view'),'automations');
    const close=new f.window.Event('beforeunload',{cancelable:true});f.window.dispatchEvent(close);assert.equal(close.defaultPrevented,true);
    f.submit();assert.equal(f.field('Название').disabled,true);
    f.view.receive({...f.data,error:'Не удалось сохранить'});assert.equal(f.field('Название').disabled,false);assert.equal(f.field('Название').value,'Сохранить меня');
    f.window.confirm=()=>true;f.workspace.navigate('home');assert.equal(f.window.document.querySelector('.hud')!.getAttribute('data-view'),'home');
  }finally{await f.window.happyDOM.close();}
});

test('simulation displays invalid and unavailable steps; real runs require a separate explicit click',async()=>{
  const f=fixture();try {
    const saved={...state.newScenario(),id:'d'.repeat(32),revision:2,name:'Вечер',actions:[{...state.newBlock('action','mac.brightness'),parameters:{level:20}}]};
    f.view.receive({...f.data,scenarios:[saved],saved:saved.id});f.button('Проверить сценарий');
    assert.equal(f.requests.at(-1)!.op,'preview');assert.equal(f.requests.some(r=>r.op==='run'),false);
    f.view.receive({...f.data,scenarios:[saved],preview:{status:'dry_run',name:'Вечер',message:'Устройства не запускались.',selection:null,conditions:[],errors:['Проверь параметры'],steps:[{path:'1',kind:'action',capability:'mac.brightness',status:'invalid',reason:'Некорректная яркость',device:'mac'},{path:'2',kind:'action',capability:'mac.brightness',status:'planned',availability:'unavailable'}]}});
    assert.match(f.window.document.querySelector('.command-preview')!.textContent,/Некорректная яркость/);assert.match(f.window.document.querySelector('.command-preview')!.textContent,/Устройство недоступно/);
    f.window.confirm=()=>false;f.button('Запустить на устройствах');assert.equal(f.requests.some(r=>r.op==='run'),false);
    f.window.confirm=()=>true;f.button('Запустить на устройствах');assert.deepEqual(f.requests.at(-1),{op:'run',id:saved.id,revision:2,variant:''});
    f.view.receive({...f.data,scenarios:[saved],runs:[{id:'test-run',scenario_id:saved.id,name:saved.name,status:'sent',started_at:'2026-11-02T12:00:00Z',steps:[{path:'1',kind:'action',capability:'mac.brightness',status:'sent'}]}]});
    assert.match(f.window.document.querySelector('.scenario-execution')!.textContent,/физический результат не подтверждён/);
    assert.match(f.window.document.querySelector('.scenario-dialogue-status')!.textContent,/Вечер/);
    f.view.receive({...f.data,scenarios:[{...saved,show_status:false}],runs:[{id:'test-run',scenario_id:saved.id,name:saved.name,status:'confirmed',started_at:'2026-11-02T12:00:00Z',steps:[]}]});
    assert.equal(f.window.document.querySelector('.scenario-dialogue-status')!.textContent,'');
  }finally{await f.window.happyDOM.close();}
});

test('General cannot be removed; plus tabs offer an empty version or an independent General copy',async()=>{
  const f=fixture();try {
    f.button('+ Создать сценарий');f.fill('Название','Единый сценарий');
    assert.equal(f.window.document.querySelectorAll('.variant-tabs [role="tab"]').length,1);
    assert.ok(f.window.document.querySelector('[aria-label="+ Общага"]'));
    assert.ok(f.window.document.querySelector('[aria-label="+ Дом"]'));
    assert.equal(f.window.document.querySelector('.scenario-location-options'),null);
    assert.equal([...f.window.document.querySelectorAll('button')].some(n=>n.textContent==='Удалить общую версию'),false);
    f.button('Яркость MacBook');f.fill('Уровень, % *','10');
    f.button('+ Общага');assert.ok(f.window.document.querySelector('.version-create'));
    f.button('Скопировать блоки из общей');f.fill('Уровень, % *','15');
    f.button('+ Дом');f.button('Создать пустую версию');
    assert.match(f.window.document.querySelector('.blocks-empty')!.textContent,/первое действие/);
    f.window.confirm=()=>false;f.button('Удалить версию');
    assert.equal(f.window.document.querySelectorAll('.variant-tabs [role="tab"]').length,3);
    f.window.confirm=()=>true;f.button('Удалить версию');
    assert.equal(f.window.document.querySelectorAll('.variant-tabs [role="tab"]').length,2);
    assert.equal(f.field('Уровень, % *').value,'10');
    assert.equal([...f.window.document.querySelectorAll('.variant-copy button')].some(n=>n.textContent==='Удалить версию'),false);
    f.submit();const draft=f.requests.at(-1)!.scenario as state.Scenario;
    assert.equal(draft.general_enabled,true);assert.equal(draft.schema_version,2);assert.equal(draft.version_policy,'fallback');
    assert.equal(draft.actions[0].parameters.level,10);assert.equal(draft.variants[0].actions[0].parameters.level,15);
    assert.match(f.window.document.querySelector('.variant-tabs')!.textContent,/Таллинн/);
    assert.match(f.window.document.querySelector('.variant-tabs')!.textContent,/Силламяэ/);
  }finally{await f.window.happyDOM.close();}
});

test('location controls request macOS consent only by an explicit click and preserve manual priority',async()=>{
  const f=fixture();try {
    f.view.receive({...f.data,location:{place_id:null,name:null,mode:'auto',status:'unknown',geolocation_enabled:false}});
    f.button('+ Создать сценарий');
    assert.match(f.window.document.querySelector('.editor-location-current')!.textContent,/место неизвестно.*Общий/);
    assert.equal(f.requests.some(r=>r.op==='location_connect'||r.op==='location_refresh'),false);
    f.button('Включить геолокацию macOS');assert.deepEqual(f.requests.at(-1),{op:'location_connect',consent:true});
    const select=f.window.document.querySelector('[aria-label="Текущее место"]')! as unknown as HTMLSelectElement;
    select.value='home';select.dispatchEvent(new f.window.Event('change'));
    assert.deepEqual(f.requests.at(-1),{op:'location',mode:'manual',place_id:'home'});
    f.view.receive({...f.data,location:{place_id:'home',name:'Дом',mode:'manual',status:'ready'}});
    assert.equal(f.window.document.querySelector('.editor-location-current .location-connect'),null);
    const manual=f.window.document.querySelector('[aria-label="Текущее место"]')! as unknown as HTMLSelectElement;
    manual.value='auto';manual.dispatchEvent(new f.window.Event('change'));
    assert.deepEqual(f.requests.at(-1),{op:'location',mode:'auto',place_id:''});
    f.view.receive({...f.data,location:{place_id:'dorm',name:'Общага',city:'Таллинн',mode:'auto',status:'ready',geolocation_enabled:true}});
    assert.match(f.window.document.querySelector('.editor-location-current')!.textContent,/Таллинн.*Общага/);
    f.button('Обновить город');assert.deepEqual(f.requests.at(-1),{op:'location_refresh'});
  }finally{await f.window.happyDOM.close();}
});

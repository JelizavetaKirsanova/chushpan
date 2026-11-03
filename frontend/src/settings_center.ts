import {applicationSettings} from './application_settings';
import type {HudEvent} from './types';
import type {Place, Scenario} from './automation_state';
import {icon} from './dashboard';

type Values = Record<string, unknown>;
export interface PersonalityProfile {id:string; revision:number; name:string; description:string; builtin:boolean; traits:Record<string,number>; communication:Values; instructions:string; technical_detail:number}
type Memory = {id:number; content:string; category:string; revision:number; status:string; source:string};
export interface SettingsState {type:'settings_center'; version:number; values:Record<string,Values>; profiles:PersonalityProfile[];
  active:{id:string;reason:string;locked:boolean}; traits:Record<string,{name:string;group:string}>;
  memory:Memory[]; memory_pending:{id:string;content:string}[]; habits_opt_in:boolean;
  engine:string; voices:string[]; voice_support:{rate:boolean;pitch:boolean;expression:boolean};
  input_devices?:{id:number;name:string}[]|null;
  permissions:{items:{id:string;name:string;status:string;note:string;url:string}[];scope:string} | null;
  models:{installed?:string[];ollama?:string;tts?:string;gemini?:{models?:{model:string;provider:string;status:string;latency_s?:number|null;error?:string|null}[]};streaming?:boolean};
  location:{name?:string|null;place_id?:string|null;mode?:string;message?:string;reason?:string}; places:Place[];
  signals:{signals?:Signal[];error?:string}; rules:Scenario[];
  integrations:{id:string;name:string;tools:string[];enabled:boolean;supported:boolean;status:string;error:string|null;details:string[]}[];
  actions:{id:string;description:string;risk:string;enabled:boolean}[]; recent_actions:Values[];
  last_errors:{created_at:string;errors:string[];model:string}[]; preview?:Preview[];
  training?:{phrase:string;operation:string}[]; selected?:string; captured?:string; message?:string;error?:string}
interface Signal {kind:string;key:string;name:string}
interface Preview {id:string;name:string;text:string;model:string;first_token_s?:number|null;first_sound_s?:number|null;total_s?:number;done?:boolean}
export type SettingsEvent = SettingsState | ({type:'settings_preview'} & Preview)
  | {type:'settings_export';document:unknown;filename:string}
  | {type:'settings_import';token:string;counts:Record<string,number>;message:string};

const categories = [
  ['personality','Характер и личность','Настроение, привычки общения и твой собственный Чушпан','sparkles'],
  ['voice','Голос и прослушивание','Голос, произношение и разговор без лишних повторов','mic'],
  ['memory','Память и обучение','Важное о тебе и то, чему Чушпан научился','heart'],
  ['models','Модели и производительность','Скорость, качество и доступность моделей','cpu'],
  ['access','Доступ и безопасность','Разрешения, приложения и границы самостоятельности','shield'],
  ['places','Места и контекст','Дом, общага и подходящие версии сценариев','pin'],
  ['integrations','Интеграции','Устройства и сервисы, которые уже подключены','link'],
  ['application','Приложение','Запуск, строка меню, разрешения и обновления','settings'],
  ['advanced','Дополнительно','Тихие часы, резервные копии и диагностика','settings'],
];
const el = (tag:string, text='', cls='') => {const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
const id = (value:string) => document.getElementById(value)!;
const clone = <T>(value:T):T => structuredClone(value);
const sec = (value:number|null|undefined) => value == null ? 'не измерено' : `${value.toFixed(2)} с`;
const timeLabels:Record<string,string> = {ready:'Доступен',error:'Ошибка',not_checked:'Ещё не проверено',observed:'Прочитано',unavailable:'Недоступен',connected:'Подключено',granted:'Разрешено',denied:'Нет доступа',manual:'Проверь в macOS',unknown:'Не определено',unsupported:'Не используется'};

export class SettingsCenterUI {
  private state:SettingsState|null=null;
  private online=false;
  private page='';
  private selected='';
  private draft:PersonalityProfile|null=null;
  private dirty=false;
  private saving=false;
  private lastLatency='Последний отклик ещё не измерен в этой сессии.';
  private group='Стиль';
  private memoryTab='about';
  private previews=new Map<string,Preview>();
  private previewText='Я опять отложила учёбу на последний момент. Что скажешь?';
  private capturedText='';
  private legacy=new Map<string,HTMLElement[]>();
  private root:HTMLElement;
  constructor(private send:(value:object)=>void, private automation:(value:object)=>void, private navigate:(view:string)=>void) {
    this.root=id('settings-center');
    this.root.innerHTML=`<div id="settings-home"><div class="settings-heading"><span class="settings-kicker">ТВОЙ ЧУШПАН</span><h1>Настройки</h1><p>Характер, голос и привычки. Всё на своём месте.</p></div><div id="settings-active" class="settings-active"></div><div id="settings-categories" class="settings-categories"></div></div><div id="settings-detail" hidden><div class="settings-detail-heading"><button id="settings-back" type="button">‹ Все настройки</button><h1 id="settings-title"></h1><p id="settings-description"></p></div><div id="settings-content"></div></div><p id="settings-feedback" class="settings-feedback" role="status">Подключаюсь к настройкам…</p>`;
    for(const [key,title,description,glyph] of categories) {
      const b=this.button('',()=>this.open(key)); b.className='settings-category';b.dataset.category=key;
      const symbol=el('span','','settings-category-icon');symbol.innerHTML=icon(glyph);
      const text=el('span');text.append(el('strong',title),el('small',description));b.append(symbol,text,el('span','›','settings-chevron'));id('settings-categories').append(b);
    }
    id('settings-back').onclick=()=>this.open('');
    const take=(key:string,node:Element|null)=>{if(node){const list=this.legacy.get(key)??[];list.push(node as HTMLElement);this.legacy.set(key,list);}};
    take('voice',id('voice-settings'));take('voice',id('dialogue-settings').closest('details'));
    take('models',id('settings-items').closest('details'));
    take('places',id('manual-location').closest('section'));
    take('integrations',id('music-settings'));take('advanced',id('ambient-settings'));
    for(const nodes of this.legacy.values())for(const node of nodes)id('settings-old').append(node);
    document.addEventListener('workspace:navigate',event=>{
      const view=(event as CustomEvent<string>).detail;
      if(view!=='settings' && this.dirty && !window.confirm('Изменения характера ещё не сохранены. Покинуть настройки?'))event.preventDefault();
    });
    this.connection(false);
  }
  connection(online:boolean):void {
    this.online=online;
    this.root.querySelectorAll<HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement|HTMLButtonElement>('input,select,textarea,button[data-write]').forEach(n=>n.disabled=!online||!this.state||n.dataset.unavailable==='true');
    if(online){this.feedback('Загружаю сохранённые настройки…');this.send({op:'refresh'});}
    else this.feedback('Нет связи с Чушпаном. Сохранение не подтверждено.',true);
  }
  private button(text:string, action:()=>void, write=false):HTMLButtonElement {
    const b=el('button',text) as HTMLButtonElement;b.type='button';b.onclick=action;if(write){b.dataset.write='true';b.disabled=!this.online||!this.state;}return b;
  }
  private feedback(text:string,error=false):void {id('settings-feedback').textContent=text;id('settings-feedback').dataset.error=String(error);}
  private write(value:object):void {if(!this.online)return;this.feedback('Сохраняю…');this.send(value);}
  open(page:string):void {
    if(this.dirty && page!==this.page && !window.confirm('Профиль ещё не сохранён. Отменить изменения?'))return;
    if(page!==this.page){this.dirty=false;this.draft=null;}
    this.page=page;id('settings-home').hidden=!!page;id('settings-detail').hidden=!page;
    const data=categories.find(v=>v[0]===page);
    id('settings-title').textContent=data?.[1]??'';id('settings-description').textContent=data?.[2]??'';
    this.render();if(this.online)this.send({op:'refresh'});
  }
  receive(event:HudEvent):void {
    if(event.type==='stats'){this.lastLatency=`Последний отклик: ${sec(event.response_s??event.voice_s)} · первый фрагмент модели: ${sec(event.first_token_s)}`;const row=document.getElementById('settings-last-latency');if(row)row.textContent=this.lastLatency;return;}
    if(event.type==='settings_preview') {
      this.previews.set(event.id,event);this.renderPreviews();return;
    }
    if(event.type==='settings_export') {
      const blob=new Blob([JSON.stringify(event.document,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);
      const link=document.createElement('a');link.href=url;link.download=event.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);this.feedback('Локальная копия готова. API-ключи в неё не входят.');return;
    }
    if(event.type==='settings_import') {
      const counts=Object.entries(event.counts).map(([k,n])=>`${({profiles:'Профили',scenarios:'Сценарии',memories:'Воспоминания',categories:'Категории'} as Record<string,string>)[k]??k}: ${n}`).join(' · ');
      const box=document.getElementById('settings-backup-result');if(!box){this.feedback('Файл проверен. Вернись в резервное копирование, чтобы выбрать его снова.');return;}box.replaceChildren(el('p',counts),el('p',event.message),this.button('Восстановить из этой копии',()=>{if(window.confirm('Применить показанную копию? История разговоров сохранится.'))this.write({op:'import_apply',token:event.token,confirm:true});},true));return;
    }
    if(event.type==='dashboard' && this.page==='integrations' && this.online){this.send({op:'refresh'});return;}
    if(event.type==='automations' && this.state) {
      this.state.location=event.location;this.state.places=event.places;
      if(this.page==='places' && !this.root.contains(document.activeElement))this.render();
      if(event.message)this.feedback(event.message);if(event.error)this.feedback(event.error,true);return;
    }
    if(event.type!=='settings_center')return;
    this.state=event;
    if(!event.message&&!event.error&&/Подключаюсь|Нет связи|Загружаю/.test(id('settings-feedback').textContent??''))this.feedback('Сохранённые настройки загружены.');
    if(event.selected){this.selected=event.selected;this.draft=null;this.dirty=false;this.saving=false;}
    if(this.saving && !event.error && event.message){this.draft=null;this.dirty=false;this.saving=false;}
    if(!this.selected||!event.profiles.some(p=>p.id===this.selected))this.selected=event.active.id;
    const active=event.profiles.find(p=>p.id===event.active.id);
    id('settings-active').replaceChildren(el('span','Сейчас'),el('strong',active?.name??'Характер не выбран'),el('small',`${event.active.reason}${event.active.locked?' · автоматическая смена заблокирована':''}`),this.button('Настроить характер',()=>this.open('personality')));
    if(event.captured){this.previewText=this.capturedText=event.captured;const input=document.getElementById('personality-preview-text') as HTMLTextAreaElement|null;if(input)input.value=event.captured;const mic=document.getElementById('settings-mic-result');if(mic)mic.textContent=event.captured;}
    if(event.message)this.feedback(event.message);if(event.error){this.saving=false;this.feedback(event.error,true);}
    const focused=this.root.contains(document.activeElement)&&!!document.activeElement?.closest('form');
    if(!this.dirty&&(!focused||this.saving||event.selected))this.render();
    this.root.querySelectorAll<HTMLButtonElement>('button[data-write]').forEach(n=>n.disabled=!this.online||n.dataset.unavailable==='true');
  }
  private render():void {
    const content=id('settings-content');
    for(const nodes of this.legacy.values())for(const node of nodes)id('settings-old').append(node);
    content.replaceChildren();if(!this.page)return;
    if(this.page==='application'){void applicationSettings(content);return;}
    if(!this.state){content.append(el('p','Настройки появятся после подключения к Чушпану.','settings-empty'));return;}
    if(this.page==='personality')this.personality(content);
    if(this.page==='voice')this.voice(content);
    if(this.page==='memory')this.memory(content);
    if(this.page==='models')this.models(content);
    if(this.page==='access')this.access(content);
    if(this.page==='places')this.places(content);
    if(this.page==='integrations')this.integrations(content);
    if(this.page==='advanced')this.advanced(content);
    for(const node of this.legacy.get(this.page)??[])content.append(node);
    this.root.querySelectorAll<HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement>('input,select,textarea').forEach(n=>{if(!n.closest('#voice-settings,#dialogue-settings,#music-settings'))n.disabled=!this.online||n.dataset.unavailable==='true';});
  }
  private label(title:string,input:HTMLElement,description=''):HTMLElement {const label=el('label','','settings-field');label.append(el('span',title),input);if(description)label.append(el('small',description));return label;}
  private input(value:unknown,type='text'):HTMLInputElement {const input=document.createElement('input');input.type=type;if(type==='checkbox')input.checked=value===true;else input.value=String(value??'');return input;}
  private select(options:[string,string][],value:string):HTMLSelectElement {const select=document.createElement('select');for(const [v,label]of options){const o=document.createElement('option');o.value=v;o.textContent=label;select.append(o);}select.value=value;return select;}
  private textarea(value:unknown):HTMLTextAreaElement {const t=document.createElement('textarea');t.value=String(value??'');t.rows=3;return t;}
  private section(title:string,description=''):HTMLElement {const group=el('section','','settings-group');group.append(el('h2',title));if(description)group.append(el('p',description,'settings-help'));return group;}
  private form(category:string,fields:[string,string,string,([string,string][])?][]):HTMLFormElement {
    const values=this.state!.values[category];const form=document.createElement('form');form.className='settings-form';form.dataset.category=category;
    for(const [key,label,type,options]of fields){
      const value=values[key];const input=type==='select'?this.select(options!,String(value)):type==='textarea'||type==='list'?this.textarea(type==='list'?(value as string[]??[]).join('\n'):value):this.input(value,type);
      input.dataset.field=key;input.dataset.kind=type;
      if(type==='number'){(input as HTMLInputElement).min=key==='silence_ms'?'350':key==='temporary_days'?'1':'0';(input as HTMLInputElement).max=key==='silence_ms'?'2000':key==='pause_ms'?'1500':key==='temporary_days'?'3650':'100';}
      if(type==='range'){const range=input as HTMLInputElement;range.min='0';range.max='100';const output=el('output',String(value));range.oninput=()=>{output.textContent=range.value;this.feedback('Есть несохранённые изменения.');};const wrap=el('div','','settings-range');wrap.append(range,output);form.append(this.label(label,wrap));}
      else form.append(this.label(label,input));
      input.addEventListener('input',()=>this.feedback('Есть несохранённые изменения.'));
    }
    const save=this.button('Сохранить',()=>{},true);save.type='submit';save.className='primary';form.append(save);
    form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;const out=clone(values);form.querySelectorAll<HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement>('[data-field]').forEach(input=>{const key=input.dataset.field!;out[key]=input.dataset.kind==='checkbox'?(input as HTMLInputElement).checked:['range','number'].includes(input.dataset.kind!)?(input.value===''?null:Number(input.value)):input.dataset.kind==='list'?input.value.split('\n').map(s=>s.trim()).filter(Boolean):input.value;});this.write({op:'save',category,value:out});};
    return form;
  }
  private personality(root:HTMLElement):void {
    const state=this.state!;this.draft??=clone(state.profiles.find(p=>p.id===this.selected)??state.profiles[0]);const draft=this.draft;
    const bar=el('div','','settings-profile-bar');const picker=this.select(state.profiles.map(p=>[p.id,p.name+(p.builtin?' · готовый':'')]),draft.id);picker.id='personality-profile';picker.onchange=()=>{if(this.dirty&&!window.confirm('Отменить несохранённые изменения?')){picker.value=this.selected;return;}this.selected=picker.value;this.draft=null;this.dirty=false;this.render();};
    bar.append(this.label('Профиль',picker),this.button('Сделать активным',()=>this.write({op:'profile_select',id:draft.id,locked:state.active.locked}),true),this.button('Создать копию',()=>this.write({op:'profile_copy',id:draft.id}),true),this.button('Новый профиль',()=>this.write({op:'profile_copy',id:draft.id,name:'Мой новый характер'}),true));root.append(bar);
    root.append(el('p',draft.description,'settings-help'));
    const lock=this.input(state.active.locked,'checkbox');lock.onchange=()=>this.write({op:'profile_select',id:state.active.id,locked:lock.checked});root.append(this.label('Не менять характер автоматически',lock,`Сейчас: ${state.profiles.find(p=>p.id===state.active.id)?.name} · ${state.active.reason}`));
    const form=document.createElement('form');form.id='personality-editor';
    const name=this.input(draft.name);name.maxLength=80;name.required=true;name.oninput=()=>{draft.name=name.value;this.changed();};form.append(this.label('Название профиля',name));
    const tabs=el('div','','settings-tabs');tabs.setAttribute('role','tablist');
    for(const group of ['Стиль','Общение','Поведение']){const b=this.button(group,()=>{this.group=group;form.querySelectorAll<HTMLElement>('[data-trait-group]').forEach(n=>n.hidden=n.dataset.traitGroup!==group);tabs.querySelectorAll('button').forEach(n=>n.setAttribute('aria-selected',String(n.textContent===group)));});b.setAttribute('role','tab');b.setAttribute('aria-selected',String(this.group===group));tabs.append(b);}form.append(tabs);
    for(const group of ['Стиль','Общение','Поведение']){const box=el('div','','personality-sliders');box.dataset.traitGroup=group;box.hidden=this.group!==group;for(const [key,meta]of Object.entries(state.traits).filter(([,v])=>v.group===group)){const slider=this.input(draft.traits[key],'range');slider.min='0';slider.max='100';slider.id='personality-'+key;const out=el('output',String(draft.traits[key]));const row=el('div','','settings-slider');const label=el('label',meta.name);label.setAttribute('for',slider.id);row.append(label,out,slider);slider.oninput=()=>{draft.traits[key]=Number(slider.value);out.textContent=slider.value;this.changed();};box.append(row);}form.append(box);}
    form.append(el('p','Уровни описывают манеру общения, а не точные вероятности. Безопасность и проверка действий всегда сохраняются.','settings-help'));
    const communication=document.createElement('details');communication.className='settings-disclosure';communication.append(el('summary','Манера общения'));
    const cf:[string,string,string,[string,string][]?][]=[['address','Обращение','select',[['ты','На «ты»'],['вы','На «вы»']]],['preferred_addresses','Предпочитаемые обращения','list'],['unwanted_addresses','Нежелательные обращения','list'],['favorite_expressions','Любимые выражения','list'],['forbidden_expressions','Запрещённые выражения','list'],['jokes','Частота шуток','select',[['off','Без шуток'],['rare','Редко'],['normal','Когда уместно'],['frequent','Часто, когда уместно']]],['allow_profanity','Допускать мат','checkbox'],['response_style','Длина ответов','select',[['brief','Кратко'],['detailed','Подробно'],['adaptive','По ситуации']]],['use_context','Использовать тему разговора','checkbox'],['errors','При ошибке','select',[['direct','Причина и следующий шаг'],['supportive','Спокойно поддержать'],['brief','Коротко назвать проблему']]],['irritation','Если я раздражена','select',[['calm','Спокойно помочь'],['brief','Ответить кратко'],['supportive','Поддержать без нравоучений']]],['serious','Серьёзный разговор','select',[['calm','Спокойно и точно'],['supportive','Чутко и бережно'],['business','Деловито']]]];
    for(const [key,label,type,options]of cf){const value=draft.communication[key];const input=type==='select'?this.select(options!,String(value)):type==='list'?this.textarea((value as string[]).join('\n')):this.input(value,'checkbox');input.onchange=()=>{draft.communication[key]=type==='checkbox'?(input as HTMLInputElement).checked:type==='list'?input.value.split('\n').map(s=>s.trim()).filter(Boolean):input.value;this.changed();};communication.append(this.label(label,input));}form.append(communication);
    const custom=this.textarea(draft.instructions);custom.id='personality-instructions';custom.maxLength=4000;custom.rows=4;custom.placeholder='Например: подшучивай над прокрастинацией, но в вопросах учёбы отвечай по делу.';custom.oninput=()=>{draft.instructions=custom.value;this.changed();};form.append(this.label('Как Чушпан должен себя вести?',custom,'Пожелания сохраняются локально. Они не дают новых прав приложениям или инструментам.'));
    const save=this.button(draft.builtin?'Сохранить как свой профиль':'Сохранить профиль',()=>{},true);save.type='submit';save.className='primary';const actions=el('div','','settings-actions');actions.append(save,this.button('Отменить изменения',()=>{this.draft=null;this.dirty=false;this.render();}));
    const remove=this.button('Удалить профиль',()=>{if(window.confirm(`Удалить профиль «${draft.name}»? Активный профиль и предустановки защищены.`))this.write({op:'profile_delete',id:draft.id,revision:draft.revision,confirm:true});},true);remove.className='danger';if(draft.builtin||draft.id===state.active.id){remove.disabled=true;remove.dataset.unavailable='true';}actions.append(remove);form.append(actions);
    form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;const profile=clone(draft);if(profile.builtin){profile.id=crypto.randomUUID().replaceAll('-','');profile.revision=0;profile.builtin=false;profile.name+=' — мой';}this.saving=true;this.write({op:'profile_save',profile});};root.append(form);
    const preview=this.section('Проверить характер','Одна реплика, одна локальная модель, без инструментов и истории. Активный характер не меняется.');const text=this.textarea(this.previewText);text.id='personality-preview-text';text.maxLength=1000;text.oninput=()=>this.previewText=text.value;preview.append(this.label('Тестовая реплика',text));const other=this.select([['','Без сравнения'],...state.profiles.map(p=>[p.id,p.name] as [string,string])],'');preview.append(this.label('Сравнить с профилем',other));const speak=this.input(false,'checkbox');preview.append(this.label('Озвучить результат',speak));
    preview.append(this.button('Проверить характер',()=>{this.previews.clear();const drafts=[clone(this.draft!)];if(other.value)drafts.push(clone(state.profiles.find(p=>p.id===other.value)!));this.send({op:'preview',drafts,text:text.value,speak:speak.checked});this.feedback('Готовлю локальный предпросмотр…');},true),this.button('Произнести тестовую реплику',()=>this.write({op:'listen'}),true),this.button('Остановить тест',()=>this.send({op:'preview_cancel'}),true));const results=el('div','','settings-previews');results.id='settings-preview-results';preview.append(results);root.append(preview);this.renderPreviews();
    root.append(this.rules());
  }
  private changed():void {this.dirty=true;this.feedback('Есть несохранённые изменения характера.');}
  private renderPreviews():void {const root=document.getElementById('settings-preview-results');if(!root)return;root.replaceChildren(...[...this.previews.values()].map(p=>{const box=el('article','','settings-preview');box.append(el('h3',p.name),el('p',p.text),el('small',`${p.model} · первый текст ${sec(p.first_token_s)} · первый звук ${sec(p.first_sound_s)} · весь ответ ${sec(p.total_s)}${p.done?'':' · ответ ещё идёт'}`));return box;}));}
  private rules():HTMLElement {
    const state=this.state!;const section=this.section('Характер по ситуации','Переключения сохраняются как обычные сценарии. Ручная блокировка выше имеет приоритет.');
    const form=document.createElement('form');const profile=this.select(state.profiles.map(p=>[p.id,p.name]),state.active.id);const kind=this.select([['manual','Вручную или голосовой фразой'],['schedule','По расписанию'],['location','При выборе места'],['time','После указанного часа']],'manual');const at=this.input('22:00','time');const place=this.select([['general','Общий'],...state.places.map(p=>[p.id,p.name] as [string,string])],'general');const hour=this.input(22,'number');hour.min='0';hour.max='23';const phrase=this.input('');
    form.append(this.label('Характер',profile),this.label('Когда включать',kind),this.label('Время расписания',at),this.label('Место',place),this.label('Час, от 0 до 23',hour),this.label('Голосовая фраза, необязательно',phrase));const update=()=>{at.parentElement!.hidden=kind.value!=='schedule';place.parentElement!.hidden=kind.value!=='location';hour.parentElement!.hidden=kind.value!=='time';};kind.onchange=update;update();const save=this.button('Создать правило',()=>{},true);save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();this.write({op:'rule',profile_id:profile.value,kind:kind.value,at:at.value,place_id:place.value,hour:Number(hour.value),phrase:phrase.value});};section.append(form);
    for(const rule of state.rules){const row=el('div','','settings-row');row.append(el('span',rule.name),this.button('Открыть в сценариях',()=>this.navigate('automations')));section.append(row);}return section;
  }
  private voice(root:HTMLElement):void {
    const s=this.state!;root.append(el('p',`Текущий движок: ${s.engine}. Голос сохраняется без подмены системным TTS.`,'settings-help'));
    const rate:[string,string][]=[['x-slow','Очень медленно'],['slow','Медленно'],['medium','Обычно'],['fast','Быстро'],['x-fast','Очень быстро']];
    const form=this.form('studio',[['voice','Голос','select',s.voices.map(v=>[v,v])],['rate','Скорость речи','select',rate],['pitch','Высота голоса','select',[['x-low','Очень низко'],['low','Ниже'],['medium','Без изменения'],['high','Выше'],['x-high','Очень высоко']]],['volume','Громкость ответа, %','range'],['pause_ms','Пауза между фразами, мс','number'],['wake_phrase','Фраза активации','text'],['silence_ms','Пауза окончания речи, мс','number'],['adaptive_endpoint','Учитывать длинные паузы в речи','checkbox'],['normalize_numbers','Произносить числа словами','checkbox'],['normalize_english','Подготавливать английские слова для русского голоса','checkbox'],['text_only','Отвечать текстом без озвучки','checkbox']]);
    const micValue=s.values.studio.input_device;
    const microphone=this.select([['','Системный микрофон'],...(s.input_devices??[]).map(d=>[String(d.id),d.name] as [string,string]),...(micValue!=null&&!(s.input_devices??[]).some(d=>String(d.id)===String(micValue))?[[String(micValue),'Текущий микрофон: '+String(micValue)] as [string,string]]:[])],String(micValue??''));
    form.append(this.label('Микрофон',microphone,'Смена устройства перезапускает только аудиовход.'));
    const originalMic=form.onsubmit;form.onsubmit=e=>{s.values.studio.input_device=microphone.value===''?null:/^\d+$/.test(microphone.value)?Number(microphone.value):microphone.value;originalMic?.call(form,e);};
    form.append(this.button('Обновить список микрофонов',()=>this.send({op:'microphones'}),true));
    for(const [key,supported]of [['rate',s.voice_support.rate],['pitch',s.voice_support.pitch]] as [string,boolean][]){if(!supported){const field=form.querySelector<HTMLSelectElement>(`[data-field="${key}"]`)!;field.disabled=true;field.dataset.unavailable='true';field.parentElement!.append(el('small','Текущий движок не поддерживает этот параметр.'));}}
    form.append(el('p','Отдельная настройка выразительности текущим движком не предоставляется.','settings-help'));
    const dictionary=s.values.studio.pronunciations as Record<string,string>;const words=this.textarea(Object.entries(dictionary).filter(([,v])=>!/[+\u0301]/.test(v)).map(([k,v])=>`${k} = ${v}`).join('\n'));const stress=this.textarea(Object.entries(dictionary).filter(([,v])=>/[+\u0301]/.test(v)).map(([k,v])=>`${k} = ${v}`).join('\n'));words.rows=4;stress.rows=3;
    form.append(this.label('Произношение слов',words,'Одна строка: слово = как произносить'),this.label('Словарь ударений',stress,'Например: Чушпан = Чушпа́н. Можно использовать знак + перед гласной.'));
    const original=form.onsubmit;form.onsubmit=e=>{const parsed:Record<string,string>={};for(const line of (words.value+'\n'+stress.value).split('\n').filter(s=>s.trim())){const index=line.indexOf('=');if(index<1){e.preventDefault();this.feedback('В словаре нужна запись «слово = произношение».',true);return;}const key=line.slice(0,index).trim(),value=line.slice(index+1).trim();if(parsed[key]&&parsed[key]!==value){e.preventDefault();this.feedback('Одно слово указано с разным произношением.',true);return;}parsed[key]=value;}s.values.studio.pronunciations=parsed;original?.call(form,e);};
    root.append(form);const test=this.section('Попробовать голос и микрофон');const phrase=this.input('Привет. Это мой текущий голос.');test.append(this.label('Текст для проверки голоса',phrase),this.button('Прослушать',()=>this.send({op:'voice_test',text:phrase.value}),true),this.button('Проверить микрофон',()=>this.send({op:'listen'}),true),this.button('Проверить wake-фразу',()=>this.send({op:'wake_test'}),true),this.button('Остановить проверку микрофона',()=>this.send({op:'listen_stop'}),true));const result=el('p',this.capturedText,'settings-help');result.id='settings-mic-result';test.append(result,el('small','Микрофон включается для одного теста после нажатия. Файл не сохраняется, команды из теста не выполняются.'));root.append(test);
  }
  private memory(root:HTMLElement):void {
    const state=this.state!;const tabs=el('div','','settings-tabs');for(const [key,label]of [['about','Обо мне'],['facts','Что запомнил'],['learned','Чему научился']]){const b=this.button(label,()=>{this.memoryTab=key;this.render();});b.setAttribute('aria-selected',String(key===this.memoryTab));tabs.append(b);}root.append(tabs);
    if(this.memoryTab==='about'){root.append(this.form('about',[['name','Как тебя зовут','text'],['address','Предпочитаемое обращение','text'],['preferences','Что для меня важно','textarea']]));return;}
    if(this.memoryTab==='learned'){const habits=this.input(state.habits_opt_in,'checkbox');habits.onchange=()=>this.write({op:'habits',enabled:habits.checked});root.append(this.label('Замечать повторяющиеся успешные действия',habits));root.append(el('p','Чушпан может предлагать сценарии. Самостоятельно создавать или запускать их это не разрешает.','settings-help'));for(const row of state.memory.filter(r=>['habit','correction','preference'].includes(r.category)&&r.status==='active'))root.append(el('p',row.content));for(const row of state.training??[])root.append(el('p',`${row.phrase} · ${row.operation}`));root.append(this.button('Голосовые фразы и обучение сценариев',()=>this.navigate('automations')));return;}
    const search=this.input('','search');search.placeholder='Найти воспоминание';root.append(this.label('Поиск по памяти',search));const list=el('div');const selected=new Set<number>();const render=()=>{list.replaceChildren();for(const memory of state.memory.filter(m=>m.status==='active'&&m.category!=='personality'&&m.content.toLocaleLowerCase().includes(search.value.toLocaleLowerCase()))){const row=el('div','','settings-memory-row');const check=this.input(selected.has(memory.id),'checkbox');check.setAttribute('aria-label','Выбрать: '+memory.content);check.onchange=()=>check.checked?selected.add(memory.id):selected.delete(memory.id);const text=this.textarea(memory.content);text.rows=2;text.maxLength=300;row.append(check,text,this.button('Сохранить запись',()=>this.write({op:'memory_edit',id:memory.id,revision:memory.revision,content:text.value}),true));list.append(row);}if(!list.childElementCount)list.append(el('p','Подходящих активных воспоминаний пока нет.','settings-empty'));};search.oninput=render;render();root.append(list,this.button('Удалить выбранные записи',()=>{if(selected.size&&window.confirm(`Удалить выбранные воспоминания (${selected.size})? История разговоров сохранится.`))this.write({op:'memory_clear',ids:[...selected],confirm:true});},true),this.button('Экспорт памяти',()=>this.send({op:'export',scope:'memory'}),true));
    for(const p of state.memory_pending){const row=el('div','','settings-row');row.append(el('span',p.content),this.button('Запомнить',()=>this.write({op:'memory_approve',id:p.id,approve:true}),true),this.button('Не сохранять',()=>this.write({op:'memory_approve',id:p.id,approve:false}),true));root.append(row);}
    const policy=this.section('Как запоминать');const form=this.form('memory',[['automatic','Автоматическое запоминание','select',[['auto','Сохранять разрешённые факты'],['ask','Только после подтверждения'],['off','Только по прямой просьбе']]],['temporary_days','Срок актуальности временных событий и привычек, дней','number']]);const names:Record<string,string>={profile:'Сведения обо мне',preference:'Предпочтения',habit:'Привычки',project:'Проекты',episode:'Временные события',correction:'Исправления'};const excluded=new Set(state.values.memory.excluded as string[]);for(const [key,label]of Object.entries(names)){const check=this.input(excluded.has(key),'checkbox');check.onchange=()=>check.checked?excluded.add(key):excluded.delete(key);form.append(this.label('Не запоминать: '+label,check));}const original=form.onsubmit;form.onsubmit=e=>{state.values.memory.excluded=[...excluded];original?.call(form,e);};policy.append(form);root.append(policy);
  }
  private models(root:HTMLElement):void {
    const state=this.state!;const installed=state.models.installed??[];const current=String(state.values.models.local_model);const models=[...new Set([current,...installed])];root.append(this.form('models',[['provider','Основная модель','select',[['gemini','Gemini'],['ollama','Локальная Ollama']]],['cloud_model','Модель Gemini','select',['gemini-3.8-flash','gemini-3.6-flash','gemini-3.5-flash','gemini-3.5-flash-lite'].map(v=>[v,v])],['cloud_fallback','Резервная модель Gemini','select',[['','Без второй облачной попытки'],['gemini-3.5-flash','gemini-3.5-flash']]],['local_model','Локальная модель','select',models.map(v=>[v,v])],['fallback','Резервная Ollama при работе через Gemini','checkbox'],['mode','Режим обработки','select',[['fast','Быстрый'],['balanced','Сбалансированный'],['quality','Качественный']]],['streaming','Начинать озвучку до завершения ответа','checkbox'],['thinking','Рассуждения Ollama','select',[['auto','Автоматически: простые вопросы без рассуждений'],['on','Всегда включены'],['off','Выключены по моему выбору']]],['cloud_thinking','Рассуждения Gemini для сложных запросов','select',[['auto','По умолчанию модели'],['low','Низкий уровень'],['medium','Средний уровень'],['high','Высокий уровень']]],['cloud_timeout_s','Gemini: тайм-аут ответа, с','number'],['cloud_first_result_s','Gemini: общий бюджет до первого результата, с','number'],['cloud_idle_s','Gemini: ожидание следующего фрагмента, с','number'],['local_first_result_s','Ollama: ожидание первого результата, с','number'],['local_idle_s','Ollama: ожидание следующего фрагмента, с','number']]));root.append(el('p','Быстрый режим сокращает ожидание облака для простых вопросов. Качественный сохраняет рассуждение и больший бюджет ожидания. Локальные команды остаются локальными.','settings-help'));
    const form=root.querySelector('form[data-category="models"]')!;
    const advanced=el('details','','settings-group');advanced.append(el('summary','Дополнительные настройки скорости'));
    advanced.append(el('p','Короткий тайм-аут может прервать медленный ответ. Низкий уровень рассуждений может снизить качество сложных задач. Ожидание озвучки не расходует сетевой тайм-аут. Основная модель используется первой; резерв включается при допустимой ошибке.','settings-help'));
    for(const field of ['thinking','cloud_thinking','cloud_timeout_s','cloud_first_result_s','cloud_idle_s','local_first_result_s','local_idle_s']) {
      const input=form.querySelector<HTMLInputElement|HTMLSelectElement>(`[data-field="${field}"]`)!;
      if(input.tagName==='INPUT'){(input as HTMLInputElement).min=field==='cloud_timeout_s'?'5':'1';(input as HTMLInputElement).max=field==='cloud_idle_s'?'30':field==='local_first_result_s'?'180':'120';(input as HTMLInputElement).step='0.5';(input as HTMLInputElement).placeholder='Автоматически по режиму';}
      advanced.append(input.closest('label')!);
    }
    form.insertBefore(advanced,form.querySelector('button[type="submit"]'));
    root.append(this.button('Проверить доступность моделей',()=>this.send({op:'models_status'}),true),el('p',`Ollama: ${timeLabels[state.models.ollama??'not_checked']??'не проверена'} · TTS: ${timeLabels[state.models.tts??'not_checked']??'не проверен'} · потоковая генерация ${state.values.models.streaming===false?'выключена':'включена'}`,'settings-help'));
    const cloud=this.section('Gemini','Статус последних запросов. Эта проверка не отправляет платный запрос.');const health=state.models.gemini?.models?.filter(m=>m.provider==='gemini')??[];for(const model of health)cloud.append(el('p',`${model.model} · ${{unknown:'ещё нет запроса',healthy:'последний запрос успешен',ok:'последний запрос успешен',cancelled:'прервано',error:'ошибка',switching:'переход на резерв',ready:'доступен',open:'временно отключён после ошибки',probing:'пробное восстановление',failed:'ошибка'}[model.status]??model.status}${model.latency_s==null?'':` · ${sec(model.latency_s)}`}${model.error?` · ${model.error}`:''}`));if(!health.length)cloud.append(el('p','Облачный клиент не активен или запросов ещё не было.','settings-help'));root.append(cloud);
    const last=el('p',this.lastLatency,'settings-help');last.id='settings-last-latency';root.append(last);
    const speed=this.section('Проверить скорость','Один фиксированный вопрос локальной модели. Платные облачные API не вызываются.');const speak=this.input(false,'checkbox');speed.append(this.label('Измерить и первый слышимый аудиоблок',speak),this.button('Проверить скорость',()=>{this.previews.clear();this.send({op:'speed_test',speak:speak.checked});this.feedback('Измеряю локальный ответ…');},true));const result=el('div','','settings-previews');result.id='settings-preview-results';speed.append(result);root.append(speed);const alternate=this.select([['','Выбери другую установленную модель'],...installed.filter(v=>v!==current).map(v=>[v,v] as [string,string])],'');speed.append(this.label('Сравнить модели на одном вопросе',alternate),this.button('Сравнить модели',()=>{if(!alternate.value){this.feedback('Сначала проверь доступность и выбери другую модель.',true);return;}this.previews.clear();this.send({op:'speed_test',models:[current,alternate.value],speak:speak.checked});},true));this.renderPreviews();
    root.append(this.button('Подробная статистика и сравнение до / после',()=>this.navigate('performance')));const errors=this.section('Последние ошибки');for(const error of state.last_errors)errors.append(el('p',`${new Date(error.created_at).toLocaleString()} · ${error.model??'Локально'} · ${error.errors.join(', ')||'Завершение не подтверждено'}`));if(!state.last_errors.length)errors.append(el('p','Сохранённых ошибок нет.','settings-help'));root.append(errors);
  }
  private access(root:HTMLElement):void {
    const state=this.state!;root.append(this.button('Проверить разрешения macOS',()=>this.send({op:'permissions'}),true));if(state.permissions){root.append(el('p',state.permissions.scope,'settings-help'));for(const permission of state.permissions.items){const row=el('div','','settings-permission');const link=el('a','Открыть системные настройки') as HTMLAnchorElement;link.href=permission.url;row.append(el('strong',permission.name),el('span',timeLabels[permission.status]??permission.status),el('small',permission.note),link);root.append(row);}}
    const actions=this.section('Разрешённые действия','Опасные операции требуют отдельного подтверждения. Ни один профиль характера не может отменить это правило.');
    const disabled=new Set(state.values.access.disabled_tools as string[]);
    for(const [risk,title] of [['safe','Чтение и безопасные проверки'],['state_change','Изменение состояния'],['dangerous','Опасные операции — с подтверждением']]){
      const group=el('details');group.append(el('summary',title));
      for(const action of state.actions.filter(a=>a.risk===risk)){
        const check=this.input(!disabled.has(action.id),'checkbox');
        check.onchange=()=>{check.checked?disabled.delete(action.id):disabled.add(action.id);this.write({op:'save',category:'access',value:{...state.values.access,disabled_tools:[...disabled]}});};
        group.append(this.label(action.description.split('.')[0]||action.id,check,action.description));
      }
      actions.append(group);
    }
    root.append(actions,this.form('access',[['blocked_apps','Не открывать и не управлять этими приложениями напрямую','list']]));
    const history=this.section('История действий');for(const run of state.recent_actions)history.append(el('p',`${run.scenario_name??run.name??'Сценарий'} · ${run.status??'Результат не определён'}`));if(!state.recent_actions.length)history.append(el('p','Запусков сценариев пока нет.','settings-help'));root.append(history);
  }
  private places(root:HTMLElement):void {
    const state=this.state!;const selection=this.select([['auto','Автоматически'],['general','Общий'],...state.places.map(p=>[p.id,p.name] as [string,string])],state.location.mode==='manual'?state.location.place_id??'general':'auto');selection.onchange=()=>this.automation({op:'location',mode:selection.value==='auto'?'auto':'manual',place_id:selection.value==='auto'?'':selection.value});root.append(this.label('Текущее место',selection),el('p',`${state.location.name??'Общий'} · ${state.location.message??state.location.reason??'Актуальная геолокация или ручной выбор'}`,'settings-help'),this.button('Обновить геолокацию',()=>this.automation({op:'location_connect',consent:true}),true),this.button('Прочитать текущий Wi-Fi и подключённые устройства',()=>this.send({op:'signals'}),true),el('p','Сигналы влияют на выбор места только после явной привязки ниже. Ручной выбор имеет приоритет. Точные координаты не нужны и здесь не сохраняются.','settings-help'));
    if(state.signals.error)root.append(el('p',state.signals.error,'settings-help'));
    const general=this.section('Общий','Всегда существует. Используется, когда место неизвестно или локальная версия сценария отсутствует.');root.append(general);
    for(const place of state.places){const form=document.createElement('form');form.className='settings-group';form.append(el('h2',place.name));const name=this.input(place.name);const cities=this.textarea(place.cities.join('\n'));form.append(this.label('Название места',name),this.label('Города и их варианты написания',cities));const selected=clone((place as Place & {signals?:Signal[]}).signals??[]);const seen=state.signals.signals??[];for(const signal of [...selected,...seen.filter(s=>!selected.some(p=>p.key===s.key&&p.kind===s.kind))]){const check=this.input(selected.some(s=>s.key===signal.key),'checkbox');check.onchange=()=>{const index=selected.findIndex(s=>s.key===signal.key);if(check.checked&&index<0)selected.push(signal);if(!check.checked&&index>=0)selected.splice(index,1);};form.append(this.label(`${signal.kind==='wifi'?'Wi-Fi':'Устройство'}: ${signal.name}`,check));}const save=this.button('Сохранить место и привязки',()=>{},true);save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();this.automation({op:'place_save',place:{...place,name:name.value,cities:cities.value.split('\n').map(s=>s.trim()).filter(Boolean),signals:selected}});};root.append(form);}
    root.append(this.button('Версии сценариев и другие места',()=>this.navigate('automations')));
  }
  private integrations(root:HTMLElement):void {
    for(const integration of this.state!.integrations){const box=this.section(integration.name);box.append(el('p',!integration.supported?'Не подключена к текущей сборке':`${integration.enabled?'Включена':'Отключена'} · ${timeLabels[integration.status]??integration.status}`));if(integration.error)box.append(el('p',integration.error,'settings-error'));if(integration.details.length)box.append(el('p',integration.details.join(', ')));for(const tool of integration.tools){const action=this.state!.actions.find(a=>a.id===tool);box.append(el('small',action?.description??tool));}if(integration.id==='calendar'){box.append(this.button('Проверить календарь',()=>this.send({op:'probe',id:'calendar'}),true),this.button(integration.enabled?'Отключить календарь':'Подключить календарь',()=>this.send({op:'calendar',enabled:!integration.enabled}),true));}else if(integration.supported){box.append(this.button('Проверить соединение',()=>this.send({op:'probe',id:integration.id}),true),this.button(integration.enabled?'Отключить':'Включить',()=>{const access=clone(this.state!.values.access);const disabled=new Set(access.disabled_tools as string[]);for(const t of integration.tools)integration.enabled?disabled.add(t):disabled.delete(t);access.disabled_tools=[...disabled];this.write({op:'save',category:'access',value:access});},true));}root.append(box);}
  }
  private advanced(root:HTMLElement):void {
    const quiet=this.section('Тихие часы','В заданное время ответы остаются в тексте. Можно отдельно остановить инициативные напоминания.');const form=this.form('quiet',[['enabled','Включить тихие часы','checkbox'],['start','Начало','time'],['end','Окончание','time'],['mute','Не произносить ответы вслух','checkbox'],['pause_initiative','Не проявлять инициативу','checkbox']]);const days=new Set(this.state!.values.quiet.weekdays as number[]);const weekdays=el('div','','settings-weekdays');['Пн','Вт','Ср','Чт','Пт','Сб','Вс'].forEach((day,n)=>{const check=this.input(days.has(n),'checkbox');check.onchange=()=>check.checked?days.add(n):days.delete(n);weekdays.append(this.label(day,check));});form.append(weekdays);const original=form.onsubmit;form.onsubmit=e=>{this.state!.values.quiet.weekdays=[...days].sort();original?.call(form,e);};quiet.append(form);root.append(quiet,this.form('notifications',[['reminders','Произносить напоминания и таймеры','checkbox'],['scenarios','Сообщать о завершении сценариев','checkbox']]));
    const backup=this.section('Резервная копия','Настройки, профили, сценарии и память. API-ключи, пароли и история разговоров в обычный экспорт не входят.');const file=this.input('','file');file.accept='.json,application/json';file.onchange=async()=>{const selected=file.files?.[0];if(!selected)return;if(selected.size>2000000){this.feedback('Размер копии должен быть до 2 МБ.',true);return;}try{this.send({op:'import_preview',document:JSON.parse(await selected.text())});}catch{this.feedback('Не удалось прочитать JSON-файл.',true);}};backup.append(this.button('Экспортировать локальную копию',()=>this.send({op:'export',scope:'all'}),true),this.label('Восстановить из файла',file));const result=el('div');result.id='settings-backup-result';backup.append(result);root.append(backup);
    const diagnostics=this.section('Диагностика');diagnostics.append(this.button('Задержки, компоненты и ошибки',()=>this.navigate('performance')),this.button('Разрешения и доступность устройств',()=>this.open('access')));root.append(diagnostics);
    const reset=this.section('Сброс категории','Обычный сброс не удаляет историю разговоров или воспоминания.');const category=this.select([['personality','Характер активного профиля'],['studio','Голосовая студия'],['memory','Правила памяти'],['models','Модели'],['about','Обо мне'],['quiet','Тихие часы'],['notifications','Уведомления'],['access','Ограничения действий'],['places','Привязки мест и ручной выбор'],['integrations','Ограничения интеграций'],['advanced','Тихие часы, уведомления и Ambient']],'personality');reset.append(this.label('Что сбросить',category),this.button('Сбросить выбранную категорию',()=>{if(window.confirm('Сбросить только выбранную категорию? История и память останутся.'))this.write({op:'reset',category:category.value,confirm:true});},true));root.append(reset);
  }
}

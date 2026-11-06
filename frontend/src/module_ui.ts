import type {HudEvent} from './types';

export type ModuleChannel = 'memory_library'|'mac_center'|'device_sync'|'analytics';
export interface ModuleEvent {type:ModuleChannel;operation_id?:string;busy?:boolean;capturing?:boolean;error?:string;message?:string|null;data?:unknown;download?:{name:string;mime:string;content?:string;base64?:string};}
export const el = <K extends keyof HTMLElementTagNameMap>(tag:K, value='', cls=''):HTMLElementTagNameMap[K] => {const node=document.createElement(tag);node.textContent=value;node.className=cls;return node;};
export const date = (value:string|number|null|undefined):string => {if(value==null||value==='')return 'Ещё нет данных';const normalized=typeof value==='string'&&/^\d{4}-\d\d-\d\d[T ]\d\d:\d\d/.test(value)&&!/(?:Z|[+-]\d\d:\d\d)$/.test(value)?value.replace(' ','T')+'Z':value;const parsed=new Date(typeof normalized==='number'?normalized*1000:normalized);return Number.isNaN(parsed.getTime())?'Дата недоступна':parsed.toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});};
export const duration = (value:number|null|undefined):string => value==null?'Нет данных':value<60?`${Math.round(value)} с`:`${Math.floor(value/3600)} ч ${Math.floor(value%3600/60)} м`;
export const seconds = (value:number|null|undefined):string => value==null?'Нет данных':`${value.toFixed(2)} с`;
export const card = (title:string, description=''):HTMLElement => {const node=el('article','','module-card');node.append(el('h2',title));if(description)node.append(el('p',description,'muted'));return node;};
export const empty = (title:string, description:string):HTMLElement => {const node=card(title,description);node.classList.add('module-empty');return node;};
export const grid = (...children:HTMLElement[]):HTMLElement => {const node=el('div','','module-grid');node.append(...children);return node;};
export const metric = (label:string,value:string,detail=''):HTMLElement => {const node=el('article','','module-metric');node.append(el('small',label),el('strong',value));if(detail)node.append(el('span',detail));return node;};
export const row = (...children:HTMLElement[]):HTMLElement => {const node=el('div','','module-actions');node.append(...children);return node;};
export function download(file:NonNullable<ModuleEvent['download']>):void {const content=file.base64?Uint8Array.from(atob(file.base64),c=>c.charCodeAt(0)):file.content??'';const url=URL.createObjectURL(new Blob([content],{type:file.mime}));const anchor=el('a');anchor.href=url;anchor.download=file.name;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);}
export interface FormField {name:string;label:string;kind?:'text'|'textarea'|'select'|'checkbox'|'number'|'date';value?:string|number|boolean;options?:[string,string][];max?:number;min?:number;required?:boolean;}
export function formFields(fields:FormField[]):{form:HTMLFormElement;read:()=>Record<string,string|boolean>} {
  const form=el('form','','module-form');
  for(const field of fields){const label=el('label',field.label);let input:HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement;
    if(field.kind==='textarea')input=el('textarea');else if(field.kind==='select'){input=el('select');for(const [value,text] of field.options??[]){const option=el('option',text);option.value=value;input.append(option);}}else{input=el('input');input.type=field.kind??'text';}
    input.name=field.name;input.required=field.required??false;
    if(input instanceof HTMLInputElement&&field.kind==='checkbox'){input.checked=field.value===true;label.className='module-check';}else input.value=String(field.value??'');
    if(input instanceof HTMLInputElement||input instanceof HTMLTextAreaElement){if(field.max!=null){if(field.kind==='number')input.setAttribute('max',String(field.max));else input.maxLength=field.max;}if(field.min!=null)input.setAttribute('min',String(field.min));}
    label.append(input);form.append(label);
  }
  return {form,read:()=>{const result:Record<string,string|boolean>={};for(const field of fields){const input=form.elements.namedItem(field.name) as HTMLInputElement;result[field.name]=field.kind==='checkbox'?input.checked:input.value;}return result;}};
}
export function dialog(title:string, body:HTMLElement,owner?:string):HTMLDialogElement {const modal=el('dialog','','module-dialog');if(owner)modal.dataset.moduleOwner=owner;modal.setAttribute('aria-label',title);const close=el('button','Закрыть','module-close');close.type='button';close.onclick=()=>modal.close();modal.append(row(el('h2',title),close),body);modal.addEventListener('close',()=>modal.remove());document.body.append(modal);modal.showModal();return modal;}

export abstract class ModulePage<T> {
  protected online=false;protected busy=false;protected state:T|null=null;protected tab='overview';
  protected content:HTMLElement;protected tabs:HTMLElement;protected feedback:HTMLElement;protected toolbar:HTMLElement;
  private timer=0;private pending='';private queuedRead:object|null=null;
  constructor(readonly channel:ModuleChannel,readonly view:string, title:string, description:string,
    private post:(channel:ModuleChannel,value:object)=>void, protected navigate:(view:string)=>void,
    tabs:[string,string][]) {
    const root=document.getElementById('module-'+view)!;root.className='workspace-page module-page module-'+view;
    const heading=el('div','','page-heading');heading.append(el('small','ТВОЙ ЧУШПАН'),el('h1',title),el('p',description));
    this.toolbar=el('div','','page-toolbar');this.tabs=el('div','','module-tabs');this.tabs.setAttribute('role','tablist');this.tabs.setAttribute('aria-label',title);
    this.content=el('div','','module-content');this.content.id=view+'-panel';this.content.setAttribute('role','tabpanel');
    this.feedback=el('p','Нет связи с Чушпаном.','module-feedback');this.feedback.setAttribute('role','status');
    for(const [key,label]of tabs){const button=el('button',label);button.type='button';button.dataset.tab=key;button.setAttribute('role','tab');button.setAttribute('aria-controls',this.content.id);button.id=`${view}-tab-${key}`;button.onclick=()=>{this.tab=key;this.render();};this.tabs.append(button);}
    this.tabs.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const buttons=[...this.tabs.querySelectorAll('button')];const current=buttons.indexOf(document.activeElement as HTMLButtonElement);const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(current+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;buttons[next]?.focus();buttons[next]?.click();});
    root.append(heading,this.toolbar,this.tabs,this.feedback,this.content);
    this.begin();
    document.addEventListener('workspace:navigate',event=>{if((event as CustomEvent<string>).detail===view&&this.online)this.refresh();});
  }
  connection(online:boolean):void {this.online=online;clearTimeout(this.timer);this.busy=false;this.pending='';this.queuedRead=null;delete this.feedback.dataset.error;this.feedback.textContent=online?'Загружаю локальные данные…':'Нет соединения. Последние данные могут быть устаревшими.';if(!this.state)this.begin();if(online)this.refresh();this.controls();}
  receive(event:HudEvent):boolean {
    if(event.type!==this.channel)return false;const value=event as ModuleEvent;
    if(value.operation_id&&value.operation_id!==this.pending)return false;
    if(value.busy!==undefined){this.busy=value.busy;if(value.busy)this.feedback.textContent='Выполняю…';else{clearTimeout(this.timer);this.pending='';}}
    if(value.error){this.feedback.textContent=value.error;this.feedback.dataset.error='true';}
    else if(value.message){this.feedback.textContent=value.message;delete this.feedback.dataset.error;}
    else if(value.busy===false){this.feedback.textContent='Данные обновлены.';delete this.feedback.dataset.error;}
    if(value.data){this.state=value.data as T;const focused=document.getElementById('module-'+this.view)?.contains(document.activeElement)&&document.activeElement?.matches('input,select,textarea');if(!focused||value.busy===false)this.render();}
    else if(value.error&&!this.state)this.begin();
    if(value.download)download(value.download);this.controls();
    if(value.busy===false&&this.queuedRead){const next=this.queuedRead;this.queuedRead=null;this.send(next);}
    return true;
  }
  protected send(value:object):void {
    if(!this.online){this.feedback.textContent='Нет связи с Чушпаном. Действие не отправлено.';return;}
    const op=(value as {op?:string}).op;
    if(this.busy&&op!=='cancel'){if(['snapshot','query','list'].includes(op??''))this.queuedRead=value;return;}
    if(op==='cancel'){this.post(this.channel,value);return;}
    this.pending=window.crypto.randomUUID();this.busy=true;this.feedback.textContent='Выполняю…';delete this.feedback.dataset.error;this.controls();this.post(this.channel,{...value,request_id:this.pending});clearTimeout(this.timer);
    this.timer=window.setTimeout(()=>{this.feedback.textContent='Ответ не получен вовремя. Обнови данные; результат не подтверждён.';this.feedback.dataset.error='true';this.busy=false;this.pending='';this.controls();if(!this.state)this.begin();},36000);
  }
  protected controls():void {this.content.setAttribute('aria-busy',String(this.busy));for(const root of [document.getElementById('module-'+this.view),...document.querySelectorAll<HTMLElement>(`.module-dialog[data-module-owner="${this.view}"]`)]){root?.querySelectorAll<HTMLButtonElement>('button[data-write]').forEach(button=>button.disabled=button.dataset.unavailable==='true'||!this.online||(this.busy&&button.dataset.allowBusy!=='true'));root?.querySelectorAll<HTMLInputElement>('input,select,textarea').forEach(input=>input.disabled=!this.online);const status=root?.querySelector<HTMLElement>('.module-dialog-feedback');if(status){status.textContent=this.feedback.textContent;status.dataset.error=this.feedback.dataset.error??'false';}}}
  protected button(label:string,action:()=>void,primary=false,allowBusy=false):HTMLButtonElement {const button=el('button',label,primary?'primary':'');button.type='button';button.dataset.write='true';if(allowBusy)button.dataset.allowBusy='true';button.disabled=!this.online||(this.busy&&!allowBusy);button.onclick=action;return button;}
  protected openDialog(title:string,body:HTMLElement):HTMLDialogElement {const modal=dialog(title,body,this.view);const status=el('p',this.feedback.textContent??'','module-feedback module-dialog-feedback');status.setAttribute('role','status');modal.append(status);return modal;}
  protected navigation(label:string,view:string):HTMLButtonElement {const button=el('button',label);button.type='button';button.onclick=()=>this.navigate(view);return button;}
  protected settings(category:string):void {this.navigate('settings');document.dispatchEvent(new CustomEvent('settings:open',{detail:category}));}
  protected begin():boolean {this.tabs.querySelectorAll<HTMLButtonElement>('button').forEach(button=>{const active=button.dataset.tab===this.tab;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;});this.content.setAttribute('aria-labelledby',`${this.view}-tab-${this.tab}`);this.content.replaceChildren();if(!this.state){const error=this.feedback.dataset.error==='true';this.content.append(empty(!this.online?'Нет соединения':error?'Не удалось загрузить данные':'Загружаю…',error?this.feedback.textContent??'Попробуй снова.':'Раздел покажет сохранённые данные после подключения к Чушпану.'));if(this.online&&error)this.content.append(this.button('Повторить загрузку',()=>this.refresh()));return false;}return true;}
  protected abstract refresh():void;
  refreshIfIdle():void {if(this.online&&!this.busy)this.refresh();}
  protected abstract render():void;
}

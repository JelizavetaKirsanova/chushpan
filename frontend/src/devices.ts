import type {HudEvent} from './types';
import {icon} from './dashboard';
import {DeviceWriter} from './devices_state';
import {ColorEditor,colorDraft} from './color_editor';
import type {ColorDraft,UserColor} from './color_editor';

type Values=Record<string,unknown>;
interface BleDiagnostic {report_id?:string;at?:string;connection?:string;compatibility?:string;errors?:{stage:string;type:string;uuid?:string}[];services?:{uuid:string;characteristics:{uuid:string;properties:string[];write_modes:string[];read?:{byte_count?:number;error?:string}}[]}[];notifications?:{uuid:string;byte_count:number}[];}
interface BleStatus {driver:string;driver_id?:string;compatibility:string;confirmed_functions?:string[];diagnostic?:BleDiagnostic;last_command?:{status:string;physical_confirmed?:boolean}|null;pending_probe?:{id:string;requested:{action:string;rgb?:number[]}}|null;}
export interface DeviceRecord {id:string;revision:number;name:string;kind:'led_strip'|'mac'|'external_display'|'airplay'|'audio_output'|'wled';protocol:string;endpoint:string;place_id:string;room:string;state:Values;sent:Values;last_contact:string|null;state_at:string|null;result_status:string|null;favorites:string[];favorite?:boolean;aliases?:string[];auto_connect?:boolean;features:Record<string,boolean|string|string[]>;connection:string;usable:boolean;reason:string;busy:boolean;ble?:BleStatus;preview_color?:string|null;}
interface Collection {id:string;revision:number;name:string;place_id:string;device_ids?:string[];}
interface Output {uid:string;name:string;active:boolean;transport:string;}
interface Candidate {token:string;name:string;kind?:string;protocol?:string;rssi?:number|null;connection?:string;compatibility?:string;compatibility_reason?:string;}
interface Discovery {scan_id:string;started_at:string;finished_at:string;show_all_ble:boolean;scanner_count?:number;candidate_count:number;ble_candidate_count?:number;excluded_count?:number;runtime?:{version:string;python:string;bleak:string;scanner_backend:string;executable:string;bluetooth_authorization:string;build?:{source_commit?:string;built_at?:string;contains_ba705ed?:boolean}};}
export interface DevicesEvent {user_colors?:UserColor[];groups?:Collection[];rooms?:Collection[];profiles?:{id:string;revision:number;name:string;cities:string[]}[];outputs?:Output[];history?:{id:string;items:{at:string;action:string;result_status:string;elapsed_ms:number}[]};metrics?:{total_ms:number;action:string};type:'devices';items:DeviceRecord[];places:{id:string;name:string}[];location:{place_id:string|null;name:string|null;status:string;mode:string};kinds:{id:string;name:string;protocol:string}[];operation_id?:string;running?:boolean;saved?:string;message?:string;error?:string;candidates?:Candidate[];discovery?:Discovery;checked?:{message:string;compatibility?:string;connection?:string};result?:{message?:string;error?:string;result_status?:string;probe_id?:string;device_id?:string;status?:string;saved_color_id?:string}}
const el=<T extends keyof HTMLElementTagNameMap>(tag:T,text='',cls=''):HTMLElementTagNameMap[T]=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
const protocolNames:Record<string,string>={ble:'Bluetooth (BLE)',local:'Этот Mac',betterdisplay:'BetterDisplay',bonjour:'AirPlay · Bonjour',coreaudio:'CoreAudio',http:'Wi-Fi · WLED'};
const deviceIcon=(d:DeviceRecord)=>d.kind==='led_strip'||d.kind==='wled'?'led':d.kind==='airplay'?'airplay':d.kind==='audio_output'?'volume':d.kind==='mac'?'cpu':'monitor';
const outcomes:Record<string,string>={success:'Подтверждено',partial_success:'Частично выполнено',failed:'Не выполнено',unverified:'Физический результат не подтверждён',requires_confirmation:'Нужно подтверждение'};
const colors:[string,string][]=[['Белый','#FFFFFF'],['Тёплый белый · RGB','#FF3020'],['Розовый','#FF0020'],['Светло-розовый','#FF1020'],['Красный','#FF0000'],['Синий','#0000FF'],['Зелёный','#00FF00'],['Фиолетовый','#FF00C8']];
function swatchAllowed(d:DeviceRecord,color:string):boolean {const values=d.features.colors as string[]??[];return d.features.color===true||values.includes('color:'+color.match(/\w{2}/g)!.map(v=>parseInt(v,16)).join(','));}

export class DevicesView {
  private root=document.getElementById('devices-root')!;
  private data:DevicesEvent|null=null;
  private online=false;
  private selected='';
  private search='';
  private place='all';
  private list=el('div','','devices-groups');
  private panel=el('aside','','device-panel');
  private feedback=el('p','Подключение к Чушпану…','devices-feedback');
  private writer:DeviceWriter;
  private wizard:HTMLDialogElement|null=null;
  private wizardStep=0;
  private draft={kind:'led_strip',token:'',endpoint:'',name:'',place_id:'general',room:'',auto_connect:false};
  private candidates:Candidate[]=[];
  private showAllBle=false;
  private discovery:Discovery|null=null;
  private checked='';
  private wizardBusy=false;
  private pendingProbes=new Map<string,string>();
  private colorDrafts=new Map<string,ColorDraft>();
  private colorEditor:ColorEditor|null=null;
  private previewTimers=new Map<string,ReturnType<typeof setTimeout>>();
  private previewRequests=new Set<string>();
  private actionTargets=new Map<string,string>();
  private actionFeedback=new Map<string,{text:string;error:boolean}>();
  private confirmation='';
  private tab:'control'|'settings'|'history'='control';
  private auxiliary:HTMLDialogElement|null=null;
  private auxiliaryKind='';
  private filterTabs=el('div','','device-filter-tabs');
  private sheet=el('dialog','','device-sheet');
  private history:DevicesEvent['history'];
  private scanRequest='';
  private checkRequest='';
  private showGroups=false;
  private autoChecked=new Set<string>();
  private autoRequest='';
  private narrow=window.matchMedia('(max-width: 1190px)');
  private previousFocus:HTMLElement|null=null;
  constructor(private send:(value:object)=>void,private chat?:(text:string)=>void,private wake?:()=>void,private navigate?:(view:string)=>void) {
    this.writer=new DeviceWriter(value=>{if(this.online){const v=value as {id:string;request_id:string};this.actionTargets.set(v.request_id,v.id);this.send(value);}},()=>{this.renderList();this.updateColorEditor();});
    const top=el('div','','devices-topbar');
    const command=el('form','','device-command-bar');const search=el('input');search.type='search';search.placeholder='Найди устройство или напиши команду…';search.setAttribute('aria-label','Поиск устройств');search.oninput=()=>{this.search=search.value;this.renderList();};
    const searchIcon=el('span');searchIcon.innerHTML=icon('search');const mic=this.button('',()=>this.wake?.(),'device-mic');mic.innerHTML=icon('mic');mic.setAttribute('aria-label','Голосовая команда');
    command.append(searchIcon,search,mic);command.onsubmit=e=>{e.preventDefault();if(this.online&&search.value.trim()&&this.chat){this.chat(search.value.trim());}};
    const audio=this.button('',()=>this.openAudio(),'device-top-output');audio.id='devices-audio-output';
    const current=this.select([['','Место не определено']],'');current.id='devices-current-place';current.setAttribute('aria-label','Текущее местоположение');current.onchange=()=>{if(current.value)this.request({op:'location',place_id:current.value});};
    const location=el('div','','device-top-place');const pin=el('span');pin.innerHTML=icon('pin');location.append(pin,this.label('Местоположение',current),this.button('⌄',()=>this.openLocations(),'device-location-settings'));
    top.append(command,audio,location);
    const heading=el('div','','devices-heading');const title=el('div');title.append(el('h1','Устройства'),el('p','Управляй техникой дома, в общаге и где угодно ещё.'));
    const actions=el('div','','device-heading-actions');const find=this.button('',()=>this.openSearch(),'device-icon-button');find.innerHTML=icon('search');find.setAttribute('aria-label','Открыть поиск устройств');const scan=this.button('Найти устройства',()=>{this.openWizard();this.wizardStep=1;this.startScan();});const add=this.button('Добавить устройство',()=>this.openWizard(),'primary device-add');actions.append(find,scan,add);heading.append(title,actions);
    const toolbar=el('div','','devices-toolbar');const filter=this.select([['all','Все']],'all');filter.id='devices-place-filter';filter.setAttribute('aria-label','Местоположение устройств');filter.className='device-filter-select';filter.onchange=()=>{this.place=filter.value;this.renderList();};
    const groups=this.button('Группы',()=>{this.showGroups=!this.showGroups;this.renderList();},'device-groups-toggle');groups.id='device-groups-toggle';toolbar.append(this.filterTabs,filter,groups);
    const layout=el('div','','devices-layout');const center=el('main','','devices-center');center.append(heading,toolbar,this.feedback,this.list);layout.append(center,this.panel);this.panel.hidden=true;
    this.feedback.setAttribute('role','status');this.feedback.setAttribute('aria-live','polite');this.root.append(top,layout);this.sheet.setAttribute('aria-label','Управление устройством');
    this.sheet.oncancel=()=>{this.selected='';this.renderPanel();this.renderList();};this.sheet.onclick=e=>{if(e.target===this.sheet){this.selected='';this.renderPanel();this.renderList();}};
    this.narrow.addEventListener('change',()=>this.placePanel());
    document.addEventListener('workspace:navigate',event=>{if((event as CustomEvent).detail==='devices'&&this.online)this.request({op:'refresh'});});
    this.connection(false);
  }
  private button(label:string,action:()=>void,cls=''):HTMLButtonElement {const b=el('button',label,cls);b.type='button';b.disabled=!this.online;b.onclick=action;return b;}
  private label(title:string,input:HTMLElement):HTMLLabelElement {const n=el('label','','device-field');n.append(el('span',title),input);return n;}
  private select(items:[string,string][],value:string):HTMLSelectElement {const s=el('select');for(const [id,name]of items){const o=el('option',name);o.value=id;s.append(o);}s.value=value;return s;}
  private request(value:object):string {const request_id=crypto.randomUUID();const v=value as {op:string;id?:string};if(v.op==='color_preview')this.previewRequests.add(request_id);if(v.id&&['probe_led','confirm_probe'].includes(v.op))this.actionTargets.set(request_id,v.id);if(this.online)this.send({...value,request_id});return request_id;}
  connection(online:boolean):void {this.online=online;if(!online){for(const timer of this.previewTimers.values())clearTimeout(timer);this.previewTimers.clear();this.previewRequests.clear();this.actionTargets.clear();this.writer.disconnect();this.autoChecked.clear();this.autoRequest='';this.scanRequest='';this.checkRequest='';this.wizardBusy=false;this.feedback.textContent='Нет соединения. Изменения не отправляются; последнее состояние может устареть.';}this.root.querySelectorAll<HTMLInputElement|HTMLSelectElement|HTMLButtonElement>('input,select,button').forEach(n=>n.disabled=!online);if(online)this.request({op:'refresh'});this.renderList();this.renderPanel();this.renderWizard();}
  receive(event:HudEvent):void {
    if(event.type!=='devices')return;
    this.data=event;
    const previewAck=!!event.operation_id&&this.previewRequests.delete(event.operation_id);
    const target=event.operation_id?this.actionTargets.get(event.operation_id):undefined;
    if(target&&!event.running&&(event.result||event.error)){
      const error=!!event.error||!!event.result?.error||event.result?.result_status==='failed';
      this.actionFeedback.set(target,{text:event.error??event.result?.error??event.result?.message??event.message??outcomes[event.result?.result_status??'']??'',error});
      this.actionTargets.delete(event.operation_id!);
    }
    for(const d of event.items){if(d.ble?.pending_probe)this.pendingProbes.set(d.id,d.ble.pending_probe.id);else if(d.ble)this.pendingProbes.delete(d.id);}
    if(event.result?.saved_color_id){const saved=event.user_colors?.find(c=>c.id===event.result?.saved_color_id);if(saved){for(const draft of this.colorDrafts.values()){if(draft.editingId===saved.id)draft.editingRevision=saved.revision;}}}
    if(event.operation_id===this.autoRequest&&!event.running&&(event.result||event.error))this.autoRequest='';
    if(event.operation_id&&!event.running&&(event.result||event.error))this.writer.complete(event.operation_id,!event.error&&event.result?.result_status!=='failed');
    if((event.error||event.message||event.result)&&(!previewAck||event.error)){this.feedback.textContent=event.error??event.result?.error??event.message??event.result?.message??outcomes[event.result?.result_status??'']??'';this.feedback.dataset.error=String(!!event.error||!!event.result?.error||event.result?.result_status==='failed');}
    else if(!this.feedback.dataset.error)this.feedback.textContent='Выбери устройство для управления. Подключение проверяется по твоему действию.';
    if(event.result?.probe_id&&event.result.device_id)this.pendingProbes.set(event.result.device_id,event.result.probe_id);
    if(event.result?.status==='confirmation_required')this.confirmation=this.selected;
    if(event.history){this.history=event.history;}
    if(event.candidates){
      const active=!!this.wizard&&this.wizardStep===1&&(event.operation_id===this.scanRequest||!event.operation_id&&!!this.scanRequest);
      if(event.discovery)this.send({op:'scan_trace',scan_id:event.discovery.scan_id,stage:active?'frontend_received':'frontend_ignored',count:event.candidates.length});
      if(active){this.candidates=event.candidates;this.discovery=event.discovery??null;this.scanRequest='';this.wizardBusy=false;this.renderWizard();if(event.discovery)this.send({op:'scan_trace',scan_id:event.discovery.scan_id,stage:'frontend_rendered',count:this.wizard?.querySelectorAll('.device-discovery-row').length??0});}
    }
    if(event.checked&&(event.operation_id===this.checkRequest||!event.operation_id&&!!this.checkRequest)){this.checked=event.checked.message;this.checkRequest='';this.draft.auto_connect=true;this.wizardBusy=false;this.renderWizard();}
    if(event.saved){this.selected=event.saved;this.closeWizard();}
    if(event.error&&this.wizard&&(!event.operation_id||[this.scanRequest,this.checkRequest].includes(event.operation_id))){if(event.operation_id===this.scanRequest)this.scanRequest='';if(event.operation_id===this.checkRequest)this.checkRequest='';this.wizardBusy=false;this.renderWizard();}
    const filter=document.getElementById('devices-place-filter') as HTMLSelectElement;
    if(!filter.matches(':focus')){const old=this.place;filter.replaceChildren(...[['all','Все'],...event.places.map(p=>[p.id,p.name])].map(([id,name])=>{const o=el('option',name);o.value=id;return o;}));filter.value=old;}
    const current=document.getElementById('devices-current-place') as HTMLSelectElement;
    if(!current.matches(':focus')){current.replaceChildren(...[{id:'',name:'Место не определено'},...event.places].map(p=>{const o=el('option',p.name);o.value=p.id;return o;}));current.value=event.location.place_id??'';current.title=event.location.status==='ready'?'Текущее место известно':'Место неизвестно. Для локальной техники выбери его вручную.';}
    this.renderTopOutput();if(this.auxiliaryKind==='audio')this.renderAudio();
    this.renderList();const active=document.activeElement;const editing=this.panel.contains(active)&&['INPUT','SELECT','TEXTAREA'].includes(active?.tagName??'');
    if(!editing)this.renderPanel();else {const current=this.data.items.find(d=>d.id===this.selected);if(current){const status=this.panel.querySelector('.device-connection');const state=this.panel.querySelector('.device-last-state');if(status)status.textContent=this.statusText(current);if(state)state.textContent=this.stateText(current);}}
    this.updateColorEditor();
    this.autoReconnect();
  }
  private autoReconnect():void {
    if(!this.online||this.autoRequest||this.data?.items.some(d=>d.busy)||this.wizard)return;
    const device=this.data?.items.find(d=>d.auto_connect&&d.usable&&!this.autoChecked.has(d.id));
    if(device){this.autoChecked.add(device.id);this.autoRequest=this.request({op:'inspect',id:device.id});}
  }
  private stateText(d:DeviceRecord):string {
    const known=d.state;const sent=d.sent;const items=[];
    if(typeof known.brightness==='number')items.push(`Яркость ${known.brightness}%`);
    if(typeof known.volume_percent==='number')items.push(known.muted===true?'Без звука':`Громкость ${known.volume_percent}%`);
    if(typeof known.power==='boolean')items.push(known.power?'Включено':'Выключено');
    if(d.kind==='led_strip'&&Object.keys(sent).length){const values=[];if(typeof sent.power==='boolean')values.push(sent.power?'включение':'выключение');if(sent.color)values.push(colors.find(([,color])=>color===sent.color)?.[0]??`цвет ${sent.color}`);if(typeof sent.brightness==='number')values.push(`яркость ${sent.brightness}%`);return `Отправлено: ${values.join(' · ')}. Факт не подтверждён.`;}
    return items.join(' · ')||'Фактическое состояние ещё не прочитано';
  }
  private statusText(d:DeviceRecord):string {return !this.online?'Нет связи с Чушпаном':!d.usable?'В другом месте':d.busy||this.writer.busy(d.id)?'Выполняется':d.connection==='observed'?'Последняя связь успешна':d.connection==='disconnected'?'BLE-соединение закрыто':d.connection==='error'?'Нужна проверка подключения':'Подключение не проверено';}
  private renderTopOutput():void {
    const button=document.getElementById('devices-audio-output')!;button.replaceChildren();const symbol=el('span');symbol.innerHTML=icon('volume');const words=el('span');words.append(el('small','Вывод звука'),el('strong',this.data?.outputs?.find(o=>o.active)?.name??'Выбрать аудиовыход'));button.append(symbol,words,el('span','⌄'));
  }
  private filterRecords():DeviceRecord[] {return (this.data?.items??[]).filter(d=>(this.place==='all'||d.place_id===this.place)&&`${d.name} ${d.room} ${(d.aliases??[]).join(' ')}`.toLocaleLowerCase().includes(this.search.toLocaleLowerCase()));}
  private renderList():void {
    this.renderTopOutput();this.filterTabs.replaceChildren();
    for(const [id,name]of [['all','Все'],...(this.data?.places.filter(p=>p.id==='dorm')??[]).map(p=>[p.id,p.name]),...(this.data?.places.filter(p=>p.id==='home')??[]).map(p=>[p.id,p.name]),['general','Общие'],...(this.data?.places.filter(p=>!['dorm','home','general'].includes(p.id))??[]).map(p=>[p.id,p.name])]){
      const count=(this.data?.items??[]).filter(d=>id==='all'||d.place_id===id).length;const b=this.button('',()=>{this.place=id;(document.getElementById('devices-place-filter') as HTMLSelectElement).value=id;this.renderList();},'device-filter');b.append(el('span',name),el('small',String(count)));b.setAttribute('aria-pressed',String(id===this.place));this.filterTabs.append(b);
    }
    this.list.replaceChildren();const records=this.filterRecords();
    const output=this.button('',()=>this.openAudio(),'device-output-banner');const speaker=el('span','','device-symbol');speaker.innerHTML=icon('volume');const caption=el('span');caption.append(el('strong','Вывод звука'),el('small','Куда воспроизводить аудио'));output.append(speaker,caption,el('span',this.data?.outputs?.find(o=>o.active)?.name??'Выбрать в macOS','device-output-name'),el('span','›'));this.list.append(output);
    if(this.showGroups){this.renderGroups();return;}
    const favorites=records.filter(d=>d.favorite);const favoriteSection=el('section','','device-favorite-section');favoriteSection.append(el('h2','Избранное'));const favoriteCards=el('div','','device-favorite-cards');
    for(const d of favorites){const b=this.button('',()=>this.choose(d.id),'device-favorite-tile');b.dataset.kind=d.kind;b.dataset.selected=String(d.id===this.selected);const symbol=el('span');symbol.innerHTML=icon(deviceIcon(d));b.append(symbol,el('strong',d.name),el('small',[this.placeName(d.place_id),d.room].filter(Boolean).join(' · ')));favoriteCards.append(b);}
    favoriteSection.append(favoriteCards);if(!favorites.length)favoriteSection.append(el('p','Отметь звёздочкой устройства, которые нужны чаще.','device-hint'));this.list.append(favoriteSection,el('h2','Устройства по комнатам','devices-room-heading'));
    const rooms=new Map<string,DeviceRecord[]>();for(const d of records){const key=d.place_id+'|'+d.room;rooms.set(key,[...(rooms.get(key)??[]),d]);}
    for(const group of rooms.values()){
      const first=group[0];const section=el('details','','device-group');section.open=true;const heading=el('summary');const roomIcon=el('span');roomIcon.innerHTML=icon('room');heading.append(roomIcon,el('strong',first.room||'Без комнаты'),el('span','· '+this.placeName(first.place_id)),el('small',String(group.length)));section.append(heading);const cards=el('div','','device-cards');
      for(const d of group){const card=el('article','','device-tile');card.dataset.device=d.id;card.dataset.selected=String(d.id===this.selected);
        const choose=this.button('',()=>this.choose(d.id),'device-tile-open');choose.setAttribute('aria-label','Открыть '+d.name);const symbol=el('span','','device-symbol');symbol.innerHTML=icon(deviceIcon(d));const words=el('span');words.append(el('strong',d.name),el('small',protocolNames[d.protocol]??d.protocol));choose.append(symbol,words);
        const state=el('span',this.statusText(d),'device-connection');state.dataset.status=d.usable?d.connection:'unavailable';card.append(choose,state);
        const star=this.button(d.favorite?'★':'☆',()=>this.request({op:'update',id:d.id,revision:d.revision,favorite:!d.favorite}),'device-star');star.setAttribute('aria-label',(d.favorite?'Убрать из избранного: ':'В избранное: ')+d.name);star.setAttribute('aria-pressed',String(!!d.favorite));card.append(star);
        const known=d.state.power; if(typeof known==='boolean'&&(d.features.power_on||d.features.power_off)){card.append(this.powerSwitch(d));}else if(d.features.power_on||d.features.power_off){const quick=el('div','','device-quick');for(const [key,power,label]of [['power_on',true,'Вкл'],['power_off',false,'Выкл']] as const){const b=this.button(label,()=>this.act(d,'led',{power},true));b.disabled=!this.canAct(d)||d.features[key]!==true;quick.append(b);}card.append(quick);}
        const more=this.button('›',()=>this.choose(d.id),'device-row-more');more.setAttribute('aria-label','Панель '+d.name);card.append(more);card.title=this.stateText(d);cards.append(card);
      }section.append(cards);this.list.append(section);
    }
    if(!records.length)this.list.append(el('div',this.data?.items.length?'Ничего не найдено. Измени фильтр или название.':'Здесь появится твоя техника. Добавь устройство — поиск начнётся только по твоему действию.','devices-empty'));
  }
  private placeName(id:string):string {return id==='general'?'Общее':this.data?.places.find(p=>p.id===id)?.name??'Место не задано';}
  private choose(id:string):void {this.previousFocus=document.activeElement as HTMLElement;this.selected=id;this.tab='control';this.renderList();this.renderPanel();}
  private placePanel():void {
    if(this.narrow.matches&&this.selected){this.root.append(this.sheet);this.sheet.append(this.panel);if(!this.sheet.open)this.sheet.showModal();this.panel.setAttribute('role','region');}
    else {if(this.sheet.open)this.sheet.close();this.root.querySelector('.devices-layout')!.append(this.panel);this.sheet.remove();if(!this.selected)this.previousFocus?.focus();}
  }
  private powerSwitch(d:DeviceRecord):HTMLButtonElement {
    const known=d.state.power;const b=this.button('',()=>this.act(d,'led',{power:!known},true),'device-power-switch');b.setAttribute('role','switch');b.setAttribute('aria-label','Питание '+d.name);b.setAttribute('aria-checked',String(known===true));b.disabled=!this.canAct(d)||d.features[known===true?'power_off':'power_on']!==true;return b;
  }
  private canAct(d:DeviceRecord):boolean {return this.online&&d.usable&&!d.busy&&!this.writer.busy(d.id);}
  private act(d:DeviceRecord,action:string,parameters:Values,immediate=false):void {if(!this.online||!d.usable)return;this.writer.offer(d.id,action,parameters,immediate);}
  private slider(d:DeviceRecord,label:string,field:string,action:string,known:unknown,sent:unknown=undefined):HTMLElement {
    const box=el('div','','device-slider');const input=el('input');input.type='range';input.min='0';input.max='100';input.step='1';input.setAttribute('aria-label',label);input.value=String(typeof known==='number'?known:typeof sent==='number'?sent:50);
    const output=el('output',typeof known==='number'?`${known}% · прочитано`:typeof sent==='number'?`${sent}% · отправлено`:'Выбери уровень');input.disabled=!this.online||!d.usable;
    input.oninput=()=>{output.textContent=input.value+'% · запрошено';this.act(d,action,{[field]:Number(input.value)});};
    input.onchange=()=>this.act(d,action,{[field]:Number(input.value)},true);
    box.append(this.label(label,input),output);return box;
  }
  private renderPanel():void {
    const d=this.data?.items.find(d=>d.id===this.selected);if(!d)this.selected='';this.panel.replaceChildren();this.colorEditor=null;this.panel.hidden=!d;this.placePanel();if(!d)return;
    const heading=el('div','','device-panel-heading');heading.append(el('h2',d.name),this.button('×',()=>{this.selected='';this.renderList();this.renderPanel();},'device-close'));const hero=el('div','','device-hero');hero.dataset.kind=d.kind;const symbol=el('span','','device-hero-symbol');symbol.innerHTML=icon(deviceIcon(d));hero.append(symbol,heading,el('p',[this.placeName(d.place_id),d.room].filter(Boolean).join(' · '),'device-hero-location'));this.panel.append(hero,el('p',this.statusText(d)+' · '+(protocolNames[d.protocol]??d.protocol),'device-connection'),el('p',this.stateText(d),'device-last-state'));
    const tabs=el('div','','device-panel-tabs');for(const [key,label]of [['control','Управление'],['settings','Настройки'],['history','История']] as const){const b=this.button(label,()=>{this.tab=key;if(key==='history')this.request({op:'history',id:d.id});this.renderPanel();});b.setAttribute('aria-pressed',String(this.tab===key));tabs.append(b);}this.panel.append(tabs);
    const feedback=this.actionFeedback.get(d.id);if(feedback){const status=el('p',feedback.text,'device-action-feedback'+(feedback.error?' device-warning':''));status.setAttribute('role',feedback.error?'alert':'status');this.panel.append(status);}
    if(this.tab==='settings'){this.metadata(d);const details=this.panel.querySelector<HTMLDetailsElement>('.device-advanced');if(details)details.open=true;return;}
    if(this.tab==='history'){const log=el('div','','device-history');if(this.history?.id===d.id&&this.history.items.length){for(const item of this.history.items){const row=el('article');row.append(el('strong',({led:'Свет',brightness:'Яркость',volume:'Громкость',inspect:'Проверка',audio_output:'Аудиовыход'} as Record<string,string>)[item.action]??item.action),el('p',outcomes[item.result_status]??'Не подтверждено'),el('small',new Date(item.at+'Z').toLocaleString('ru-RU')+' · '+item.elapsed_ms+' мс'));log.append(row);}}else log.append(el('p','История команд пока пуста.','device-hint'));this.panel.append(log);return;}
    if(!d.usable)this.panel.append(el('p',d.reason,'device-warning'));
    const inspect=this.button(d.kind==='led_strip'?'Проверить / переподключить Bluetooth':'Обновить состояние',()=>this.request({op:'inspect',id:d.id}));inspect.disabled=!this.canAct(d);this.panel.append(inspect);
    if(d.last_contact)this.panel.append(el('p','Последняя успешная связь: '+new Date(d.last_contact).toLocaleString('ru-RU'),'device-hint'));
    if(d.kind==='led_strip'||d.kind==='wled')this.ledPanel(d);
    if(d.kind==='airplay'||d.kind==='audio_output')this.airplayPanel(d);
    if(d.kind==='mac'){
      if(d.features.volume)this.panel.append(this.slider(d,'Громкость Mac','level','volume',d.state.volume_percent));
      if(d.features.mute){const b=this.button(d.state.muted===true?'Включить звук':'Отключить звук',()=>this.act(d,'volume',{mute:d.state.muted!==true},true));b.disabled=!this.canAct(d);this.panel.append(b);}
      if(d.features.playback){const row=el('div','','device-playback');for(const [action,label]of [['previous','Предыдущий'],['pause','Пауза'],['resume','Продолжить'],['next','Следующий']]){const b=this.button(label,()=>this.act(d,'playback',{action},true));b.disabled=!this.canAct(d);row.append(b);}this.panel.append(row);}
      const battery=d.state.battery as {percent?:number;charging?:boolean;on_power_adapter?:boolean}|undefined;
      this.panel.append(el('p',battery?.percent!=null?`Батарея ${battery.percent}% · ${battery.charging?'заряжается':battery.on_power_adapter?'от сети':'от батареи'}`:'Батарея и питание: нет свежих данных','device-hint'));
      const playback=d.state.playback as {state?:string;track?:string;artist?:string}|undefined;if(playback)this.panel.append(el('p',[playback.state==='playing'?'Плеер сообщил о воспроизведении':playback.state==='paused'?'Пауза':'Воспроизведение не подтверждено',playback.artist,playback.track].filter(Boolean).join(' · '),'device-hint'));
      if(d.features.lock){const lock=this.button('Заблокировать экран…',()=>this.act(d,'lock',{},true));lock.disabled=!this.canAct(d);this.panel.append(lock);}
      if(this.confirmation===d.id){const code=el('input');code.inputMode='numeric';code.maxLength=4;code.placeholder='Код из сообщения';this.panel.append(this.label('Подтверждение блокировки',code),this.button('Подтвердить код',()=>this.request({op:'confirm',id:d.id,code:code.value})));}
    }
    if(d.kind!=='led_strip'&&d.features.brightness)this.panel.append(this.slider(d,'Яркость экрана','level','brightness',d.state.brightness));
    if(d.kind==='external_display')this.panel.append(el('p','Яркость — через BetterDisplay. Громкость, входы и питание текущей интеграцией не поддерживаются.','device-hint'));
    this.metadata(d);
    if(typeof this.data?.metrics?.total_ms==='number'&&Number.isFinite(this.data.metrics.total_ms))this.panel.append(el('p','Последняя операция · '+this.data.metrics.total_ms+' мс','device-hint'));
  }
  private ledPanel(d:DeviceRecord):void {
    if(d.kind==='led_strip')this.blePanel(d);
    if(typeof d.state.power==='boolean'){const row=el('div','','device-power-row');row.append(el('strong','Питание'),this.powerSwitch(d));this.panel.append(row);}
    const power=el('div','','device-quick');for(const [feature,value,label]of [['power_on',true,'Включить'],['power_off',false,'Выключить']] as const){const b=this.button(label,()=>this.act(d,'led',{power:value},true));b.disabled=!this.canAct(d)||d.features[feature]!==true;power.append(b);}this.panel.append(power);
    if(d.features.brightness)this.panel.append(this.slider(d,'Яркость ленты','brightness','led',d.state.brightness,d.sent.brightness));
    let draft=this.colorDrafts.get(d.id);if(!draft){draft=colorDraft(d.preview_color??String(d.sent.color??d.state.color??'#FFFFFF'));this.colorDrafts.set(d.id,draft);}
    this.colorEditor=new ColorEditor({device:{...d,busy:d.busy||this.writer.busy(d.id)},online:this.online,draft,colors:this.data?.user_colors??[],presets:colors,
      preview:color=>this.previewColor(d.id,color),apply:color=>{const current=this.data?.items.find(item=>item.id===d.id);if(!current||!this.canAct(current))return;this.flushPreview(d.id,color);this.act(current,'led',{color},true);},
      save:(value,existing)=>this.request({op:'color_save',value,...(existing?{color_id:existing.id,revision:existing.revision}:{})}),
      remove:color=>this.request({op:'color_delete',color_id:color.id,revision:color.revision,confirmed:true}),
      favorite:color=>this.request({op:'favorites',id:d.id,revision:d.revision,colors:[...new Set([...d.favorites,color])]}),
      verify:color=>{const current=this.data?.items.find(item=>item.id===d.id);if(current&&this.canAct(current)&&window.confirm(`Отправить один тест RGB ${color} выбранной ленте «${current.name}»? После отправки подтверди видимый цвет.`))this.request({op:'probe_led',id:d.id,parameters:{color},consent:true});},
      confirm:(probe_id,observed)=>this.request({op:'confirm_probe',id:d.id,probe_id,observed})});
    this.panel.append(this.colorEditor.root);if(d.kind==='led_strip')this.panel.append(el('p','Тёплый белый — RGB-имитация. Отдельный белый канал не поддерживается.','device-hint'));
    if(d.sent.color){const b=this.button('Повторить последний выбранный цвет',()=>this.act(d,'led',{color:d.sent.color},true));b.disabled=!this.canAct(d)||!swatchAllowed(d,String(d.sent.color));this.panel.append(b);}
    if(d.favorites.length){const favorites=el('div','','device-favorites');for(const color of d.favorites){const remove=this.button('Убрать '+color,()=>this.request({op:'favorites',id:d.id,revision:d.revision,colors:d.favorites.filter(c=>c!==color)}));remove.setAttribute('aria-label','Удалить любимый цвет '+color);favorites.append(remove);}this.panel.append(favorites);}
    if(d.kind!=='led_strip')return;
    const test=el('details','','device-advanced device-control-test');const toggle=this.button('Проверить управление',()=>{test.open=!test.open;});test.append(el('summary','Проверка функций контроллера'),el('p','Непроверенные функции заблокированы. Имя и GATT UUID не доказывают совместимость протокола. Каждый тест отправляет ровно одну выбранную команду; затем подтверди наблюдение. После теста одного цвета подтверди, что лента показала ожидаемый цвет: это разрешит RGB этого драйвера. Питание и яркость проверяются отдельно.'));
    for(const [label,parameters]of [['Включение',{power:true}],['Выключение',{power:false}],['Яркость 50%',{brightness:50}],...colors.slice(4,7).map(([name,color])=>[name,{color}])] as [string,Values][]){const b=this.button('Тест: '+label,()=>{if(window.confirm(`Отправить тестовую команду «${label}» этой ленте?`))this.request({op:'probe_led',id:d.id,parameters,consent:true});});b.disabled=!this.canAct(d)||!d.features.probe;test.append(b);}
    if(this.pendingProbes.has(d.id)){const probe_id=this.pendingProbes.get(d.id)!;test.open=true;test.append(el('p','Функция сработала так, как указано?'));for(const [value,label]of [[true,'Да, вижу результат'],[false,'Нет, не сработало']] as const)test.append(this.button(label,()=>{this.pendingProbes.delete(d.id);this.request({op:'confirm_probe',id:d.id,probe_id,observed:value});}));}
    this.panel.append(toggle,test);
  }
  private blePanel(d:DeviceRecord):void {
    const advanced=el('details','','device-advanced device-ble-details');advanced.append(el('summary','Расширенная BLE-диагностика'));
    const ble=d.ble;const compatibility:Record<string,string>={unsupported:'Протокол неизвестен',unverified:'GATT ещё не проверен',gatt_match:'GATT совпал; функции требуют наблюдения',gatt_mismatch:'GATT не соответствует драйверу'};
    advanced.append(el('p','Драйвер: '+(ble?.driver??'Не выбран'),'device-hint'),el('p','Протокол: '+(compatibility[ble?.compatibility??'unverified']??'Не подтверждён'),'device-hint'));
    if(ble?.confirmed_functions?.length)advanced.append(el('p','Проверено наблюдением: '+ble.confirmed_functions.join(', '),'device-hint'));
    const last=ble?.last_command;advanced.append(el('p','Последняя команда: '+(last?.status==='unknown'?'исход записи неизвестен; проверь ленту':d.result_status==='failed'?'не подтверждена':last?.status==='written'?(last.physical_confirmed?'подтверждена наблюдением':'байты отправлены; физическое выполнение не подтверждено'):'команд в журнале пока нет'),'device-hint'));
    const diagnose=this.button('Диагностика',()=>this.request({op:'diagnose_ble',id:d.id}));diagnose.disabled=!this.canAct(d);advanced.append(diagnose);
    const report=ble?.diagnostic;
    if(report?.report_id){const details=el('details','','device-advanced');details.append(el('summary','Последний GATT-отчёт'),el('p','BLE: '+(report.connection==='disconnected'?'соединение закрыто':'закрытие не подтверждено')+'. Управляющие команды не отправлялись. Значения чтений и уведомлений скрыты.'));
      for(const service of report.services??[]){details.append(el('p','Сервис '+service.uuid));for(const char of service.characteristics){details.append(el('code',char.uuid),el('p',char.properties.join(', ')+' · запись: '+(char.write_modes.join(', ')||'нет')+(char.read?' · чтение: '+(char.read.error??`${char.read.byte_count} байт`):'')));}}
      for(const error of report.errors??[])details.append(el('p',`${error.stage}: ${error.type}`,'device-warning'));
      details.append(el('p',`Уведомлений: ${report.notifications?.length??0}. Уведомление не подтверждает выполнение команды.`),this.button('Скачать GATT-отчёт',()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const a=el('a');a.href=url;a.download='ble-diagnostic-'+report.report_id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}));advanced.append(details);}
    const profiles=el('details','','device-advanced');profiles.append(el('summary','BLE-профиль'),el('p','Несовместимая смена профиля сбрасывает подтверждённые функции. Импорт не подтверждает совместимость этой ленты. После выбора выполни диагностику и проверь одну функцию.'));
    const driver=this.select([['auto','Авто · кандидат по модели'],['elk-bledob-legacy','ELK-BLEDOB · существующий'],['elk-bleddm-candidate','ELK-BLEDDM · кандидат']],ble?.driver_id??'auto');driver.disabled=!this.canAct(d);const select=this.button('Выбрать профиль',()=>{if(window.confirm('Сменить профиль этой ленты? Для несовместимого драйвера подтверждения будут сброшены.'))this.request({op:'ble_profile',id:d.id,driver:driver.value,consent:true});});select.disabled=!this.canAct(d);
    const file=el('input');file.type='file';file.accept='.json,application/json';file.setAttribute('aria-label','Импорт BLE-профиля');file.disabled=!this.canAct(d);file.onchange=async()=>{const selected=file.files?.[0];if(!selected)return;try{if(selected.size>32768)throw new Error('Профиль больше 32 КБ');const profile=JSON.parse(await selected.text());if(window.confirm('Импортировать профиль и сбросить подтверждения этой ленты?'))this.request({op:'ble_profile',id:d.id,profile,consent:true});}catch{this.feedback.textContent='Не удалось прочитать JSON-профиль размером до 32 КБ.';this.feedback.dataset.error='true';}};profiles.append(driver,select,this.label('Импорт подтверждённого профиля JSON',file));advanced.append(profiles);
    this.panel.append(advanced);
  }
  private updateColorEditor():void {const d=this.data?.items.find(d=>d.id===this.selected);if(d)this.colorEditor?.updateDevice({...d,busy:d.busy||this.writer.busy(d.id)},this.online,this.data?.user_colors??[]);}
  private previewColor(id:string,color:string):void {const timer=this.previewTimers.get(id);if(timer)clearTimeout(timer);if(this.online)this.previewTimers.set(id,setTimeout(()=>{this.previewTimers.delete(id);this.request({op:'color_preview',id,color});},180));}
  private flushPreview(id:string,color:string):void {const timer=this.previewTimers.get(id);if(timer)clearTimeout(timer);this.previewTimers.delete(id);this.request({op:'color_preview',id,color});}
  private metadata(d:DeviceRecord):void {
    const detail=el('details','','device-advanced');detail.append(el('summary','Настройки устройства'));
    const form=el('form');const name=el('input');name.value=d.name;name.required=true;name.maxLength=80;
    const place=this.select((this.data?.places??[]).map(p=>[p.id,p.name]),d.place_id);const room=el('input');room.value=d.room;room.maxLength=60;room.placeholder='Например, спальня';const roomOptions=el('datalist');roomOptions.id='device-room-options';for(const item of this.data?.rooms??[]){if(item.place_id===d.place_id){const option=el('option');option.value=item.name;roomOptions.append(option);}}room.setAttribute('list',roomOptions.id);form.append(roomOptions);
    const aliases=el('input');aliases.value=(d.aliases??[]).join(', ');aliases.placeholder='лента, подсветка стола';const favorite=el('input');favorite.type='checkbox';favorite.checked=!!d.favorite;const auto=el('input');auto.type='checkbox';auto.checked=!!d.auto_connect;
    form.append(this.label('Название',name),this.label('Местоположение',place),this.label('Комната',room),this.label('Голосовые синонимы через запятую',aliases),this.label('В избранном',favorite),this.label('Автоматически проверять подключение в текущем месте',auto));const save=this.button('Сохранить настройки',()=>{});save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(form.reportValidity())this.request({op:'update',id:d.id,revision:d.revision,name:name.value,place_id:place.value,room:room.value,aliases:aliases.value.split(',').map(v=>v.trim()).filter(Boolean),favorite:favorite.checked,auto_connect:auto.checked});};
    const technical=el('details');technical.append(el('summary','Техническая информация'),el('p',`Протокол: ${d.protocol}`),el('code',d.endpoint),el('p',`Последний результат: ${outcomes[d.result_status??'']??'Не проверен'}`));
    technical.append(el('p','Функции: '+Object.entries(d.features).filter(([,v])=>v===true).map(([k])=>({power_on:'включение',power_off:'выключение',brightness:'яркость',color:'RGB',volume:'громкость',playback:'плеер',audio_output:'аудиовыход',airplay_audio:'AirPlay-аудио',airplay_video:'зеркалирование',probe:'проверка BLE'} as Record<string,string>)[k]??k).join(', ')));detail.append(form,el('p','Автопроверка без команд выполняется один раз после соединения с Чушпаном для доступных устройств с включённой настройкой. BLE подключается заново перед каждой командой и закрывает соединение после неё; до отправки допускается один повтор подключения, после отправки повтора нет.','device-hint'),this.button('Разрешения macOS',()=>this.navigate?.('settings')),technical,this.button('Удалить устройство…',()=>{if(window.confirm(`Удалить «${d.name}» из списка? Это не выключит устройство и не отменит Bluetooth-сопряжение.`)){this.request({op:'delete',id:d.id,revision:d.revision});this.selected='';}},'device-delete'));this.panel.append(detail);
  }
  private openWizard():void {this.scanRequest='';this.checkRequest='';this.showAllBle=false;this.discovery=null;this.feedback.dataset.error='false';this.wizardStep=0;this.draft={kind:'led_strip',token:'',endpoint:'',name:'',place_id:this.data?.location.place_id??'general',room:'',auto_connect:false};this.candidates=[];this.checked='';this.wizardBusy=false;this.wizard=el('dialog','','device-wizard');this.wizard.setAttribute('aria-label','Добавление устройства');this.root.append(this.wizard);this.wizard.oncancel=()=>{this.cancelScan();};this.wizard.onclose=()=>{this.wizard?.remove();this.wizard=null;this.autoReconnect();};this.renderWizard();this.wizard.showModal();}
  private startScan():void {this.cancelScan();this.candidates=[];this.discovery=null;this.feedback.dataset.error='false';this.wizardBusy=true;this.scanRequest=this.request({op:'discover',kind:this.showAllBle?'led_strip':this.draft.kind,show_all_ble:this.showAllBle});this.renderWizard();}
  private selectCandidate(candidate:Candidate,connect=false):void {this.cancelScan();this.draft.token=candidate.token;this.draft.name=candidate.name;this.draft.auto_connect=false;if(candidate.kind)this.draft.kind=candidate.kind;this.wizardStep=2;this.checked='';this.wizardBusy=false;if(connect)this.startCheck();else this.renderWizard();}
  private startCheck():void {this.checked='';this.feedback.dataset.error='false';this.wizardBusy=true;this.checkRequest=this.request({op:'check',...this.draft});this.renderWizard();}
  private closeWizard():void {this.cancelScan();this.wizardBusy=false;this.wizard?.close();this.wizard?.remove();this.wizard=null;}
  private renderWizard():void {
    const dialog=this.wizard;if(!dialog)return;dialog.replaceChildren();const title=el('div','','device-panel-heading');title.append(el('h2','Новое устройство'),this.button('×',()=>this.closeWizard(),'device-close'));const steps=el('div','','device-wizard-steps');for(let i=0;i<4;i++){const step=el('span',String(i+1));step.dataset.active=String(i<=this.wizardStep);steps.append(step);}dialog.append(title,steps,el('p',`Шаг ${this.wizardStep+1} из 4 · ${['Тип устройства','Поиск и выбор','Подключение и возможности','Название и место'][this.wizardStep]}`,'device-hint'));
    if(this.wizardStep===0){for(const k of [{id:'auto',name:'Автоматический поиск',protocol:'auto'},...(this.data?.kinds??[])]){const b=this.button(k.name,()=>{this.draft.kind=k.id;this.draft.name=k.name;this.wizardStep=1;this.candidates=[];this.renderWizard();},'device-kind-choice');dialog.append(b);}dialog.append(el('p','Лампы, розетки, пылесосы и другие протоколы появятся после подключения соответствующего адаптера. Фиктивное управление не добавляется.','device-hint'));}
    if(this.wizardStep===1){const scan=this.button(this.wizardBusy?'Ищу…':this.candidates.length?'Повторить поиск':'Найти устройства',()=>this.startScan(),'primary');scan.disabled=!this.online||this.wizardBusy;scan.setAttribute('aria-busy',String(this.wizardBusy));dialog.append(scan,el('p','Поиск начинается только по нажатию. Bluetooth pairing не выполняется.','device-hint'));
      if(['led_strip','auto'].includes(this.draft.kind)){const all=el('input');all.type='checkbox';all.checked=this.showAllBle;all.disabled=!this.online||this.wizardBusy;all.onchange=()=>{this.showAllBle=all.checked;this.candidates=[];this.discovery=null;this.renderWizard();};dialog.append(this.label('Показать все BLE-устройства без фильтров',all));}
      if(this.wizardBusy){const progress=el('p','Сканирование Bluetooth LE…','device-scan-progress');progress.setAttribute('role','status');dialog.append(progress);}
      if(this.discovery){const report=this.discovery;dialog.append(el('p',`Новый поиск ${report.scan_id.slice(0,8)} · ${new Date(report.finished_at).toLocaleTimeString('ru-RU')} · BLE: ${report.scanner_count??'—'} → передано: ${report.candidate_count} · исключено: ${report.excluded_count??0}`,'device-discovery-summary'));const runtime=report.runtime;if(runtime){const technical=el('details','','device-discovery-runtime');technical.append(el('summary','Сборка и диагностика поиска'),el('p',`Чушпан ${runtime.version} · коммит ${runtime.build?.source_commit?.slice(0,12)??'не записан'} · Bluetooth: ${runtime.bluetooth_authorization}`),el('p',`Python ${runtime.python} · Bleak ${runtime.bleak} · ${runtime.scanner_backend}`),el('code',runtime.executable),el('p','Поиск не использует фильтр локации, категории или состояния подключения. Результаты относятся только к указанному запросу.'));dialog.append(technical);}}
      for(const [unknown,label]of [[false,'Возможные LED-ленты и другие устройства'],[true,'Неизвестные Bluetooth LE устройства']] as const){
        const group=this.candidates.filter(c=>(c.compatibility==='unknown')===unknown);if(!group.length)continue;
        const section=el('section','','device-discovery-group');section.append(el('h3',this.showAllBle?'Все Bluetooth LE устройства · диагностика':label));
        for(const candidate of group){const row=el('article','','device-discovery-row');const name=this.button(candidate.name,()=>this.selectCandidate(candidate),'device-discovery-name');name.disabled=!this.online||this.wizardBusy;
          const info=el('div');const signal=typeof candidate.rssi==='number'?`${candidate.rssi} dBm`:'Сигнал не указан';info.append(name,el('p',[protocolNames[candidate.protocol??'ble']??candidate.protocol,signal,'Обнаружено · подключение не проверено'].filter(Boolean).join(' · '),'device-hint'),el('p',candidate.compatibility_reason??'Совместимость ещё не проверена.','device-hint'));
          const connect=this.button('Подключить',()=>this.selectCandidate(candidate,true));connect.setAttribute('aria-label','Подключить '+candidate.name);connect.disabled=!this.online||this.wizardBusy;row.append(info,connect);section.append(row);
        }dialog.append(section);
      }
      if(!this.wizardBusy&&!this.candidates.length)dialog.append(el('p','Список пока пуст. Запусти поиск или добавь вручную.','device-hint'));
      const manual=el('details');manual.append(el('summary','Добавить вручную'));const endpoint=el('input');endpoint.value=this.draft.endpoint;endpoint.placeholder=this.draft.kind==='wled'?'192.168.1.50':this.draft.kind==='mac'?'local-mac':'Идентификатор устройства';endpoint.setAttribute('aria-label','Идентификатор подключения');manual.append(this.label('Идентификатор подключения',endpoint),this.button('Использовать идентификатор',()=>{this.draft.endpoint=endpoint.value.trim();this.draft.token='';this.wizardStep=2;this.checked='';this.renderWizard();}));if(!['auto','airplay','audio_output'].includes(this.draft.kind))dialog.append(manual);if(this.wizardBusy)dialog.append(this.button('Остановить поиск',()=>{this.cancelScan();this.wizardBusy=false;this.renderWizard();}));
    }
    if(this.wizardStep===2){dialog.append(el('p','Проверка читает доступный профиль и подключение. Цвет, яркость и питание не изменяются. Для LED физическую поддержку функций подтверждают отдельно после добавления.'));
      dialog.append(el('h3',this.draft.name));const check=this.button(this.wizardBusy?'Проверяю…':'Проверить подключение',()=>this.startCheck(),'primary');check.disabled=!this.online||this.wizardBusy;check.setAttribute('aria-busy',String(this.wizardBusy));const next=this.button(this.checked?'Далее':'Добавить с непроверенным подключением',()=>{this.wizardStep=3;this.renderWizard();});next.disabled=!this.online||this.wizardBusy;dialog.append(check,el('p',this.checked||'Подключение ещё не проверено.','device-hint'),next);}
    if(this.wizardStep===3){const form=el('form');const name=el('input');name.value=this.draft.name;name.required=true;name.maxLength=80;const place=this.select((this.data?.places??[]).map(p=>[p.id,p.name]),this.draft.place_id);const room=el('input');room.value=this.draft.room;room.maxLength=60;
      const auto=el('input');auto.type='checkbox';auto.checked=this.draft.auto_connect;form.append(this.label('Название',name),this.label('Местоположение',place),this.label('Комната',room),this.label('Автоматически проверять подключение в текущем месте',auto));const save=this.button('Сохранить устройство',()=>{},'primary');save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(form.reportValidity()){this.draft={...this.draft,name:name.value,place_id:place.value,room:room.value,auto_connect:auto.checked};this.request({op:'add',...this.draft});}};dialog.append(form);}
    if(this.wizardStep>0)dialog.append(this.button('Назад',()=>{this.cancelScan();this.wizardBusy=false;this.wizardStep--;this.renderWizard();}));
    if(this.feedback.dataset.error==='true')dialog.append(el('p',this.feedback.textContent??'','device-warning'));
  }
  private cancelScan():void {
    for(const operation_id of [this.scanRequest,this.checkRequest])if(operation_id)this.request({op:'cancel',operation_id});
    this.scanRequest='';this.checkRequest='';
  }
  private dialog(title:string,kind:string):HTMLDialogElement {
    this.auxiliary?.close();this.auxiliary?.remove();const dialog=el('dialog','','device-wizard device-dialog');this.auxiliary=dialog;this.auxiliaryKind=kind;
    dialog.setAttribute('aria-label',title);this.root.append(dialog);dialog.onclose=()=>{dialog.remove();if(this.auxiliary===dialog){this.auxiliary=null;this.auxiliaryKind='';}};dialog.showModal();return dialog;
  }
  private dialogTitle(dialog:HTMLDialogElement,title:string,subtitle:string):void {
    const row=el('div','','device-panel-heading');row.append(el('h2',title),this.button('×',()=>dialog.close(),'device-close'));dialog.append(row,el('p',subtitle,'device-hint'));
  }
  private openSearch():void {
    const dialog=this.dialog('Поиск устройств','search');this.dialogTitle(dialog,'Найти устройство','Поиск среди добавленной техники. Для новых устройств открой мастер подключения.');
    const input=el('input');input.type='search';input.placeholder='Название, комната или синоним';input.setAttribute('aria-label','Найти зарегистрированное устройство');const results=el('div','','device-search-results');
    const render=()=>{results.replaceChildren();const matches=(this.data?.items??[]).filter(d=>`${d.name} ${d.room} ${(d.aliases??[]).join(' ')}`.toLowerCase().includes(input.value.toLowerCase()));for(const d of matches){const b=this.button('',()=>{dialog.close();this.choose(d.id);},'device-kind-choice');b.append(el('strong',d.name),el('small',[this.placeName(d.place_id),d.room,protocolNames[d.protocol]].filter(Boolean).join(' · ')));results.append(b);}if(!matches.length)results.append(el('p','Ничего не найдено.','device-hint'));};input.oninput=render;dialog.append(input,results,this.button('Добавить устройство',()=>{dialog.close();this.openWizard();},'primary'));render();input.focus();
  }
  private openAudio():void {this.dialog('Вывод звука и AirPlay','audio');this.renderAudio();this.request({op:'audio_refresh'});}
  private renderAudio():void {
    const dialog=this.auxiliary;if(!dialog||this.auxiliaryKind!=='audio')return;dialog.replaceChildren();this.dialogTitle(dialog,'Вывод звука','Активный выход подтверждается macOS. AirPlay-аудио, видеотрансляция и пульт плеера работают независимо.');
    for(const output of this.data?.outputs??[]){const b=this.button('',()=>{this.request({op:'audio_select',uid:output.uid});},'device-kind-choice device-audio-choice');const symbol=el('span');symbol.innerHTML=icon(output.transport==='airplay'?'airplay':'volume');const words=el('span');words.append(el('strong',output.name),el('small',output.active?'Активен · подтверждено macOS':output.transport==='airplay'?'AirPlay-аудио':'Доступен системе'));b.append(symbol,words,el('span',output.active?'✓':'○'));b.setAttribute('aria-pressed',String(output.active));dialog.append(b);}
    if(!this.data?.outputs?.length)dialog.append(el('p','Список выходов ещё не прочитан. Нажми «Обновить выходы» или открой настройки звука.','device-hint'));
    const actions=el('div','','device-dialog-actions');actions.append(this.button('Обновить выходы',()=>this.request({op:'audio_refresh'})),this.button('AirPlay · системный выбор',()=>this.request({op:'system_audio'})),this.button('Зеркалирование экрана…',()=>this.request({op:'system_audio',video:true})));dialog.append(actions,el('p','Чтобы завершить AirPlay, выбери встроенные динамики Mac. Пауза и продолжение доступны в карточке Mac и управляют его медиаплеером.','device-hint'),el('p',this.feedback.textContent??'','device-dialog-feedback'));
  }
  private airplayPanel(d:DeviceRecord):void {
    const box=el('section','','device-airplay-controls');box.append(el('h3',d.kind==='airplay'?'Apple AirPlay':'Системный аудиовыход'));
    if(d.features.audio_output){const select=this.button(d.state.active_output===true?'Обновить активный выход':'Выбрать аудиовыход',()=>this.act(d,'audio_output',{},true),'primary');select.disabled=!this.canAct(d);box.append(select);}
    if(d.features.airplay_audio){const audio=this.button('Выбрать AirPlay-аудио в macOS',()=>this.act(d,'airplay_audio',{},true),'primary');audio.disabled=!this.canAct(d);box.append(audio,el('p','Откроется системный выбор. Выбери приёмник вручную и обнови аудиовыходы; до этого подключение не подтверждено.','device-hint'));}
    if(d.features.airplay_video){const video=this.button('Зеркалирование / видеовывод…',()=>this.act(d,'airplay_video',{},true));video.disabled=!this.canAct(d);box.append(video);}
    box.append(this.button('Вернуть звук на Mac…',()=>this.openAudio()),el('p','Прямой пульт и громкость приёмника не заявлены этим адаптером. Для паузы музыки используй управление плеером Mac.','device-hint'));this.panel.append(box);
  }
  private openLocations():void {
    const dialog=this.dialog('Местоположения и комнаты','locations');this.dialogTitle(dialog,'Местоположения','Ручной выбор имеет приоритет. «Общее» не делает устройство доступным в другой сети.');
    const current=el('p',this.data?.location.status==='ready'?`Сейчас: ${this.data.location.name??'Общее'} · ${this.data.location.mode==='manual'?'выбрано вручную':'определено автоматически'}`:'Текущее место не подтверждено. Выбери его вручную.','device-location-summary');dialog.append(current);
    const mode=this.select([['manual','Ручной выбор'],['auto','Авто · по разрешённым данным']],this.data?.location.mode??'manual');mode.onchange=()=>this.request({op:'location_mode',mode:mode.value,place_id:this.data?.location.place_id??'general'});dialog.append(this.label('Способ определения',mode));
    for(const place of this.data?.places??[]){const row=el('div','','device-place-row');const pick=this.button(place.id==='dorm'?'Общага · Таллинн':place.id==='home'?'Дом · Силламяэ':place.name,()=>{this.request({op:'location',place_id:place.id});dialog.close();},'device-kind-choice');row.append(pick);const profile=this.data?.profiles?.find(p=>p.id===place.id);if(profile)row.append(this.button('Изменить',()=>this.editProfile(profile)));dialog.append(row);}
    dialog.append(this.button('Новое местоположение',()=>this.editProfile()),this.button('Комнаты',()=>this.editRoom()),el('p','Разрешения геолокации и явные привязки Wi-Fi настраиваются в общем разделе «Настройки → Места и контекст». При недоступных данных место остаётся неизвестным.','device-hint'),this.button('Места и разрешения',()=>{dialog.close();this.navigate?.('settings');}));
  }
  private editProfile(profile?:{id:string;revision:number;name:string;cities:string[]}):void {
    const dialog=this.dialog(profile?'Изменить местоположение':'Новое местоположение','profile');this.dialogTitle(dialog,profile?'Изменить местоположение':'Новое местоположение','Этот профиль используется устройствами и существующими сценариями.');
    const form=el('form');const name=el('input');name.required=true;name.maxLength=80;name.value=profile?.name??'';form.append(this.label('Название',name));const save=this.button('Сохранить место',()=>{},'primary');save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(form.reportValidity()){this.request({op:'profile',value:profile?{...profile,name:name.value}:{name:name.value}});dialog.close();}};dialog.append(form);
  }
  private editRoom(room?:Collection):void {
    const dialog=this.dialog('Комнаты','rooms');this.dialogTitle(dialog,'Комнаты','Создай комнату и выбери её в настройках устройства.');
    for(const item of this.data?.rooms??[]){const row=el('div','','device-place-row');row.append(el('span',`${item.name} · ${this.placeName(item.place_id)}`),this.button('Изменить',()=>this.editRoom(item)),this.button('Удалить',()=>{this.request({op:'collection',kind:'room',value:item,delete:true});dialog.close();}));dialog.append(row);}
    const form=el('form');const name=el('input');name.required=true;name.maxLength=60;name.value=room?.name??'';const place=this.select((this.data?.places??[]).map(p=>[p.id,p.name]),room?.place_id??this.data?.location.place_id??'general');form.append(this.label('Название комнаты',name),this.label('Местоположение',place));const save=this.button(room?'Сохранить комнату':'Создать комнату',()=>{},'primary');save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(form.reportValidity()){this.request({op:'collection',kind:'room',value:{...(room??{}),name:name.value,place_id:place.value}});dialog.close();}};dialog.append(form);
  }
  private renderGroups():void {
    const heading=el('div','','device-section-heading');heading.append(el('h2','Группы устройств'),this.button('Создать группу',()=>this.editGroup()));this.list.append(heading);
    const groups=(this.data?.groups??[]).filter(g=>this.place==='all'||g.place_id===this.place);
    for(const group of groups){const box=el('section','','device-group-card');box.append(el('h3',group.name),el('p',`${this.placeName(group.place_id)} · ${group.device_ids?.length??0} устройств`,'device-hint'));
      const members=(this.data?.items??[]).filter(d=>group.device_ids?.includes(d.id));box.append(el('p',members.map(d=>d.name).join(' · ')||'Добавь устройства в настройках группы.','device-hint'));for(const [power,label,key]of [[true,'Включить','power_on'],[false,'Выключить','power_off']] as const){if(members.some(d=>d.features[key]))box.append(this.button(label,()=>this.request({op:'act_group',id:group.id,action:'led',parameters:{power}})));}box.append(this.button('Настроить группу',()=>this.editGroup(group)));this.list.append(box);
    }
    if(!groups.length)this.list.append(el('p','Групп пока нет. Объедини добавленные устройства для общей команды.','devices-empty'));
  }
  private editGroup(group?:Collection):void {
    const dialog=this.dialog('Группа устройств','group');this.dialogTitle(dialog,group?'Настроить группу':'Новая группа','При общей команде неподдерживаемые действия и ошибки показываются отдельно.');
    const form=el('form');const name=el('input');name.required=true;name.maxLength=60;name.value=group?.name??'';const place=this.select((this.data?.places??[]).map(p=>[p.id,p.name]),group?.place_id??'general');const room=el('input');room.maxLength=60;room.value=(group as Collection&{room?:string}|undefined)?.room??'';form.append(this.label('Название',name),this.label('Местоположение',place),this.label('Комната',room));const choices:{id:string;input:HTMLInputElement}[]=[];
    for(const device of this.data?.items??[]){const input=el('input');input.type='checkbox';input.checked=group?.device_ids?.includes(device.id)??false;choices.push({id:device.id,input});form.append(this.label(device.name+' · '+this.placeName(device.place_id),input));}
    const save=this.button('Сохранить группу',()=>{},'primary');save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(form.reportValidity()){this.request({op:'collection',kind:'group',value:{...(group??{}),name:name.value,place_id:place.value,room:room.value,device_ids:choices.filter(c=>c.input.checked).map(c=>c.id)}});dialog.close();}};dialog.append(form);if(group)dialog.append(this.button('Удалить группу',()=>{this.request({op:'collection',kind:'group',value:group,delete:true});dialog.close();},'device-delete'));
  }

}

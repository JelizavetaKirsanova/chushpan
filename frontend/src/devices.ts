import type {HudEvent} from './types';
import {icon} from './dashboard';
import {DeviceWriter} from './devices_state';

type Values=Record<string,unknown>;
export interface DeviceRecord {id:string;revision:number;name:string;kind:'led_strip'|'mac'|'external_display';protocol:string;endpoint:string;place_id:string;room:string;state:Values;sent:Values;last_contact:string|null;state_at:string|null;result_status:string|null;favorites:string[];features:Record<string,boolean|string|string[]>;connection:string;usable:boolean;reason:string;busy:boolean}
export interface DevicesEvent {type:'devices';items:DeviceRecord[];places:{id:string;name:string}[];location:{place_id:string|null;name:string|null;status:string;mode:string};kinds:{id:string;name:string;protocol:string}[];operation_id?:string;running?:boolean;saved?:string;message?:string;error?:string;candidates?:{token:string;name:string}[];checked?:{message:string};result?:{message?:string;error?:string;result_status?:string;probe_id?:string;status?:string}}
const el=<T extends keyof HTMLElementTagNameMap>(tag:T,text='',cls=''):HTMLElementTagNameMap[T]=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
const kindNames:Record<string,string>={led_strip:'Подсветка',mac:'Компьютеры',external_display:'Телевизоры и дисплеи'};
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
  private draft={kind:'led_strip',token:'',endpoint:'',name:'',place_id:'general',room:''};
  private candidates:{token:string;name:string}[]=[];
  private checked='';
  private wizardBusy=false;
  private pendingProbe:{id:string;probe_id:string}|null=null;
  private confirmation='';
  constructor(private send:(value:object)=>void) {
    this.writer=new DeviceWriter(value=>{if(this.online)this.send(value);},()=>this.renderList());
    const heading=el('div','','devices-heading');const title=el('div');title.append(el('small','ТВОЁ ПРОСТРАНСТВО'),el('h1','Устройства'),el('p','Свет, звук и техника — под рукой.'));
    heading.append(title,this.button('Добавить устройство',()=>this.openWizard(),'primary'));
    const toolbar=el('div','','devices-toolbar');const search=el('input');search.type='search';search.placeholder='Найти устройство';search.setAttribute('aria-label','Поиск устройств');search.oninput=()=>{this.search=search.value;this.renderList();};
    const filter=this.select([['all','Все'],['general','Общие'],['dorm','Общага'],['home','Дом']],'all');filter.id='devices-place-filter';filter.setAttribute('aria-label','Местоположение устройств');filter.onchange=()=>{this.place=filter.value;this.renderList();};
    const current=this.select([['general','Место неизвестно · Общие']],'general');current.id='devices-current-place';current.setAttribute('aria-label','Текущее местоположение');current.onchange=()=>this.request({op:'location',place_id:current.value});
    toolbar.append(search,filter,this.label('Я сейчас',current));
    const layout=el('div','','devices-layout');layout.append(this.list,this.panel);this.panel.hidden=true;
    this.feedback.setAttribute('role','status');this.feedback.setAttribute('aria-live','polite');this.root.append(heading,toolbar,this.feedback,layout);
    document.addEventListener('workspace:navigate',event=>{if((event as CustomEvent).detail==='devices'&&this.online)this.request({op:'refresh'});});
    this.connection(false);
  }
  private button(label:string,action:()=>void,cls=''):HTMLButtonElement {const b=el('button',label,cls);b.type='button';b.disabled=!this.online;b.onclick=action;return b;}
  private label(title:string,input:HTMLElement):HTMLLabelElement {const n=el('label','','device-field');n.append(el('span',title),input);return n;}
  private select(items:[string,string][],value:string):HTMLSelectElement {const s=el('select');for(const [id,name]of items){const o=el('option',name);o.value=id;s.append(o);}s.value=value;return s;}
  private request(value:object):void {if(this.online)this.send({...value,request_id:crypto.randomUUID()});}
  connection(online:boolean):void {this.online=online;if(!online){this.writer.disconnect();this.feedback.textContent='Нет соединения. Изменения не отправляются; последнее состояние может устареть.';}this.root.querySelectorAll<HTMLInputElement|HTMLSelectElement|HTMLButtonElement>('input,select,button').forEach(n=>n.disabled=!online);if(online)this.request({op:'refresh'});this.renderList();this.renderPanel();}
  receive(event:HudEvent):void {
    if(event.type!=='devices')return;
    this.data=event;
    if(event.operation_id&&!event.running&&(event.result||event.error))this.writer.complete(event.operation_id,!event.error&&event.result?.result_status!=='failed');
    if(event.error||event.message||event.result){this.feedback.textContent=event.error??event.message??event.result?.error??event.result?.message??outcomes[event.result?.result_status??'']??'';this.feedback.dataset.error=String(!!event.error||!!event.result?.error);}
    else if(!this.feedback.dataset.error)this.feedback.textContent='Выбери устройство для управления. Подключение проверяется по твоему действию.';
    if(event.result?.probe_id&&this.selected)this.pendingProbe={id:this.selected,probe_id:event.result.probe_id};
    if(event.result?.status==='confirmation_required')this.confirmation=this.selected;
    if(event.candidates){this.candidates=event.candidates;this.wizardBusy=false;this.renderWizard();}
    if(event.checked){this.checked=event.checked.message;this.wizardBusy=false;this.renderWizard();}
    if(event.saved){this.selected=event.saved;this.closeWizard();}
    if(event.error&&this.wizard){this.wizardBusy=false;this.renderWizard();}
    const filter=document.getElementById('devices-place-filter') as HTMLSelectElement;
    if(!filter.matches(':focus')){const old=this.place;filter.replaceChildren(...[['all','Все'],...event.places.map(p=>[p.id,p.name])].map(([id,name])=>{const o=el('option',name);o.value=id;return o;}));filter.value=old;}
    const current=document.getElementById('devices-current-place') as HTMLSelectElement;
    if(!current.matches(':focus')){current.replaceChildren(...event.places.map(p=>{const o=el('option',p.name);o.value=p.id;return o;}));current.value=event.location.place_id??'general';current.title=event.location.status==='ready'?'Текущее место известно':'Место неизвестно. Для локальной техники выбери его вручную.';}
    this.renderList();const active=document.activeElement;const editing=this.panel.contains(active)&&['INPUT','SELECT','TEXTAREA'].includes(active?.tagName??'');
    if(!editing)this.renderPanel();else {const current=this.data.items.find(d=>d.id===this.selected);if(current){const status=this.panel.querySelector('.device-connection');const state=this.panel.querySelector('.device-last-state');if(status)status.textContent=this.statusText(current);if(state)state.textContent=this.stateText(current);}}
  }
  private stateText(d:DeviceRecord):string {
    const known=d.state;const sent=d.sent;const items=[];
    if(typeof known.brightness==='number')items.push(`Яркость ${known.brightness}%`);
    if(typeof known.volume_percent==='number')items.push(known.muted===true?'Без звука':`Громкость ${known.volume_percent}%`);
    if(typeof known.power==='boolean')items.push(known.power?'Включено':'Выключено');
    if(d.kind==='led_strip'&&Object.keys(sent).length){const values=[];if(typeof sent.power==='boolean')values.push(sent.power?'включение':'выключение');if(sent.color)values.push(colors.find(([,color])=>color===sent.color)?.[0]??`цвет ${sent.color}`);if(typeof sent.brightness==='number')values.push(`яркость ${sent.brightness}%`);return `Отправлено: ${values.join(' · ')}. Факт не подтверждён.`;}
    return items.join(' · ')||'Фактическое состояние ещё не прочитано';
  }
  private statusText(d:DeviceRecord):string {return !this.online?'Нет связи с Чушпаном':!d.usable?'В другом месте':d.busy||this.writer.busy(d.id)?'Выполняется':d.connection==='observed'?'Последняя связь успешна':d.connection==='error'?'Нужна проверка подключения':'Подключение не проверено';}
  private renderList():void {
    this.list.replaceChildren();const records=(this.data?.items??[]).filter(d=>(this.place==='all'||d.place_id===this.place)&&`${d.name} ${d.room}`.toLocaleLowerCase().includes(this.search.toLocaleLowerCase()));
    for(const [kind,title]of Object.entries(kindNames)){
      const group=records.filter(d=>d.kind===kind);if(!group.length)continue;
      const section=el('section','','device-group');section.append(el('h2',`${title} · ${group.length}`));const cards=el('div','','device-cards');
      for(const d of group){const card=el('article','','device-tile');card.dataset.device=d.id;card.dataset.selected=String(d.id===this.selected);
        const choose=this.button('',()=>{this.selected=d.id;this.pendingProbe=null;this.renderList();this.renderPanel();},'device-tile-open');choose.setAttribute('aria-label','Открыть '+d.name);
        const symbol=el('span','','device-symbol');symbol.innerHTML=icon(d.kind==='led_strip'?'lamp':d.kind==='mac'?'cpu':'monitor');const words=el('span');words.append(el('strong',d.name),el('small',[this.data?.places.find(p=>p.id===d.place_id)?.name,d.room].filter(Boolean).join(' · ')));choose.append(symbol,words);
        const state=el('p',this.statusText(d),'device-connection');state.dataset.status=d.connection;
        card.append(choose,state,el('p',this.stateText(d),'device-last-state'));
        if(d.features.power_on||d.features.power_off){const actions=el('div','','device-quick');for(const [key,value,label]of [['power_on',true,'Включить'],['power_off',false,'Выключить']] as const){const b=this.button(label,()=>this.act(d,'led',{power:value},true));b.disabled=!this.canAct(d)||d.features[key]!==true;actions.append(b);}card.append(actions);}
        cards.append(card);
      }section.append(cards);this.list.append(section);
    }
    if(!records.length)this.list.append(el('div',this.data?.items.length?'Ничего не найдено. Измени фильтр или название.':'Добавь первое устройство. Уже настроенная техника появится здесь автоматически.','devices-empty'));
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
    const d=this.data?.items.find(d=>d.id===this.selected);this.panel.replaceChildren();this.panel.hidden=!d;if(!d)return;
    const heading=el('div','','device-panel-heading');heading.append(el('h2',d.name),this.button('×',()=>{this.selected='';this.renderList();this.renderPanel();},'device-close'));this.panel.append(heading,el('p',this.statusText(d),'device-connection'),el('p',this.stateText(d),'device-last-state'));
    if(!d.usable)this.panel.append(el('p',d.reason,'device-warning'));
    const inspect=this.button(d.kind==='led_strip'?'Проверить / переподключить Bluetooth':'Обновить состояние',()=>this.request({op:'inspect',id:d.id}));inspect.disabled=!this.canAct(d);this.panel.append(inspect);
    if(d.last_contact)this.panel.append(el('p','Последняя успешная связь: '+new Date(d.last_contact).toLocaleString('ru-RU'),'device-hint'));
    if(d.kind==='led_strip')this.ledPanel(d);
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
  }
  private ledPanel(d:DeviceRecord):void {
    const power=el('div','','device-quick');for(const [feature,value,label]of [['power_on',true,'Включить'],['power_off',false,'Выключить']] as const){const b=this.button(label,()=>this.act(d,'led',{power:value},true));b.disabled=!this.canAct(d)||d.features[feature]!==true;power.append(b);}this.panel.append(power);
    if(d.features.brightness)this.panel.append(this.slider(d,'Яркость ленты','brightness','led',d.state.brightness,d.sent.brightness));
    const picker=el('input');picker.type='color';picker.value=String(d.sent.color??d.state.color??'#FFFFFF');picker.setAttribute('aria-label','Выбрать цвет ленты');picker.disabled=!this.online||!d.usable||d.features.color!==true;picker.oninput=()=>this.act(d,'led',{color:picker.value.toUpperCase()});picker.onchange=()=>this.act(d,'led',{color:picker.value.toUpperCase()},true);
    const colorRow=el('div','','device-color-picker');const favorite=this.button('В любимые',()=>this.request({op:'favorites',id:d.id,revision:d.revision,colors:[...new Set([...d.favorites,picker.value.toUpperCase()])]}));colorRow.append(this.label('Выбранный RGB-цвет',picker),favorite);this.panel.append(colorRow);
    const swatches=el('div','','device-swatches');for(const [name,color]of colors){const b=this.button(name,()=>this.act(d,'led',{color},true),'device-swatch');b.style.setProperty('--color',color);b.disabled=!this.canAct(d)||!swatchAllowed(d,color);swatches.append(b);}this.panel.append(swatches,el('p','Тёплый белый — RGB-имитация. Отдельный белый канал не поддерживается.','device-hint'));
    if(d.sent.color){const b=this.button('Повторить последний выбранный цвет',()=>this.act(d,'led',{color:d.sent.color},true));b.disabled=!this.canAct(d)||!swatchAllowed(d,String(d.sent.color));this.panel.append(b);}
    if(d.favorites.length){const favorites=el('div','','device-favorites');favorites.append(el('h3','Любимые цвета'));for(const color of d.favorites){const b=this.button(color,()=>this.act(d,'led',{color},true),'device-swatch');b.style.setProperty('--color',color);b.disabled=!this.canAct(d)||!swatchAllowed(d,color);const remove=this.button('×',()=>this.request({op:'favorites',id:d.id,revision:d.revision,colors:d.favorites.filter(c=>c!==color)}));remove.setAttribute('aria-label','Удалить любимый цвет '+color);favorites.append(b,remove);}this.panel.append(favorites);}
    const test=el('details','','device-advanced');test.append(el('summary','Проверка функций контроллера'),el('p','Непроверенные функции заблокированы. Каждый тест отправляет ровно одну выбранную команду; затем подтверди наблюдение. Для полного RGB проверь красный, зелёный и синий.'));
    for(const [label,parameters]of [['Включение',{power:true}],['Выключение',{power:false}],['Яркость 50%',{brightness:50}],...colors.slice(4,7).map(([name,color])=>[name,{color}])] as [string,Values][]){const b=this.button('Тест: '+label,()=>{if(window.confirm(`Отправить тестовую команду «${label}» этой ленте?`))this.request({op:'probe_led',id:d.id,parameters,consent:true});});b.disabled=!this.canAct(d)||!d.features.probe;test.append(b);}
    if(this.pendingProbe?.id===d.id){const probe_id=this.pendingProbe.probe_id;test.open=true;test.append(el('p','Функция сработала так, как указано?'));for(const [value,label]of [[true,'Да, вижу результат'],[false,'Нет, не сработало']] as const)test.append(this.button(label,()=>{this.pendingProbe=null;this.request({op:'confirm_probe',id:d.id,probe_id,observed:value});}));}
    this.panel.append(test);
  }
  private metadata(d:DeviceRecord):void {
    const detail=el('details','','device-advanced');detail.append(el('summary','Настройки устройства'));
    const form=el('form');const name=el('input');name.value=d.name;name.required=true;name.maxLength=80;
    const place=this.select((this.data?.places??[]).map(p=>[p.id,p.name]),d.place_id);const room=el('input');room.value=d.room;room.maxLength=60;room.placeholder='Например, спальня';
    form.append(this.label('Название',name),this.label('Местоположение',place),this.label('Комната',room));const save=this.button('Сохранить настройки',()=>{});save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(form.reportValidity())this.request({op:'update',id:d.id,revision:d.revision,name:name.value,place_id:place.value,room:room.value});};
    const technical=el('details');technical.append(el('summary','Техническая информация'),el('p',`Протокол: ${d.protocol}`),el('code',d.endpoint),el('p',`Последний результат: ${outcomes[d.result_status??'']??'Не проверен'}`));
    detail.append(form,technical,this.button('Удалить устройство…',()=>{if(window.confirm(`Удалить «${d.name}» из списка? Это не выключит устройство и не отменит Bluetooth-сопряжение.`)){this.request({op:'delete',id:d.id,revision:d.revision});this.selected='';}},'device-delete'));this.panel.append(detail);
  }
  private openWizard():void {this.wizardStep=0;this.draft={kind:'led_strip',token:'',endpoint:'',name:'',place_id:this.data?.location.place_id??'general',room:''};this.candidates=[];this.checked='';this.wizardBusy=false;this.wizard=el('dialog','','device-wizard');this.wizard.setAttribute('aria-label','Добавление устройства');this.root.append(this.wizard);this.wizard.onclose=()=>{this.wizard?.remove();this.wizard=null;};this.renderWizard();this.wizard.showModal();}
  private closeWizard():void {this.wizard?.close();this.wizard?.remove();this.wizard=null;}
  private renderWizard():void {
    const dialog=this.wizard;if(!dialog)return;dialog.replaceChildren();const title=el('div','','device-panel-heading');title.append(el('h2','Новое устройство'),this.button('×',()=>this.closeWizard(),'device-close'));dialog.append(title,el('p',`Шаг ${this.wizardStep+1} из 4 · ${['Тип устройства','Поиск и выбор','Подключение и возможности','Название и место'][this.wizardStep]}`,'device-hint'));
    if(this.wizardStep===0){for(const k of this.data?.kinds??[]){const b=this.button(k.name,()=>{this.draft.kind=k.id;this.draft.name=k.name;this.wizardStep=1;this.candidates=[];this.renderWizard();},'device-kind-choice');dialog.append(b);}dialog.append(el('p','Лампы, розетки, пылесосы и другие протоколы появятся после подключения соответствующего адаптера. Фиктивное управление не добавляется.','device-hint'));}
    if(this.wizardStep===1){const scan=this.button(this.wizardBusy?'Ищу…':'Найти поблизости',()=>{this.wizardBusy=true;this.request({op:'discover',kind:this.draft.kind});this.renderWizard();},'primary');scan.disabled=!this.online||this.wizardBusy;dialog.append(scan,el('p','Поиск начинается только по нажатию. Bluetooth pairing не выполняется.','device-hint'));
      for(const candidate of this.candidates)dialog.append(this.button(candidate.name,()=>{this.draft.token=candidate.token;this.draft.name=candidate.name;this.wizardStep=2;this.checked='';this.renderWizard();},'device-kind-choice'));
      if(!this.wizardBusy&&!this.candidates.length)dialog.append(el('p','Список пока пуст. Запусти поиск или добавь вручную.','device-hint'));
      const manual=el('details');manual.append(el('summary','Добавить вручную'));const endpoint=el('input');endpoint.value=this.draft.endpoint;endpoint.placeholder=this.draft.kind==='mac'?'local-mac':'UUID устройства';endpoint.setAttribute('aria-label','Идентификатор подключения');manual.append(this.label('Идентификатор подключения',endpoint),this.button('Использовать идентификатор',()=>{this.draft.endpoint=endpoint.value.trim();this.draft.token='';this.wizardStep=2;this.checked='';this.renderWizard();}));dialog.append(manual);
    }
    if(this.wizardStep===2){dialog.append(el('p','Проверка читает доступный профиль и подключение. Цвет, яркость и питание не изменяются. Для LED физическую поддержку функций подтверждают отдельно после добавления.'));
      const check=this.button(this.wizardBusy?'Проверяю…':'Проверить подключение',()=>{this.wizardBusy=true;this.request({op:'check',...this.draft});this.renderWizard();},'primary');check.disabled=!this.online||this.wizardBusy;dialog.append(check,el('p',this.checked||'Подключение ещё не проверено.','device-hint'),this.button(this.checked?'Далее':'Добавить с непроверенным подключением',()=>{this.wizardStep=3;this.renderWizard();}));}
    if(this.wizardStep===3){const form=el('form');const name=el('input');name.value=this.draft.name;name.required=true;name.maxLength=80;const place=this.select((this.data?.places??[]).map(p=>[p.id,p.name]),this.draft.place_id);const room=el('input');room.value=this.draft.room;room.maxLength=60;
      form.append(this.label('Название',name),this.label('Местоположение',place),this.label('Комната',room));const save=this.button('Сохранить устройство',()=>{},'primary');save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(form.reportValidity()){this.draft={...this.draft,name:name.value,place_id:place.value,room:room.value};this.request({op:'add',...this.draft});}};dialog.append(form);}
    if(this.wizardStep>0)dialog.append(this.button('Назад',()=>{this.wizardBusy=false;this.wizardStep--;this.renderWizard();}));
    if(this.feedback.dataset.error==='true')dialog.append(el('p',this.feedback.textContent??'','device-warning'));
  }
}

import type {HudEvent} from './types';
import {ModulePage,card,date,el,empty,formFields,grid,metric,row,type ModuleChannel} from './module_ui';
export interface SyncPeer {id:string;name:string;kind:string;online:boolean;revoked_at:number|null;disconnected:number;last_seen:number|null;last_sync:number|null;categories:string[];capabilities:string[];connection_type?:'local'|'tailscale';}
export interface SyncData {running:boolean;error?:string|null;endpoint?:{host:string;port:number};tailnet_host?:string;url:string;fingerprint:string;invite:{url:string;expires:number;qr:string}|null;pending:{id:string;name:string;kind:string;code:string;expires:number}[];peers:SyncPeer[];categories:Record<string,string>;capabilities:string[];history:{peer_id:string;command_id:string;operation:string;created_at:number;status:string}[];conflicts:{id:number;peer_id:string;memory_id:number;revision:number;content:string;at:number}[];}
const grants:Record<string,string>={chat:'Текстовый чат с настроенной моделью',scenario:'Запуск доступных сценариев Mac',devices:'Управление устройствами из центра устройств',open_app:'Открытие приложений на Mac'};

export class DeviceSyncView extends ModulePage<SyncData> {
  private pairing:HTMLDialogElement|null=null;private pairingPhase:boolean|null=null;
  receive(event:HudEvent):boolean {const accepted=super.receive(event);if(accepted)this.renderPairing();return accepted;}
  constructor(post:(channel:ModuleChannel,value:object)=>void,navigate:(view:string)=>void){super('device_sync','my-devices','Мои устройства','Mac обнаруживается автоматически. Новые iPhone и iPad требуют подтверждения; разрешения выдаются отдельно.',post,navigate,[['overview','Устройства'],['sync','Синхронизация'],['commands','Удалённые команды']]);}
  protected refresh():void {this.send({op:'snapshot'});}
  protected render():void {
    if(!this.begin())return;const data=this.state!;if(data.error)this.content.append(card('Сервер недоступен',data.error));
    this.toolbar.replaceChildren(this.button('+ Подключить устройство',()=>this.wizard(),true),data.running?this.button('Остановить сервер',()=>this.send({op:'stop'})):this.button('Запустить сервер',()=>this.wizard()),this.button('Обновить статус',()=>this.refresh()),this.navigation('Свет и другие устройства','devices'),this.navigation('Приватность','privacy'));
    this.content.append(grid(metric('Главный Mac',data.running?'Сервер включён':'Сервер выключен',data.running?data.url:'Запуск выполняется по твоему действию'),metric('На связи',String(data.peers.filter(p=>data.running&&p.online).length),'Связь подтверждается запросом клиента'),metric('Доверенные',String(data.peers.filter(p=>!p.revoked_at).length),'Разрешения задаются отдельно')));
    for(const request of data.pending){const node=card(`${request.name} · ${request.kind}`,'Сравни код с экраном телефона. Срок: '+date(request.expires));node.append(el('strong',request.code,'pair-code'),row(this.button('Код совпадает — подключить',()=>this.send({op:'approve',id:request.id,code:request.code,confirmed:true}),true),this.button('Отклонить',()=>this.send({op:'reject',id:request.id}))));this.content.append(node);}
    if(this.tab==='overview'){
      if(!data.peers.length)this.content.append(empty('Подключённых устройств пока нет','Открой Чушпана на iPhone или iPad. Новое устройство появится здесь после запроса на сопряжение.'));
      this.content.append(grid(...data.peers.map(peer=>{const node=card(peer.name,`${peer.kind==='ipad'?'iPad':'iPhone'} · ${peer.revoked_at?'Доверие отозвано':peer.disconnected?'Отключено':data.running&&peer.online?'На связи':`Нет связи · ${peer.last_seen?'был(а) '+date(peer.last_seen):'ещё не подключался(ась)'}`}`);const settings=this.button('Разрешения',()=>this.peer(peer));settings.dataset.write='true';if(peer.revoked_at){settings.dataset.unavailable='true';settings.disabled=true;}const disconnect=this.button('Отключить',()=>this.send({op:'disconnect',id:peer.id}));if(peer.disconnected||peer.revoked_at){disconnect.dataset.unavailable='true';disconnect.disabled=true;}node.append(el('p','Тип соединения: '+(peer.connection_type==='tailscale'?'Удалённое · Tailscale':'Локальная сеть')),el('p','Последняя активность: '+date(peer.last_seen)),el('p','Разрешённые возможности: '+(peer.capabilities.map(value=>grants[value]||value).join(' · ')||'нет')),row(settings,disconnect));if(!peer.revoked_at)node.append(this.button('Отозвать доступ',()=>{if(confirm('Отозвать доступ '+peer.name+'?'))this.send({op:'revoke',id:peer.id});}));return node;})));
      const limitations=card('Возможности iPhone и iPad','Клиент в Safari: текстовый чат, разрешённые сценарии, свет, открытие приложений Mac, выборочная копия данных и история. Работает при открытой странице. Прямое управление экраном iOS, разблокировка и включение устройства недоступны.');this.content.append(limitations);
    }else if(this.tab==='sync'){
      if(!data.peers.length)this.content.append(empty('Сначала подключи устройство','После сопряжения можно выбрать категории и разрешения. По умолчанию они выключены.'));
      for(const peer of data.peers.filter(p=>!p.revoked_at)){const node=card(peer.name,'Чувствительная и проектная память исключена. Настройки с ключами и паролями не передаются.');node.append(el('p',peer.categories.map(c=>data.categories[c]||c).join(' · ')||'Передача данных выключена'),this.button('Выбрать категории',()=>this.peer(peer)));this.content.append(node);}
      for(const conflict of data.conflicts){const node=card('Исправление с мобильного устройства',conflict.content);node.append(el('p',`Воспоминание №${conflict.memory_id}, базовая версия ${conflict.revision}. Проверь текущую версию в памяти.`),row(this.navigation('Проверить в памяти','memory'),this.button('Применить с указанной версией',()=>{if(confirm('Применить этот текст к воспоминанию №'+conflict.memory_id+'?'))this.send({op:'conflict',id:conflict.id,revision:conflict.revision,accept:true});}),this.button('Отклонить',()=>this.send({op:'conflict',id:conflict.id,accept:false}))));this.content.append(node);}
    }else{
      if(!data.history.length)this.content.append(empty('Удалённых команд пока нет','Здесь появятся ID, отправитель, время и фактический статус каждой команды.'));
      for(const command of data.history){const peer=data.peers.find(p=>p.id===command.peer_id);this.content.append(card(command.operation.split(':')[0],`${peer?.name||command.peer_id.slice(0,8)} · ${date(command.created_at)} · ${command.status} · ${command.command_id.slice(0,8)}`));}
    }
  }
  private wizard():void {
    if(this.pairing?.open)return;
    this.pairingPhase=null;this.pairing=this.openDialog('Подключение устройства',el('div','','pair-wizard'));
    this.pairing.addEventListener('close',()=>{this.pairing=null;this.pairingPhase=null;});this.renderPairing();
  }
  private renderPairing():void {
    const modal=this.pairing,data=this.state;if(!modal?.open||!data)return;
    if(this.pairingPhase===false&&!data.running)return;
    this.pairingPhase=data.running;const node=modal.querySelector<HTMLElement>('.pair-wizard')!;node.replaceChildren();
    node.append(el('p','Чушпан автоматически запускает защищённый сервис и публикует его в Bonjour. Новое устройство появится после нажатия «Подключить» в приложении iPhone/iPad; подтверждение не включает разрешения.'),el('p',data.running?`Состояние: обнаружение включено · порт ${data.endpoint?.port??'—'}${data.tailnet_host?' · Tailscale настроен':''}`:'Состояние: сервер остановлен.'));
    if(!data.running){node.append(this.button('Запустить автоматическое обнаружение',()=>this.send({op:'start'}),true));}
    else {
      node.append(el('p','Сравни шестизначный код на Mac и мобильном устройстве, затем подтверди запрос. Сертификат проверяется сопряжением; устанавливать CA в iOS не нужно.'));
      node.append(el('p','Tailscale подключается только при установленном клиенте и разрешённой VPN-конфигурации. Сервер привязывается к локальному и tailnet адресам, не к публичному интерфейсу.'));
      node.append(this.button('Остановить сервер',()=>this.send({op:'stop'})));
    }
    const advanced=el('details');advanced.append(el('summary','Дополнительные настройки'));
    const {form,read}=formFields([{name:'host',label:'IPv4-адрес Mac (локальный или Tailscale)',value:data.endpoint?.host??'',max:64},{name:'port',label:'Порт (0 — автоматический)',kind:'number',value:data.endpoint?.port??0,min:0,max:65535}]);
    form.append(this.button('Запустить по указанному адресу',()=>{if(form.reportValidity()){const values=read();this.send({op:'start',host:values.host,port:Number(values.port)});}},true));form.onsubmit=e=>e.preventDefault();
    advanced.append(form,el('p','Резервное сопряжение для старых версий: можно создать одноразовый QR. CA не требуется устанавливать; ручная сверка отпечатка сохранена только для совместимости со старым клиентом.'),row(this.button('Скачать сертификат',()=>this.send({op:'certificate'})),this.button('Создать QR-приглашение',()=>this.send({op:'invite'}))));
    if(data.invite&&data.invite.expires>Date.now()/1000){const image=el('img');image.src=data.invite.qr;image.alt='Одноразовое приглашение для подключения устройства';image.className='pair-qr';advanced.append(image,el('p',data.invite.url,'certificate-fingerprint'),el('p','Приглашение действует до: '+date(data.invite.expires)));}
    node.append(advanced);
    this.controls();
  }
  private peer(peer:SyncPeer):void {
    const node=el('div');node.append(el('p',`Идентификатор: ${peer.id.slice(0,16)}…`, 'certificate-fingerprint'));const {form,read}=formFields([{name:'name',label:'Имя устройства',value:peer.name,required:true,max:80}]);form.append(this.button('Переименовать',()=>{if(form.reportValidity()){this.send({op:'rename',id:peer.id,name:read().name});modal.close();}}));form.onsubmit=e=>e.preventDefault();node.append(form);
    const categories=el('fieldset');categories.append(el('legend','Выборочная синхронизация'));for(const [key,label]of Object.entries(this.state!.categories)){const input=el('input');input.type='checkbox';input.name='category';input.value=key;input.checked=peer.categories.includes(key);const wrapper=el('label',label,'module-check');wrapper.append(input);categories.append(wrapper);}node.append(categories);
    const permissions=el('fieldset');permissions.append(el('legend','Разрешённые команды'));for(const key of this.state!.capabilities){const input=el('input');input.type='checkbox';input.name='capability';input.value=key;input.checked=peer.capabilities.includes(key);const label=el('label',grants[key]||key,'module-check');label.append(input);permissions.append(label);}node.append(permissions,el('p','Запуск сценариев распространяется на доступные сценарии Mac. Опасные шаги по-прежнему требуют конкретного подтверждения.'));
    const modal=this.openDialog(peer.name,node);node.append(row(this.button('Сохранить разрешения',()=>{const selected=(name:string)=>[...node.querySelectorAll<HTMLInputElement>(`input[name="${name}"]:checked`)].map(input=>input.value);this.send({op:'permissions',id:peer.id,categories:selected('category'),capabilities:selected('capability')});modal.close();},true),this.button('Отозвать доверие',()=>{if(confirm('Отозвать доступ '+peer.name+'? Сохранённая копия данных в браузере удаляется на самом устройстве.')){this.send({op:'revoke',id:peer.id});modal.close();}})));
  }
}

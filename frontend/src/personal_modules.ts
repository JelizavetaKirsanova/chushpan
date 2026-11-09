import type {HudEvent} from './types';
import {AnalyticsView,type AnalyticsData} from './analytics';
import {DeviceSyncView,type SyncData} from './device_sync';
import {MacCenterView,type MacData} from './mac_center';
import {MemoryLibraryView,type MemoryData} from './memory_library';
import {card,el,grid,row,type ModuleChannel,type ModuleEvent} from './module_ui';
import {icon} from './dashboard';

export class PersonalModules {
  private views;private quick:HTMLElement;private capture:HTMLElement;private summaries:Record<string,string>={};
  constructor(post:(channel:ModuleChannel,value:object)=>void,private navigate:(view:string)=>void){
    this.views=[new MemoryLibraryView(post,navigate),new MacCenterView(post,navigate),new DeviceSyncView(post,navigate),new AnalyticsView(post,navigate)];
    this.quick=document.getElementById('module-quick-links')!;this.quick.className='module-quick-links';this.quickLinks();
    this.capture=el('aside','','capture-indicator');this.capture.hidden=true;this.capture.setAttribute('role','status');this.capture.append(el('span','Захват экрана по твоему запросу'),this.action('Остановить',()=>post('mac_center',{op:'cancel'})));document.body.append(this.capture);
    const privacy=document.getElementById('module-privacy')!;privacy.className='workspace-page module-page module-privacy';const heading=el('div','','page-heading');heading.append(el('small','ТВОИ ДАННЫЕ'),el('h1','Приватность'),el('p','Общий доступ к данным новых разделов. Настройки хранятся локально.'));privacy.append(heading);
    const memory=card('Память','Воспоминания, источники, проекты, история использования и экспорт. Удаление исключает записи из поиска. Старые резервные копии очищаются отдельно.');memory.append(this.nav('Открыть память','memory'));
    const screen=card('Экран и приложения','Захват только по запросу. Настройки доступа позволяют исключить приложения. Превью можно убрать в любой момент.');screen.append(row(this.nav('Разрешения и исключения','settings'),this.action('Остановить захват и убрать превью',()=>post('mac_center',{op:'cancel'}))),this.nav('Управление Mac','mac-control'));
    const devices=card('Доверенные устройства','На каждом устройстве — отдельные категории данных и команды. Отзыв доверия запрещает новые подписанные запросы. Локальная копия в Safari удаляется на телефоне.');devices.append(this.nav('Управлять устройствами','my-devices'));
    const analytics=card('Личная аналитика','Сбор выключен по умолчанию. Минимальные события, исключения приложений и срок хранения доступны в одном месте.');analytics.append(this.nav('Сбор, экспорт и удаление','analytics'));
    privacy.append(grid(memory,screen,devices,analytics));
    const refreshSync=()=>{if(!document.hidden&&document.querySelector('.hud')?.getAttribute('data-view')==='my-devices')this.views[2].refreshIfIdle();};
    document.addEventListener('visibilitychange',refreshSync);window.setInterval(refreshSync,10000);
  }
  private nav(label:string,view:string):HTMLButtonElement{return this.action(label,()=>this.navigate(view));}
  private action(label:string,action:()=>void):HTMLButtonElement{const button=el('button',label);button.type='button';button.onclick=action;return button;}
  private quickLinks():void {const entries=[['memory','Память','note'],['mac-control','Управление Mac','monitor'],['my-devices','Мои устройства','link'],['analytics','Аналитика','chart']];this.quick.replaceChildren(...entries.map(([view,title,glyph])=>{const button=el('button','','module-quick-card');button.type='button';const symbol=el('span','','module-symbol');symbol.innerHTML=icon(glyph);const text=el('span');text.append(el('strong',title),el('small',this.summaries[view]||'Ещё нет данных'));button.append(symbol,text,el('span','›'));button.onclick=()=>this.navigate(view);return button;}));}
  connection(online:boolean):void {for(const view of this.views)view.connection(online);if(!online){this.capture.hidden=true;this.summaries={};this.quickLinks();}}
  openAnalyticsCollection():void {(this.views[3] as AnalyticsView).openCollection();this.navigate('analytics');}
  receive(event:HudEvent):void {const view=this.views.find(view=>view.channel===event.type);if(view&&!view.receive(event))return;const module=event as ModuleEvent;if(event.type==='mac_center'&&module.capturing!==undefined)this.capture.hidden=!module.capturing;if(!module.data)return;
    if(event.type==='memory_library'){const data=module.data as MemoryData;this.summaries.memory=`${data.total} воспоминаний · ${data.used_today} использовано сегодня`;}
    else if(event.type==='mac_center'){const data=module.data as MacData;this.summaries['mac-control']=data.apps.find(a=>a.active)?.name||'Приложение ещё не проверено';}
    else if(event.type==='device_sync'){const data=module.data as SyncData;this.summaries['my-devices']=`${data.peers.filter(p=>data.running&&p.online).length} на связи · ${data.running?'TLS включён':'сервер выключен'}`;}
    else if(event.type==='analytics'){const data=module.data as AnalyticsData;this.summaries.analytics=data.policy.enabled?`${data.summary.count} обращений за выбранный период`:'Сбор данных выключен';}
    else return;this.quickLinks();
  }
}

import type {CinemaEvent} from './cinema';
import {playbackTime} from './cinema';

export interface PlaybackSnapshot {
  state:string;code:string;message:string;revision:number;token:string;last_successful_at:number|null;pending_command?:string;
  source:{url:string;title_id:string;episode_id:string;source_id:string;link_id:string;adapter_id:string;content_type:string;voice:string;capabilities:Record<string,unknown>;validation:{content_verified?:boolean;live_player_verified?:boolean}}|null;
}
const labels:Record<string,string>={IDLE:'Просмотр закрыт',SEARCHING:'Ищу источник',VALIDATING_SOURCE:'Проверяю видео',CHECKING_REGION:'Проверяю доступность',CHECKING_PLAYER:'Проверяю плеер',READY:'Источник подготовлен',OPENING:'Открываю источник',WAITING_FOR_PLAYER:'Ожидаю плеер',STARTING:'Проверяю запуск',PLAYING:'Видео воспроизводится',PAUSED:'На паузе',BLOCKED:'Нужно действие',FAILED:'Не удалось подключиться'};
function el<K extends keyof HTMLElementTagNameMap>(tag:K,text=''):HTMLElementTagNameMap[K]{const n=document.createElement(tag);n.textContent=text;return n;}

/** Compact session screen outside poster cards. Updates never recreate the video. */
export class CinemaViewer {
  readonly root=el('section');
  private status=el('p');
  private message=el('p');
  private time=el('p');
  private controls=el('div');
  private signature='';
  private online=false;
  private data:CinemaEvent|null=null;
  constructor(private send:(value:object)=>void){
    this.root.className='cinema-viewer';this.root.hidden=true;this.root.setAttribute('aria-label','Экран просмотра');
    this.status.setAttribute('role','status');this.message.setAttribute('aria-live','polite');this.controls.className='cinema-viewer-controls';
    this.root.append(this.status,this.message,this.time,this.controls);
  }
  connection(value:boolean):void{this.online=value;this.signature='';if(this.data)this.receive(this.data);}
  private button(label:string,action:()=>void,enabled=true):HTMLButtonElement{const b=el('button',label);b.type='button';b.disabled=!this.online||!enabled;b.onclick=action;return b;}
  receive(data:CinemaEvent):void{
    this.data=data;const p=data.playback;
    this.root.hidden=!p||p.state==='IDLE';if(!p)return;
    this.status.textContent=labels[p.state]??'Подключение';this.root.dataset.state=p.state;
    this.message.textContent=p.message;const source=p.source;
    const item=data.items.find(i=>i.id===source?.title_id);const episode=item?.episodes.find(e=>e.id===source?.episode_id);
    this.time.textContent=item?`${item.title}${item.kind==='series'&&episode?` · ${episode.season} сезон, ${episode.episode} серия`:''} · сохранено ${playbackTime(episode?.position??0)}`:'';
    const signature=JSON.stringify([p.token,p.pending_command,data.session?.mode,data.preferences.allow_browser,source?.adapter_id,source?.validation.content_verified,source?.capabilities,source?.voice,episode?.links,item?.episodes.map(e=>[e.id,e.name]),this.online]);
    if(signature===this.signature)return;this.signature=signature;this.controls.replaceChildren();
    if(!source||!item||!episode)return;
    const op=(action:string)=>this.send({op:'playback_control',token:p.token,action});
    const controllable=['html5','native','embedded'].includes(data.session?.mode??'');
    this.controls.append(this.button('Продолжить',()=>op('play'),controllable&&!p.pending_command),this.button('Пауза',()=>op('pause'),controllable&&!p.pending_command));
    const links=el('select');links.setAttribute('aria-label','Источник просмотра');
    for(const link of episode.links){const o=el('option',`${link.source_name}${link.voice?` · ${link.voice}`:''}`);o.value=link.id;links.append(o);}links.value=source.link_id;links.disabled=!this.online;
    this.controls.append(links,this.button('Выбрать источник',()=>this.send({op:'playback_switch',token:p.token,link_id:links.value})));
    const voices=source.capabilities.audio_tracks;
    if(Array.isArray(voices)&&voices.length){const select=el('select');select.setAttribute('aria-label','Дорожка плеера');for(const voice of voices){if(typeof voice!=='string')continue;const o=el('option',voice);o.value=voice;select.append(o);}select.value=source.voice;select.disabled=!this.online;select.onchange=()=>this.send({op:'playback_voice',token:p.token,voice:select.value});this.controls.append(select);}
    if(item.kind==='series'){
      const select=el('select');select.setAttribute('aria-label','Серия просмотра');for(const ep of item.episodes){const o=el('option',`${ep.season} сезон · ${ep.episode} серия${ep.name?` · ${ep.name}`:''}`);o.value=ep.id;select.append(o);}select.value=episode.id;select.disabled=!this.online;
      this.controls.append(select,this.button('Смотреть серию',()=>this.send({op:'open',title_id:item.id,episode_id:select.value})));
    }
    const config=data.sources.find(s=>s.id===source.source_id);
    const direct=config?.adapter==='direct'&&source.validation.content_verified;
    const native=config?.adapter==='local'||direct;
    if(native&&source.adapter_id!=='avkit')this.controls.append(this.button('В нативном плеере',()=>this.send({op:'playback_switch',token:p.token,player:'native'})));
    if(config?.adapter!=='local'&&source.adapter_id!=='safari')this.controls.append(this.button('В Safari',()=>this.send({op:'playback_switch',token:p.token,player:'safari'}),data.preferences.allow_browser!==false));
    if(['html5','native'].includes(data.session?.mode??''))this.controls.append(this.button('Полный экран',()=>op('fullscreen')));
    this.controls.append(this.button('Сохранить и закрыть',()=>op('close')));
  }
}

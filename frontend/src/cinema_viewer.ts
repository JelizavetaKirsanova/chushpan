import type {CinemaEvent} from './cinema';
import {playbackTime,parsePlaybackTime} from './cinema';

export interface PlaybackSnapshot {
  state:string;code:string;message:string;revision:number;token:string;last_successful_at:number|null;pending_command?:string;
  source:{url:string;title_id:string;episode_id:string;source_id:string;link_id:string;adapter_id:string;content_type:string;voice:string;capabilities:Record<string,unknown>;validation:{content_verified?:boolean;live_player_verified?:boolean}}|null;
}
const labels:Record<string,string>={IDLE:'Просмотр закрыт',SEARCHING:'Ищу источник',VALIDATING_SOURCE:'Проверяю видео',CHECKING_REGION:'Проверяю доступность',CHECKING_PLAYER:'Проверяю плеер',READY:'Источник подготовлен',OPENING:'Открываю источник',WAITING_FOR_PLAYER:'Ожидаю плеер',STARTING:'Проверяю запуск',RESTORING_POSITION:'Восстанавливаю позицию',ENTERING_FULLSCREEN:'Включаю полный экран плеера',PLAYING:'Видео воспроизводится',PAUSED:'На паузе',BLOCKED:'Нужно действие',FAILED:'Не удалось подключиться'};
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
  private requestId='';
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
    this.message.textContent=data.error??p.message;const source=p.source;
    if(data.error&&source){const voice=this.controls.querySelector<HTMLSelectElement>('[aria-label="Дорожка плеера"]');if(voice)voice.value=source.voice;}
    if(data.watch_progress?.request_id)this.requestId=data.watch_progress.request_id;
    if(['PLAYING','PAUSED','IDLE','FAILED'].includes(p.state))this.requestId='';
    const item=data.items.find(i=>i.id===source?.title_id);const episode=item?.episodes.find(e=>e.id===source?.episode_id);
    this.time.textContent=item?`${item.title}${item.kind==='series'&&episode?` · ${episode.season} сезон, ${episode.episode} серия`:''} · сохранено ${playbackTime(episode?.position??0)}`:'';
    const signature=JSON.stringify([p.token,p.state,this.requestId,p.pending_command,data.session?.mode,data.preferences.allow_browser,source?.adapter_id,source?.validation.content_verified,source?.capabilities,source?.voice,episode?.links,item?.episodes.map(e=>[e.id,e.name]),this.online]);
    if(signature===this.signature)return;this.signature=signature;this.controls.replaceChildren();
    if(this.requestId)this.controls.append(this.button('Отменить поиск и запуск',()=>this.send({op:'watch_cancel',request_id:this.requestId})));
    if(!source||!item||!episode)return;
    const op=(action:string)=>this.send({op:'playback_control',token:p.token,action});
    const controllable=['html5','native','embedded','learned'].includes(data.session?.mode??'');
    this.controls.append(this.button('Продолжить',()=>op('play'),controllable&&!p.pending_command),this.button('Пауза',()=>op('pause'),controllable&&source.capabilities.pause!==false&&!p.pending_command));
    if(source.capabilities.seek){const time=el('input');time.type='text';time.placeholder='37:00';time.setAttribute('aria-label','Перейти к таймкоду');
      this.controls.append(time,this.button('Перейти к времени',()=>{try{const position=parsePlaybackTime(time.value);if(episode.duration&&position>=episode.duration)throw new Error('Таймкод выходит за длительность видео.');this.send({op:'playback_control',token:p.token,action:'seek',position});}catch(error){this.message.textContent=error instanceof Error?error.message:'Проверь таймкод.';}}));}
    const links=el('select');links.setAttribute('aria-label','Источник просмотра');
    for(const link of episode.links){const o=el('option',`${link.source_name}${link.voice?` · ${link.voice}`:''}`);o.value=link.id;links.append(o);}links.value=source.link_id;links.disabled=!this.online;
    this.controls.append(links,this.button('Выбрать источник',()=>this.send({op:'playback_switch',token:p.token,link_id:links.value})));
    const voices=source.capabilities.audio_tracks;
    if(Array.isArray(voices)&&voices.length){const select=el('select');select.setAttribute('aria-label','Дорожка плеера');for(const voice of voices){if(typeof voice!=='string')continue;const o=el('option',voice);o.value=voice;select.append(o);}select.value=source.voice;select.disabled=!this.online;select.onchange=()=>this.send({op:'playback_voice',token:p.token,voice:select.value});this.controls.append(select);}
    if(item.kind==='series'){
      const select=el('select');select.setAttribute('aria-label','Серия просмотра');for(const ep of item.episodes){const o=el('option',`${ep.season} сезон · ${ep.episode} серия${ep.name?` · ${ep.name}`:''}`);o.value=ep.id;select.append(o);}select.value=episode.id;select.disabled=!this.online;
      this.controls.append(select,this.button('Смотреть серию',()=>this.send({op:'watch',title_id:item.id,episode_id:select.value,request_id:`watch-viewer-${Date.now()}`})));
    }
    const config=data.sources.find(s=>s.id===source.source_id);
    const direct=config?.adapter==='direct'&&source.validation.content_verified;
    const native=config?.adapter==='local'||direct;
    if(native&&source.adapter_id!=='avkit')this.controls.append(this.button('В нативном плеере',()=>this.send({op:'playback_switch',token:p.token,player:'native'})));
    if(config?.adapter!=='local'&&source.adapter_id!=='safari')this.controls.append(this.button('В Safari',()=>this.send({op:'playback_switch',token:p.token,player:'safari'}),data.preferences.allow_browser!==false));
    if(['html5','native','learned'].includes(data.session?.mode??''))this.controls.append(this.button(source.capabilities.fullscreen?'Выйти из полного экрана':'Полный экран',()=>op('fullscreen'),source.capabilities.fullscreen_available!==false));
    this.controls.append(this.button('Сохранить и закрыть',()=>op('close')));
  }
}

import type {CinemaEvent} from './cinema';

export interface EmbeddedPlayerState {token:string;url:string;title:string;position:number;title_id:string;episode_id:string}
export interface EmbeddedControl {token:string;action:string;seconds?:number;command_id:string}

/** The media element survives HUD refreshes; only its own events report progress. */
export class CinemaEmbeddedPlayer {
  private root=document.createElement('section');
  private video:HTMLVideoElement|null=null;
  private current:EmbeddedPlayerState|null=null;
  private sequence=0;
  private sentAt=0;
  private previous=0;
  private confirmed=false;
  private commandId='';
  private feedback=document.createElement('p');
  constructor(private send:(value:object)=>void,private container:HTMLElement){this.root.className='cinema-embedded';}
  prepareSwitch():void {if(this.video&&this.current){this.video.pause();this.report('pause');}}
  receive(event:CinemaEvent):void {
    if(this.current&&event.session?.mode!=='embedded'){this.clear();}
    const state=event.embedded_player;
    if(state&&state.token!==this.current?.token){
      this.clear();this.current=state;this.sequence=0;this.sentAt=0;this.confirmed=false;this.previous=state.position;
      const title=document.createElement('h3');title.textContent=state.title;
      const video=document.createElement('video');this.video=video;video.controls=true;video.playsInline=true;video.preload='metadata';video.src=state.url;video.setAttribute('aria-label',`Видео: ${state.title}`);
      this.feedback.textContent='Ожидаю запуска видео. Если оно не началось, нажми ▶.';this.feedback.setAttribute('role','status');
      const close=document.createElement('button');close.type='button';close.textContent='Сохранить и закрыть плеер';close.onclick=()=>{this.report('close');this.clear();};
      const fullscreen=document.createElement('button');fullscreen.type='button';fullscreen.textContent='Полный экран';fullscreen.onclick=()=>{
        const api=(window as unknown as {pywebview?:{api?:{toggle_fullscreen?:()=>Promise<void>}}}).pywebview?.api;
        if(api?.toggle_fullscreen){this.root.classList.toggle('cinema-video-fullscreen');void api.toggle_fullscreen();}
        else if(document.fullscreenElement)void document.exitFullscreen();else void video.requestFullscreen?.();
      };
      const series=document.createElement('div');series.className='cinema-embedded-episodes';
      const item=event.items.find(i=>i.id===state.title_id);
      if(item?.kind==='series'){
        const season=document.createElement('select');season.setAttribute('aria-label','Сезон встроенного плеера');
        const episode=document.createElement('select');episode.setAttribute('aria-label','Серия встроенного плеера');
        for(const n of [...new Set(item.episodes.map(e=>e.season))]){const o=document.createElement('option');o.value=String(n);o.textContent=`Сезон ${n}`;season.append(o);}
        const selected=item.episodes.find(e=>e.id===state.episode_id);season.value=String(selected?.season??1);
        const fill=()=>{episode.replaceChildren();for(const e of item.episodes.filter(e=>e.season===Number(season.value))){const o=document.createElement('option');o.value=e.id;o.textContent=`Серия ${e.episode}${e.name?` · ${e.name}`:''}`;episode.append(o);}if(selected?.season===Number(season.value))episode.value=selected.id;};fill();season.onchange=fill;
        const open=document.createElement('button');open.type='button';open.textContent='Открыть серию';open.onclick=()=>{video.pause();this.report('pause');this.send({op:'open',title_id:item.id,episode_id:episode.value});};series.append(season,episode,open);
      }
      this.root.replaceChildren(title,video,this.feedback,series,fullscreen,close);this.container.append(this.root);
      video.onloadedmetadata=()=>{if(state.position&&Number.isFinite(video.duration))video.currentTime=Math.min(state.position,Math.max(0,video.duration-.1));void video.play().catch(()=>{this.feedback.textContent='Нажми ▶ в плеере, чтобы начать просмотр.';});};
      video.ontimeupdate=()=>{const advanced=!video.paused&&!video.seeking&&video.currentTime>this.previous+.05&&video.currentTime-this.previous<5;this.previous=video.currentTime;if(advanced&&!this.confirmed){this.confirmed=true;this.feedback.textContent='Воспроизведение подтверждено.';this.report('playing');}else if(advanced&&Date.now()-this.sentAt>=(event.preferences.poll_seconds??5)*1000)this.report('periodic');};
      video.onpause=()=>this.report('pause');video.onseeked=()=>{this.previous=video.currentTime;this.report('seek');};video.onended=()=>this.report('ended');video.onerror=()=>{this.feedback.textContent='Не удалось воспроизвести видео. Проверь источник или выбери другой.';this.report('error');};
    }
    if(event.embedded_control&&this.current?.token===event.embedded_control.token)this.control(event.embedded_control);
  }
  private control(command:EmbeddedControl):void {
    const video=this.video;if(!video)return;this.commandId=command.command_id;
    if(command.action==='pause'){video.pause();this.report('pause');}
    if(command.action==='play'){this.confirmed=false;this.previous=video.currentTime;void video.play().catch(()=>this.report('error'));}
    if(command.action==='seek'&&Number.isFinite(video.duration))video.currentTime=Math.max(0,Math.min(video.duration-.1,video.currentTime+(command.seconds??0)));
  }
  private report(event:string):void {
    if(!this.video||!this.current)return;
    const duration=Number.isFinite(this.video.duration)?this.video.duration:0;
    const position=duration?Math.min(duration,Math.max(0,this.video.currentTime)):0;
    this.send({op:'embedded_event',token:this.current.token,sequence:++this.sequence,event,position,duration,paused:this.video.paused,command_id:this.commandId});this.sentAt=Date.now();
    if(['playing','pause','seek','error'].includes(event))this.commandId='';
  }
  private clear():void {
    if(this.root.classList.contains('cinema-video-fullscreen')){this.root.classList.remove('cinema-video-fullscreen');const api=(window as unknown as {pywebview?:{api?:{toggle_fullscreen?:()=>Promise<void>}}}).pywebview?.api;void api?.toggle_fullscreen?.();}
    if(this.video){this.video.onpause=null;this.video.ontimeupdate=null;this.video.onseeked=null;this.video.onended=null;this.video.onerror=null;this.video.onloadedmetadata=null;this.video.pause();this.video.removeAttribute('src');this.video.load();}
    this.video=null;this.current=null;this.root.remove();
  }
}

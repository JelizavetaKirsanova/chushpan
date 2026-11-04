import type {HudEvent} from './types';
import {icon} from './dashboard';

export interface CinemaSource {id:string;name:string;base_url:string;search_url:string;adapter:'manual'|'html5';enabled:boolean}
export interface CinemaLink {id:string;episode_id:string;source_id:string;source_name:string;url:string;voice:string}
export interface CinemaEpisode {id:string;title_id:string;season:number;episode:number;position:number;duration:number;watched:number;last_viewed:string|null;last_link_id:string|null;links:CinemaLink[]}
export interface CinemaTitle {id:string;title:string;kind:'movie'|'series';year:number;poster_url:string;description:string;status:'watching'|'planned'|'watched';favorite:number;episodes:CinemaEpisode[];current:CinemaEpisode|null}
interface CinemaHistory {id:number;episode_id:string;title:string;title_id:string;season:number;episode:number;position:number;duration:number;created_at:string;event:string;source_name:string;watched:number}
export interface CinemaEvent {type:'cinema';items:CinemaTitle[];sources:CinemaSource[];history:CinemaHistory[];preferences:{watched_threshold:number;auto_next:boolean;startup:'continue'|'library';poll_seconds:number;devices:Record<string,'mac'|'airplay'>};location:string;devices:{id:string;name:string;available:boolean;reason:string}[];durable:boolean;session:{title_id:string;episode_id:string;mode:string}|null;message:string;error?:string;saved?:string;navigate?:boolean;matches?:{title:string;description?:string;poster_url?:string;catalogue:string}[]}

export function playbackTime(value:number):string {
  const total=Math.max(0,Math.floor(Number.isFinite(value)?value:0));
  return total>=3600?`${Math.floor(total/3600)}:${String(Math.floor(total/60)%60).padStart(2,'0')}:${String(total%60).padStart(2,'0')}`:`${Math.floor(total/60)}:${String(total%60).padStart(2,'0')}`;
}
export function parsePlaybackTime(value:string):number {
  if(!/^\d+(?::[0-5]\d){0,2}$/.test(value.trim()))throw new Error('Укажи секунды или время в формате 12:34 / 1:12:34.');
  return value.trim().split(':').reduce((total,part)=>total*60+Number(part),0);
}
export function viewingProgress(episode:CinemaEpisode|null):number {return episode&&episode.duration>0?Math.min(100,Math.max(0,episode.position/episode.duration*100)):0;}
export function episodeCaption(item:CinemaTitle,ep=item.current):string {return item.kind==='series'&&ep?`Сезон ${ep.season} · Серия ${ep.episode}`:'Фильм';}

function el<K extends keyof HTMLElementTagNameMap>(tag:K,text='',className=''):HTMLElementTagNameMap[K] {const node=document.createElement(tag);node.textContent=text;node.className=className;return node;}
const statuses={watching:'Смотрю',planned:'Планирую',watched:'Просмотрено'};
const events:Record<string,string>={manual:'Вручную',periodic:'Просмотр',pause:'Пауза',ended:'Завершено',switch:'Смена серии',close:'Закрытие',seek:'Перемотка',playing:'Воспроизведение'};

export class CinemaView {
  private root=document.getElementById('cinema-root')!;
  private content=el('div','','cinema-content');
  private feedback=el('p','','cinema-feedback');
  private tabs=el('nav','','cinema-tabs');
  private data:CinemaEvent|null=null;
  private online=false;
  private tab='continue';
  private query='';
  private status='all';
  private list=false;
  private dialog:HTMLDialogElement|null=null;
  private selectedLink:string|null=null;
  private search:HTMLInputElement;
  private sourceFilter='all';
  private dateFilter='';
  private first=true;
  constructor(private send:(value:object)=>void,private chat:(text:string)=>void,private navigate:()=>void,private listen:()=>void) {
    const heading=el('header','','cinema-heading');const title=el('h1');title.innerHTML=icon('film');title.append(el('span','Кино и сериалы'));
    const searchForm=el('form','','cinema-search');const glyph=el('span');glyph.innerHTML=icon('search');this.search=el('input');this.search.type='search';this.search.placeholder='Найти фильм или сериал…';this.search.setAttribute('aria-label','Поиск по кинотеке');this.search.maxLength=200;
    this.search.oninput=()=>{this.query=this.search.value;this.render();};searchForm.onsubmit=e=>{e.preventDefault();this.tab='search';this.request({op:'search',query:this.query});this.render();};searchForm.append(glyph,this.search);
    const mic=this.button('Голосовая команда',this.listen,'cinema-mic');mic.innerHTML=icon('mic');mic.setAttribute('aria-label','Голосовая команда');heading.append(title,searchForm,mic);
    this.tabs.setAttribute('aria-label','Разделы кинотеки');this.feedback.setAttribute('role','status');this.feedback.setAttribute('aria-live','polite');
    const composer=el('form','','cinema-composer');const spark=el('span');spark.innerHTML=icon('activity');const input=el('input');input.placeholder='Скажи, что посмотреть…';input.setAttribute('aria-label','Команда Чушпану о кино');input.maxLength=400;
    const submit=this.button('Отправить',()=>{});submit.type='submit';submit.innerHTML=icon('send');submit.setAttribute('aria-label','Отправить команду о кино');composer.append(spark,input,submit);composer.onsubmit=e=>{e.preventDefault();if(this.online&&input.value.trim()){this.chat(input.value.trim());input.value='';}};
    this.root.append(heading,this.tabs,this.feedback,this.content,composer);
    document.addEventListener('workspace:navigate',event=>{if((event as CustomEvent).detail==='cinema'&&this.online)this.request({op:'refresh'});});
    this.connection(false);
  }
  private button(label:string,action:()=>void,cls=''):HTMLButtonElement {const b=el('button',label,cls);b.type='button';b.disabled=!this.online;b.onclick=action;return b;}
  private field(label:string,input:HTMLElement):HTMLLabelElement {input.setAttribute('aria-label',label);const node=el('label','','cinema-field');node.append(el('span',label),input);return node;}
  private input(value='',type='text'):HTMLInputElement {const n=el('input');n.type=type;n.value=value;return n;}
  private select(options:[string,string][],value:string):HTMLSelectElement {const node=el('select');for(const [id,label]of options){const option=el('option',label);option.value=id;node.append(option);}node.value=value;return node;}
  private request(value:object):void {if(this.online)this.send(value);}
  connection(online:boolean):void {this.online=online;if(!online)this.feedback.textContent='Нет соединения с Чушпаном. Последние данные могут устареть.';else this.request({op:'refresh'});this.root.querySelectorAll<HTMLInputElement|HTMLSelectElement|HTMLButtonElement>('input,select,button').forEach(n=>n.disabled=!online);this.render();}
  receive(event:HudEvent):void {
    if(event.type!=='cinema')return;
    this.data=event;
    if(this.first){this.tab=event.preferences.startup;this.first=false;}
    this.feedback.textContent=event.error??event.message??'';this.feedback.dataset.error=String(!!event.error);
    if(event.error&&this.dialog){let error=this.dialog.querySelector<HTMLElement>('.cinema-form-error');if(!error){error=el('p','','cinema-form-error');error.setAttribute('role','alert');this.dialog.append(error);}error.textContent=event.error;}
    if(event.saved&&this.dialog?.dataset.form==='true')this.closeDialog();
    if(event.navigate){this.tab='library';this.navigate();}
    if(!event.durable)this.feedback.textContent='Долговременная SQLite недоступна. Изменения не сохраняются.';
    this.render();
  }
  private render():void {
    this.tabs.replaceChildren(...[['continue','play','Продолжить'],['library','bookmark','Моя библиотека'],['search','search','Поиск'],['history','history','История'],['sources','layers','Источники']].map(([id,glyph,label])=>{const b=this.button(label,()=>{this.tab=id;this.render();});b.innerHTML=icon(glyph);b.append(el('span',label));b.setAttribute('aria-current',this.tab===id?'page':'false');return b;}));
    this.content.replaceChildren();
    if(!this.data){this.content.append(this.empty(this.online?'Загружаю твою кинотеку…':'Кинотека появится после подключения к Чушпану.'));return;}
    if(this.tab==='sources'){this.sources();return;}
    if(this.tab==='history'){this.history();return;}
    const matches=this.data.items.filter(item=>item.title.toLocaleLowerCase('ru').includes(this.query.toLocaleLowerCase('ru')));
    if(this.tab==='continue'){
      const section=el('section');section.append(this.sectionHeading('Продолжить просмотр'));
      const resumable=matches.filter(item=>item.current?.last_viewed&&item.status!=='watched').sort((a,b)=>(b.current?.last_viewed??'').localeCompare(a.current?.last_viewed??''));
      const cards=el('div','','cinema-continue-grid');cards.append(...resumable.slice(0,3).map(item=>this.card(item,true)));section.append(cards);
      if(!resumable.length)section.append(this.empty('Вечер начинается с хорошей истории. Добавь фильм или сериал — и Чушпан запомнит, где ты остановилась.','Добавить в библиотеку',()=>this.addForm()));
      this.content.append(section);
    }
    const library=el('section');library.append(this.sectionHeading(this.tab==='search'?'Поиск':'Моя библиотека',this.button('+ Добавить',()=>this.addForm())));
    if(this.tab!=='search'){
      const filters=el('div','','cinema-filters');for(const [id,label]of [['all','Все'],['watching','Смотрю'],['planned','Планирую'],['watched','Просмотрено'],['favorite','☆ Избранное']]){const count=matches.filter(item=>id==='all'||id==='favorite'&&item.favorite||item.status===id).length;const b=this.button(`${label}  ${count}`,()=>{this.status=id;this.render();});b.setAttribute('aria-pressed',String(this.status===id));filters.append(b);}const layout=this.button(this.list?'▦ Карточки':'☷ Список',()=>{this.list=!this.list;this.render();},'cinema-layout-toggle');filters.append(layout);library.append(filters);
    }
    const visible=matches.filter(item=>this.tab==='search'||this.status==='all'||this.status==='favorite'&&item.favorite||item.status===this.status);
    const grid=el('div','',this.list?'cinema-grid cinema-list':'cinema-grid');grid.append(...visible.map(item=>this.card(item)));library.append(grid);
    if(!visible.length)library.append(this.empty(this.query?'В личной библиотеке ничего не найдено.':'Здесь будут твои фильмы и сериалы. Без случайных рекомендаций и выдуманной истории.','Добавить фильм или сериал',()=>this.addForm()));
    this.content.append(library);
    if(this.tab==='search'){
      const sources=el('section','','cinema-source-search');sources.append(el('h2','Поиск в источниках'),el('p','Открывает поиск на выбранном сайте. Каталоги с официальными API можно подключить через адаптер.'));
      const usable=this.data.sources.filter(source=>source.enabled&&source.search_url);
      for(const source of usable){const b=this.button(`Искать в ${source.name} ↗`,()=>this.request({op:'source_search',source_id:source.id,query:this.query}));b.disabled=!this.online||!this.query.trim();sources.append(b);}
      if(!usable.length)sources.append(this.button('Настроить источники',()=>{this.tab='sources';this.render();}));
      for(const match of this.data.matches??[])sources.append(this.button(`${match.title} · ${match.catalogue}`,()=>this.addForm(undefined,match)));
      this.content.append(sources);
    }
    if(this.data.session){const session=this.data.session;const toolbar=el('div','','cinema-player-bar');toolbar.append(el('span',session.mode==='html5'?'Активный HTML5-плеер':'Ручной режим'),...['Поставь фильм на паузу','Продолжай','Включи следующую серию','Какая это серия?'].map(command=>this.button(command,()=>this.request({op:'control',command}))));this.content.append(toolbar);}
  }
  private sectionHeading(title:string,action?:HTMLElement):HTMLElement {const n=el('div','','cinema-section-heading');n.append(el('h2',title));if(action)n.append(action);return n;}
  private empty(message:string,label?:string,action?:()=>void):HTMLElement {const n=el('div','','cinema-empty');const symbol=el('span');symbol.innerHTML=icon('film');n.append(symbol,el('p',message));if(label&&action)n.append(this.button(label,action,'primary'));return n;}
  private poster(item:CinemaTitle):HTMLElement {const n=el('div','','cinema-poster');n.style.setProperty('--poster-hue',String([...item.title].reduce((sum,c)=>sum+c.charCodeAt(0),0)%360));if(item.poster_url){const image=el('img');image.src=item.poster_url;image.alt=`Постер: ${item.title}`;image.loading='lazy';image.referrerPolicy='no-referrer';image.onerror=()=>image.remove();n.append(image);}const placeholder=el('div','','cinema-poster-placeholder');const glyph=el('span');glyph.innerHTML=icon('film');placeholder.append(glyph,el('strong',item.title),el('small',item.kind==='series'?'СЕРИАЛ':'ФИЛЬМ'));n.append(placeholder);return n;}
  private progress(ep:CinemaEpisode|null):HTMLElement {const n=el('div','','cinema-progress');const p=el('progress');p.max=100;p.value=viewingProgress(ep);p.setAttribute('aria-label','Прогресс просмотра');n.append(p,el('span',`${Math.round(p.value)}%`));return n;}
  private card(item:CinemaTitle,wide=false):HTMLElement {
    const card=el('article','',wide?'cinema-card cinema-wide':'cinema-card');const poster=this.button(`Открыть ${item.title}`,()=>this.detail(item.id),'cinema-poster-button');poster.replaceChildren(this.poster(item));poster.setAttribute('aria-label',`Открыть ${item.title}`);
    const body=el('div','','cinema-card-body');const heading=el('div','','cinema-card-heading');heading.append(this.button(item.title,()=>this.detail(item.id),'cinema-title'),this.button(item.favorite?'★':'☆',()=>this.request({op:'update',id:item.id,values:{favorite:!item.favorite}}),'cinema-favorite'));heading.lastElementChild?.setAttribute('aria-label',item.favorite?'Убрать из избранного':'В избранное');body.append(heading,el('p',wide?episodeCaption(item):statuses[item.status]));if(!wide&&item.kind==='series')body.append(el('small',episodeCaption(item)));if(wide)body.append(this.progress(item.current),el('small',`${playbackTime(item.current?.position??0)} из ${item.current?.duration?playbackTime(item.current.duration):'—'}`));else body.append(this.progress(item.current));if(wide)body.append(this.button('▶ Продолжить',()=>this.open(item),'primary'));
    card.append(poster,body);return card;
  }
  private open(item:CinemaTitle,ep=item.current,linkId?:string,device?:string):void {this.request({op:'open',title_id:item.id,...(ep?{episode_id:ep.id}:{}),...(linkId?{link_id:linkId}:{}),...(device?{device}:{})});}
  private showDialog(title:string,form=false):HTMLDialogElement {this.closeDialog();const dialog=el('dialog','','cinema-dialog');dialog.dataset.form=String(form);const heading=el('header');heading.append(el('h2',title),this.button('×',()=>this.closeDialog(),'cinema-close'));heading.lastElementChild?.setAttribute('aria-label','Закрыть');dialog.append(heading);dialog.addEventListener('close',()=>{dialog.remove();if(this.dialog===dialog)this.dialog=null;});this.root.append(dialog);this.dialog=dialog;dialog.showModal();return dialog;}
  private closeDialog():void {this.dialog?.close();this.dialog?.remove();this.dialog=null;}
  private detail(id:string,episodeId?:string,linkId?:string):void {
    const item=this.data?.items.find(v=>v.id===id);if(!item)return;
    const ep=item.episodes.find(v=>v.id===episodeId)??item.current;if(!ep)return;
    const link=ep.links.find(v=>v.id===(linkId??ep.last_link_id))??ep.links[0];this.selectedLink=link?.id??null;
    const dialog=this.showDialog(item.title);const summary=el('div','','cinema-detail-summary');summary.append(this.poster(item),el('p',item.description||`${item.kind==='series'?'Сериал':'Фильм'}${item.year?` · ${item.year}`:''}. Прогресс сохраняется отдельно для каждой серии.`));dialog.append(summary);
    const choices=el('div','','cinema-detail-choices');const seasons=[...new Set(item.episodes.map(v=>v.season))];
    if(item.kind==='series'){
      const season=this.select(seasons.map(v=>[String(v),`Сезон ${v}`]),String(ep.season));season.onchange=()=>this.detail(id,item.episodes.find(v=>v.season===Number(season.value))?.id);
      const episode=this.select(item.episodes.filter(v=>v.season===ep.season).map(v=>[v.id,`Серия ${v.episode}${v.watched?' · ✓':''}`]),ep.id);episode.onchange=()=>this.detail(id,episode.value);choices.append(this.field('Сезон',season),this.field('Серия',episode));
    }
    const source=this.select([...new Map(ep.links.map(v=>[v.source_id,v.source_name])).entries()],link?.source_id??'');source.onchange=()=>this.detail(id,ep.id,ep.links.find(v=>v.source_id===source.value)?.id);
    const voice=this.select(ep.links.filter(v=>v.source_id===link?.source_id).map(v=>[v.id,v.voice||'Озвучка сайта']),link?.id??'');voice.onchange=()=>{this.selectedLink=voice.value;};choices.append(this.field('Источник',source),this.field('Озвучка',voice));dialog.append(choices);
    const status=this.select(Object.entries(statuses),item.status);status.onchange=()=>this.request({op:'update',id,values:{status:status.value}});dialog.append(this.field('Статус',status),this.progress(ep),el('p',`${episodeCaption(item,ep)} · ${playbackTime(ep.position)} из ${ep.duration?playbackTime(ep.duration):'—'}`));
    const devices=this.select((this.data?.devices??[]).filter(d=>d.id==='mac'||d.id==='airplay').map(d=>[d.id,d.name+(d.available?'':' · недоступно')]),this.data?.preferences.devices[this.data.location]??'mac');const location=this.data?.location??'general';
    devices.onchange=()=>{this.request({op:'preferences',values:{devices:{...this.data?.preferences.devices,[location]:devices.value}}});reason.textContent=this.data?.devices.find(d=>d.id===devices.value)?.reason??'';};const reason=el('p',this.data?.devices.find(d=>d.id===devices.value)?.reason??'','cinema-muted');dialog.append(this.field(`Устройство · ${{home:'Дом',dorm:'Общага',general:'Общий'}[location]??location}`,devices),reason);
    const actions=el('div','','cinema-actions');actions.append(this.button('▶ Продолжить просмотр',()=>this.open(item,ep,this.selectedLink??undefined,devices.value),'primary'),this.button('Следующая серия',()=>{const next=item.episodes[item.episodes.findIndex(v=>v.id===ep.id)+1];if(next){this.detail(item.id,next.id);this.open(item,next);}else this.feedback.textContent='Следующая серия ещё не добавлена. Укажи её страницу.';}),this.button('Добавить серию / источник',()=>this.addForm(item)));dialog.append(actions);
    const manual=el('details');manual.append(el('summary','Изменить прогресс вручную'));const form=el('form','','cinema-detail-choices');const position=this.input(playbackTime(ep.position));const duration=this.input(playbackTime(ep.duration));const watched=this.input('','checkbox');watched.checked=!!ep.watched;form.append(this.field('Позиция · мин:сек',position),this.field('Длительность · мин:сек',duration),this.field('Серия просмотрена',watched));const save=this.button('Сохранить прогресс',()=>{});save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();try{this.request({op:'progress',episode_id:ep.id,position:parsePlaybackTime(position.value),duration:parsePlaybackTime(duration.value),watched:watched.checked,link_id:this.selectedLink});this.closeDialog();}catch(error){const message=el('p',String((error as Error).message),'cinema-form-error');form.append(message);}};manual.append(form);dialog.append(manual);
    dialog.append(el('p','HTML5: доступное видео на странице или в iframe того же сайта. Чужие iframe, защита и запрет автоматизации могут потребовать ручного режима.','cinema-muted'));
  }
  private addForm(existing?:CinemaTitle,metadata?:{title:string;description?:string;poster_url?:string}):void {
    if(!this.data?.sources.length){this.tab='sources';this.render();this.sourceForm();return;}
    const dialog=this.showDialog(existing?'Добавить серию или другой источник':'Добавить в кинотеку',true);const form=el('form');
    const name=this.input(existing?.title??metadata?.title??'');name.required=true;name.maxLength=200;const kind=this.select([['series','Сериал'],['movie','Фильм']],existing?.kind??'series');const year=this.input(String(existing?.year||''),'number');year.min='0';year.max='2200';
    const source=this.select(this.data.sources.filter(v=>v.enabled).map(v=>[v.id,v.name]),this.data.sources.find(v=>v.enabled)?.id??'');source.required=true;
    const page=this.input('','url');page.required=true;page.placeholder='https://…/страница-серии';const season=this.input(String(existing?.current?.season||1),'number');season.min='1';season.max='999';const episode=this.input(String(existing?.current?.episode||1),'number');episode.min='1';episode.max='9999';
    const voice=this.input();voice.maxLength=100;const poster=this.input(existing?.poster_url??metadata?.poster_url??'','url');poster.placeholder='https://…/poster.jpg';const description=el('textarea');description.value=existing?.description??metadata?.description??'';description.maxLength=3000;
    form.append(this.field('Название',name),this.field('Тип',kind),this.field('Год · для различения ремейков',year),this.field('Источник',source),this.field('Точная страница фильма или серии',page));const episodes=el('div','','cinema-detail-choices');episodes.append(this.field('Сезон',season),this.field('Серия',episode));episodes.hidden=kind.value==='movie';kind.onchange=()=>{episodes.hidden=kind.value==='movie';};form.append(episodes,this.field('Озвучка · если есть',voice));
    const extra=el('details');extra.append(el('summary','Постер и описание'),this.field('Постер · необязательно',poster),this.field('Описание',description));form.append(extra,el('p','Для другой серии укажи её номер и точную страницу. Одинаковое название, тип и год объединяются в одну карточку.','cinema-muted'));const save=this.button('Сохранить в библиотеку',()=>{} ,'primary');save.type='submit';form.append(save);
    form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;this.request({op:'add',entry:{title:name.value,kind:kind.value,year:Number(year.value)||0,source_id:source.value,url:page.value,season:Number(season.value),episode:Number(episode.value),voice:voice.value,poster_url:poster.value,description:description.value}});};dialog.append(form);
  }
  private sources():void {
    this.content.append(this.sectionHeading('Твои источники',this.button('+ Добавить источник',()=>this.sourceForm())));
    const grid=el('div','','cinema-sources');for(const source of this.data?.sources??[]){const card=el('article','','cinema-source-card');card.append(el('h3',source.name),el('p',source.adapter==='html5'?'HTML5 · зависит от доступности плеера':'Ручное открытие и прогресс'),el('small',source.enabled?'Включён · реальный сайт не проверен':'Отключён'),this.button('Настроить',()=>this.sourceForm(source)));grid.append(card);}this.content.append(grid);
    if(!this.data?.sources.length)this.content.append(this.empty('Добавь сайт, которым уже пользуешься. Адреса и доступность сервисов задаются тобой.'));
    const examples=el('div','','cinema-actions');examples.append(el('span','Примеры пользовательских источников:'),this.button('+ Kinogo',()=>this.sourceForm(undefined,'Kinogo')),this.button('+ Filmix',()=>this.sourceForm(undefined,'Filmix')));this.content.append(examples,el('p','Эти примеры не означают поддержку конкретного домена или плеера. DRM, CAPTCHA и доступ к платному контенту не обходятся.','cinema-muted'));
    const settings=el('details','','cinema-settings');settings.append(el('summary','Поведение просмотра'));const form=el('form');const prefs=this.data!.preferences;const threshold=this.input(String(Math.round(prefs.watched_threshold*100)),'number');threshold.min='50';threshold.max='100';threshold.required=true;const auto=this.input('','checkbox');auto.checked=prefs.auto_next;const startup=this.select([['continue','Продолжить просмотр'],['library','Моя библиотека']],prefs.startup);form.append(this.field('Считать просмотренным после · %',threshold),this.field('Автоматически открывать следующую добавленную серию',auto),this.field('При открытии раздела',startup),el('p','После запуска приложения воспроизведение начинается только по твоей команде.','cinema-muted'));const save=this.button('Сохранить настройки',()=>{});save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(form.reportValidity())this.request({op:'preferences',values:{watched_threshold:Number(threshold.value)/100,auto_next:auto.checked,startup:startup.value}});};settings.append(form);this.content.append(settings);
  }
  private sourceForm(existing?:CinemaSource,preset=''):void {
    const dialog=this.showDialog(existing?'Настроить источник':'Новый источник',true);const form=el('form');const name=this.input(existing?.name??preset);name.required=true;name.maxLength=80;const base=this.input(existing?.base_url??'','url');base.required=true;base.placeholder='https://адрес-сайта';const search=this.input(existing?.search_url??'');search.placeholder='https://адрес-сайта/search?q={query}';const adapter=this.select([['manual','Ручной · открытие страницы'],['html5','HTML5 · управление через Safari']],existing?.adapter??'manual');const enabled=this.input('','checkbox');enabled.checked=existing?.enabled??true;
    form.append(this.field('Название',name),this.field('Адрес сайта',base),this.field('Адрес поиска · необязательно',search),this.field('Способ управления',adapter),this.field('Источник включён',enabled),el('p','Для HTML5 нужны разрешение Автоматизация → Safari и JavaScript из событий Apple в Safari. Чушпан не включает эти разрешения автоматически.','cinema-muted'));const save=this.button('Сохранить источник',()=>{} ,'primary');save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;this.request({op:'source',source:{id:existing?.id??'',name:name.value,base_url:base.value,search_url:search.value,adapter:adapter.value,enabled:enabled.checked}});};dialog.append(form);
  }
  private history():void {
    this.content.append(this.sectionHeading('История просмотра'));const toolbar=el('div','','cinema-filters');const source=this.select([['all','Все источники'],...(this.data?.sources.map(s=>[s.name,s.name] as [string,string])??[])],this.sourceFilter);source.setAttribute('aria-label','Источник в истории');source.onchange=()=>{this.sourceFilter=source.value;this.render();};const date=this.input(this.dateFilter,'date');date.setAttribute('aria-label','Дата просмотра');date.onchange=()=>{this.dateFilter=date.value;this.render();};toolbar.append(source,date);this.content.append(toolbar);
    const rows=this.data!.history.filter(row=>(this.sourceFilter==='all'||row.source_name===this.sourceFilter)&&row.title.toLocaleLowerCase('ru').includes(this.query.toLocaleLowerCase('ru'))&&(!this.dateFilter||new Date(row.created_at).toLocaleDateString('sv-SE')===this.dateFilter));
    if(!rows.length)this.content.append(this.empty('Истории по выбранным фильтрам пока нет.'));
    const list=el('div','','cinema-history');for(const row of rows){const card=el('article');const info=el('div');info.append(this.button(row.title,()=>this.detail(row.title_id,row.episode_id),'cinema-title'),el('p',`${row.season?`Сезон ${row.season} · Серия ${row.episode} · `:''}${row.source_name} · ${events[row.event]??row.event}`));card.append(info,el('span',`${playbackTime(row.position)} / ${row.duration?playbackTime(row.duration):'—'}`),el('time',new Date(row.created_at).toLocaleString('ru-RU',{dateStyle:'short',timeStyle:'short'})));list.append(card);}this.content.append(list);
  }
}

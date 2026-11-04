import type {HudEvent} from './types';
import {icon} from './dashboard';
import {CinemaCatalogueView} from './cinema_catalogue';
import type {CataloguePage,CatalogueConfig} from './cinema_catalogue';

export interface CinemaSource {id:string;name:string;base_url:string;search_url:string;adapter:'manual'|'html5'|'local';category?:'site'|'streaming'|'local';enabled:boolean}
export interface CinemaLink {id:string;episode_id:string;source_id:string;source_name:string;url:string;voice:string}
export interface CinemaEpisode {id:string;title_id:string;season:number;episode:number;position:number;duration:number;watched:number;last_viewed:string|null;last_link_id:string|null;name?:string;links:CinemaLink[]}
export interface CinemaMetadata {genres:string[];runtime:number;rating:number;seasons:{season:number;episodes:number;name:string}[];episode_count:number;original_title?:string;original_language?:string;countries?:string[];release_date?:string;popularity?:number}
export interface CatalogueTitle {title:string;kind:'movie'|'series';year:number;poster_url:string;description:string;catalogue_key:string;metadata:CinemaMetadata}
interface SourceOptions {title_id:string;episode_id:string;options:{source:CinemaSource;link:CinemaLink|null;available:boolean;reason:string}[];official?:{region:string;url:string;providers:{name:string;type:string}[]};provider_error?:string}
export interface CinemaTitle {id:string;title:string;kind:'movie'|'series';year:number;poster_url:string;description:string;status:'watching'|'planned'|'watched';favorite:number;catalogue_key?:string;metadata?:CinemaMetadata;user_rating?:number|null;completed_at?:string|null;added_at?:string;favorite_added_at?:string|null;episodes:CinemaEpisode[];current:CinemaEpisode|null}
interface CinemaHistory {id:number;episode_id:string;title:string;title_id:string;season:number;episode:number;position:number;duration:number;created_at:string;event:string;source_name:string;watched:number}
export interface CinemaEvent {type:'cinema';items:CinemaTitle[];sources:CinemaSource[];history:CinemaHistory[];preferences:{watched_threshold:number;auto_next:boolean;startup:'continue'|'library';poll_seconds:number;watch_region?:string;save_progress?:boolean;recommendations_show_watched?:boolean;devices:Record<string,'mac'|'airplay'>};location:string;devices:{id:string;name:string;available:boolean;reason:string}[];durable:boolean;session:{title_id:string;episode_id:string;mode:string}|null;message:string;error?:string;saved?:string;navigate?:boolean;matches?:CatalogueTitle[];tmdb?:{configured:boolean};catalogue_detail?:CatalogueTitle;source_options?:SourceOptions;active_tab?:string;season_loaded?:string;loaded_season?:number;catalogue_page?:CataloguePage;catalogue_config?:CatalogueConfig;catalogue_config_error?:string;catalogue_intent?:{query?:string;genre?:string}}

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
const statuses={watching:'Смотрю',planned:'Хочу посмотреть',watched:'Просмотрено'};
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
  private list=false;
  private libraryKind='all';
  private librarySort='recent';
  private ratingMin=0;
  private ratingMax=10;
  private libraryQuery='';
  private scrollPositions=new Map<string,number>();
  private renderedTab='';
  private catalogueView:CinemaCatalogueView;
  private pendingWatch:string|null=null;
  private dialog:HTMLDialogElement|null=null;
  private selectedLink:string|null=null;
  private search:HTMLInputElement;
  private sourceFilter='all';
  private dateFilter='';
  private first=true;
  constructor(private send:(value:object)=>void,private chat:(text:string)=>void,private navigate:()=>void,private listen:()=>void) {
    const heading=el('header','','cinema-heading');const title=el('h1');title.innerHTML=icon('film');title.append(el('span','Фильмы и сериалы'));
    const searchForm=el('form','','cinema-search');const glyph=el('span');glyph.innerHTML=icon('search');this.search=el('input');this.search.type='search';this.search.placeholder='Поиск фильмов и сериалов…';this.search.setAttribute('aria-label','Поиск по кинотеке');this.search.maxLength=200;
    this.search.oninput=()=>{this.query=this.search.value;this.changeTab('catalogue',false);this.catalogueView.search(this.query);this.render();};searchForm.onsubmit=e=>{e.preventDefault();this.changeTab('catalogue',false);this.catalogueView.search(this.query,false);this.render();};searchForm.append(glyph,this.search);
    const mic=this.button('Голосовая команда',this.listen,'cinema-mic');mic.innerHTML=icon('mic');mic.setAttribute('aria-label','Голосовая команда');heading.append(title,searchForm,mic,this.button('Настройки',()=>{this.changeTab('sources');}));
    this.tabs.setAttribute('aria-label','Разделы кинотеки');this.feedback.setAttribute('role','status');this.feedback.setAttribute('aria-live','polite');
    const composer=el('form','','cinema-composer');const spark=el('span');spark.innerHTML=icon('activity');const input=el('input');input.placeholder='Скажи, что посмотреть…';input.setAttribute('aria-label','Команда Чушпану о кино');input.maxLength=400;
    const submit=this.button('Отправить',()=>{});submit.type='submit';submit.innerHTML=icon('send');submit.setAttribute('aria-label','Отправить команду о кино');composer.append(spark,input,submit);composer.onsubmit=e=>{e.preventDefault();if(this.online&&input.value.trim()){this.chat(input.value.trim());input.value='';}};
    this.catalogueView=new CinemaCatalogueView({send:value=>this.request(value),poster:item=>this.poster(item),
      detail:item=>{const existing=this.data?.items.find(v=>v.catalogue_key===item.catalogue_key);if(existing)this.detail(existing.id);else this.request({op:'catalogue_detail',key:item.catalogue_key});},
      watch:item=>{const existing=this.data?.items.find(v=>v.catalogue_key===item.catalogue_key);if(existing)this.open(existing);else{this.pendingWatch=item.catalogue_key;this.request({op:'catalogue_add',key:item.catalogue_key});}},
      change:(item,values)=>{const existing=this.data?.items.find(v=>v.catalogue_key===item.catalogue_key);if(existing)this.request({op:'update',id:existing.id,values});else this.request({op:'catalogue_add',key:item.catalogue_key,values});},
      remove:item=>this.deleteTitle(item),query:value=>{this.query=value;this.search.value=value;},settings:()=>{this.changeTab('sources');}});
    this.root.append(heading,this.tabs,this.feedback,this.content,composer);
    document.addEventListener('cinema:settings',()=>{this.changeTab('sources');});
    document.addEventListener('workspace:navigate',event=>{if((event as CustomEvent).detail==='cinema'&&this.online)this.request({op:'refresh'});});
    this.connection(false);
  }
  private button(label:string,action:()=>void,cls=''):HTMLButtonElement {const b=el('button',label,cls);b.type='button';b.disabled=!this.online;b.onclick=action;return b;}
  private field(label:string,input:HTMLElement):HTMLLabelElement {input.setAttribute('aria-label',label);const node=el('label','','cinema-field');node.append(el('span',label),input);return node;}
  private input(value='',type='text'):HTMLInputElement {const n=el('input');n.type=type;n.value=value;return n;}
  private select(options:[string,string][],value:string):HTMLSelectElement {const node=el('select');for(const [id,label]of options){const option=el('option',label);option.value=id;node.append(option);}node.value=value;return node;}
  private request(value:object):void {if(this.online)this.send(value);}
  connection(online:boolean):void {this.online=online;this.catalogueView.connection(online);if(!online)this.feedback.textContent='Нет соединения с Чушпаном. Последние данные могут устареть.';else this.request({op:'refresh'});this.root.querySelectorAll<HTMLInputElement|HTMLSelectElement|HTMLButtonElement>('input,select,button').forEach(n=>n.disabled=!online);this.render();}
  receive(event:HudEvent):void {
    if(event.type!=='cinema')return;
    this.data=event;this.catalogueView.receive(event);
    if(this.first){this.tab=event.preferences.startup==='library'?'planned':'watching';this.first=false;}
    this.feedback.textContent=event.error??event.message??'';this.feedback.dataset.error=String(!!event.error);
    if(event.error)this.pendingWatch=null;
    if(event.error&&this.dialog){let error=this.dialog.querySelector<HTMLElement>('.cinema-form-error');if(!error){error=el('p','','cinema-form-error');error.setAttribute('role','alert');this.dialog.append(error);}error.textContent=event.error;}
    if(event.saved&&this.dialog?.dataset.form==='true')this.closeDialog();
    if(event.navigate){this.tab=event.active_tab??'watching';this.navigate();if(event.active_tab==='catalogue'){if(event.catalogue_intent)this.catalogueView.intent(event.catalogue_intent);else if(event.matches?.length)this.catalogueView.legacy(event.matches);}}
    if(!event.durable)this.feedback.textContent='Долговременная SQLite недоступна. Изменения не сохраняются.';
    this.render();
    if(event.catalogue_detail)this.catalogueDetail(event.catalogue_detail);
    if(event.source_options)this.sourceChoices(event.source_options);
    if(event.season_loaded)this.detail(event.season_loaded,event.items.find(i=>i.id===event.season_loaded)?.episodes.find(ep=>ep.season===event.loaded_season)?.id);
    if(event.saved&&this.pendingWatch){const item=event.items.find(v=>v.id===event.saved);if(item){this.pendingWatch=null;this.open(item);}}
  }
  private changeTab(tab:string,render=true):void {
    if(this.tab!==tab){const scroller=this.root.closest<HTMLElement>('.workspace-page');if(scroller)this.scrollPositions.set(this.tab,scroller.scrollTop);this.tab=tab;}
    if(render)this.render();
  }
  private render():void {
    this.tabs.replaceChildren(...[['catalogue','search','Каталог'],['planned','bookmark','Хочу посмотреть'],['watching','play','Смотрю'],['watched','history','Просмотрено'],['favorites','heart','Избранное']].map(([id,glyph,label])=>{
      const b=this.button(label,()=>this.changeTab(id));b.innerHTML=icon(glyph);b.append(el('span',label));b.setAttribute('aria-current',this.tab===id?'page':'false');return b;
    }));
    const scroller=this.root.closest<HTMLElement>('.workspace-page');
    const top=this.renderedTab===this.tab?scroller?.scrollTop??0:this.scrollPositions.get(this.tab)??0;
    this.renderedTab=this.tab;
    if(!this.data){this.content.replaceChildren(this.empty(this.online?'Загружаю твою кинотеку…':'Кинотека появится после подключения к Чушпану.'));return;}
    if(this.tab==='catalogue'){this.catalogue();}
    else if(this.tab==='sources'){this.content.replaceChildren();this.sources();}
    else this.renderLibrary();
    if(scroller)scroller.scrollTop=top;
    this.syncDetail();
  }
  private renderLibrary():void {
    if(!this.data)return;
    const visible=this.data.items.filter(item=>(this.tab==='favorites'?!!item.favorite:item.status===this.tab)
      &&(this.libraryKind==='all'||item.kind===this.libraryKind)
      &&item.title.toLocaleLowerCase('ru').includes(this.libraryQuery.toLocaleLowerCase('ru'))
      &&(this.tab!=='watched'||(!this.ratingMin&&this.ratingMax===10)||
        (item.user_rating!=null&&item.user_rating>=this.ratingMin&&item.user_rating<=this.ratingMax)));
    visible.sort((a,b)=>this.librarySort==='rating'?(b.user_rating??-1)-(a.user_rating??-1)||a.title.localeCompare(b.title,'ru'):
      this.librarySort==='title'?a.title.localeCompare(b.title,'ru'):
      (this.tab==='favorites'?b.favorite_added_at??b.added_at??'':this.tab==='watching'?b.current?.last_viewed??'':b.added_at??'')
        .localeCompare(this.tab==='favorites'?a.favorite_added_at??a.added_at??'':this.tab==='watching'?a.current?.last_viewed??'':a.added_at??'')||a.title.localeCompare(b.title,'ru'));
    const section=el('section','','cinema-library');
    const toolbar=el('div','','cinema-library-toolbar');const types=el('div','','cinema-type-switch');
    for(const [id,label] of [['all','Всё'],['movie','Фильмы'],['series','Сериалы']]){const b=this.button(label,()=>{this.libraryKind=id;this.render();});b.setAttribute('aria-pressed',String(this.libraryKind===id));types.append(b);}
    const sort=this.select([['recent',this.tab==='watching'?'По последнему просмотру':'По дате добавления'],['rating','По моей оценке'],['title','По названию']],this.librarySort);sort.onchange=()=>{this.librarySort=sort.value;this.render();};
    const layout=this.button(this.list?'Карточки':'Список',()=>{this.list=!this.list;this.render();});
    toolbar.append(types,this.field('Сортировка медиатеки',sort),layout,this.button('+ Добавить',()=>this.addForm()));section.append(toolbar);
    const filters=el('details','','cinema-display');filters.open=this.content.querySelector<HTMLDetailsElement>('.cinema-display')?.open??false;filters.append(el('summary','Поиск и фильтры'));
    const search=this.input(this.libraryQuery,'search');search.placeholder='Название в этом списке';search.onchange=()=>{this.libraryQuery=search.value;this.render();};filters.append(this.field('Поиск в списке',search));
    if(this.tab==='watched'){
      const options=[['0','Любая'],...Array.from({length:19},(_,i)=>[String(1+i*.5),String(1+i*.5)])] as [string,string][];
      const min=this.select(options,String(this.ratingMin));const max=this.select(options.slice(1),String(this.ratingMax));
      min.onchange=()=>{this.ratingMin=Number(min.value);this.ratingMax=Math.max(this.ratingMin,this.ratingMax);this.render();};max.onchange=()=>{this.ratingMax=Number(max.value);this.ratingMin=Math.min(this.ratingMin,this.ratingMax);this.render();};
      const range=el('div','','cinema-rating-range');range.append(this.field('Моя оценка от',min),this.field('Моя оценка до',max));filters.append(range);
      if(this.ratingMin||this.ratingMax!==10)filters.append(this.button('Сбросить оценки',()=>{this.ratingMin=0;this.ratingMax=10;this.render();}));
    }
    section.append(filters);
    const grid=el('div','',this.list?'cinema-grid cinema-list':this.tab==='watching'?'cinema-continue-grid':'cinema-grid');grid.append(...visible.map(item=>this.card(item,this.tab==='watching'&&!this.list)));section.append(grid);
    if(!visible.length)section.append(this.empty(this.libraryQuery||this.ratingMin||this.ratingMax!==10?'По выбранным фильтрам ничего не найдено.':this.tab==='watching'?'Вечер начинается с хорошей истории. Начни просмотр — и Чушпан запомнит, где ты остановилась.':this.tab==='planned'?'Сохраняй сюда фильмы и сериалы на будущее. Источник можно привязать позже.':this.tab==='favorites'?'Любимые истории всегда рядом. Нажми сердечко на карточке, чтобы сохранить их здесь.':'Здесь появятся завершённые просмотры и твои оценки.','Найти в каталоге',()=>this.changeTab('catalogue')));
    this.content.replaceChildren(section);
    if(this.tab==='watched'){const history=el('details');history.append(el('summary','Журнал просмотра'));const holder=el('div');this.content.append(history);this.history(holder);history.append(holder);}
    if(this.data.session){const session=this.data.session;const toolbar=el('div','','cinema-player-bar');toolbar.append(el('span',session.mode==='html5'?'Активный HTML5-плеер':'Ручной режим · сохрани прогресс в карточке'),...['Поставь фильм на паузу','Продолжай','Включи следующую серию','Какая это серия?'].map(command=>this.button(command,()=>this.request({op:'control',command}))));this.content.append(toolbar);}
  }
  private syncDetail():void {
    const item=this.data?.items.find(i=>(!!this.dialog?.dataset.titleId&&i.id===this.dialog.dataset.titleId)||(!!this.dialog?.dataset.catalogueKey&&i.catalogue_key===this.dialog.dataset.catalogueKey));
    if(!item||!this.dialog)return;
    const rating=this.dialog.querySelector<HTMLSelectElement>('[data-personal-rating]');if(rating)rating.value=String(item.user_rating??'');
    const status=this.dialog.querySelector<HTMLSelectElement>('[data-status]');if(status)status.value=item.status;
    const personal=this.dialog.querySelector('.cinema-personal-rating');if(personal)personal.textContent=`Моя оценка: ${item.user_rating??'—'}/10`;
    const heart=this.dialog.querySelector<HTMLButtonElement>('[data-detail-favorite]');if(heart){heart.textContent=item.favorite?'♥ В избранном':'♡ В избранное';heart.setAttribute('aria-pressed',String(!!item.favorite));}
  }
  private detailFavorite(item:CinemaTitle|CatalogueTitle):HTMLButtonElement {
    const current=()=>this.data?.items.find(i=>'id' in item?i.id===item.id:i.catalogue_key===item.catalogue_key);
    const b=this.button('♡ В избранное',()=>{const existing=current();this.request(existing?{op:'update',id:existing.id,values:{favorite:!existing.favorite}}:{op:'catalogue_add',key:item.catalogue_key,values:{favorite:true}});},'cinema-detail-favorite');b.dataset.detailFavorite='';b.setAttribute('aria-pressed',String(!!current()?.favorite));b.textContent=current()?.favorite?'♥ В избранном':'♡ В избранное';return b;
  }
  private metadata(item:{year:number;kind:string;metadata?:CinemaMetadata}):string {const m=item.metadata;return [item.kind==='series'?'Сериал':'Фильм',item.year||'',m?.genres.join(', '),m?.runtime?`${m.runtime} мин`:'',m?.rating?`★ ${m.rating.toFixed(1)}`:''].filter(Boolean).join(' · ');}
  private catalogue():void {this.catalogueView.mount(this.content);}
  private catalogueDetail(item:CatalogueTitle):void {
    const dialog=this.showDialog(item.title);dialog.dataset.catalogueKey=item.catalogue_key;
    const summary=el('div','','cinema-detail-summary');summary.append(this.poster(item),el('p',item.description||'Описание пока отсутствует.'));dialog.append(summary,el('p',this.metadata(item),'cinema-muted'));
    const ratings=el('div','','cinema-ratings');ratings.append(el('span',`TMDB: ${item.metadata.rating.toFixed(1)}/10`,'cinema-tmdb-rating'),el('span','Моя оценка: —/10','cinema-personal-rating'));dialog.append(ratings,this.detailFavorite(item));
    if(item.kind==='series')dialog.append(el('p',`${item.metadata.seasons.length} сезонов · ${item.metadata.episode_count} серий`),...item.metadata.seasons.map(s=>el('p',`${s.name||`Сезон ${s.season}`} · ${s.episodes} серий`)));
    dialog.append(this.button('Хочу посмотреть',()=>{this.request({op:'catalogue_add',key:item.catalogue_key});this.closeDialog();},'primary'));
  }
  private sourceChoices(choices:SourceOptions):void {
    const item=this.data?.items.find(v=>v.id===choices.title_id);if(!item)return;const ep=item.episodes.find(v=>v.id===choices.episode_id);if(!ep)return;
    const dialog=this.showDialog(`Смотреть · ${item.title}`);dialog.append(el('p',`${episodeCaption(item,ep)} · ${playbackTime(ep.position)}`,'cinema-muted'));
    if(!choices.options.length)dialog.append(el('p','Источники пока не подключены. Можно привязать страницу официального сервиса или локальный файл.'));
    for(const option of choices.options){const row=el('article','','cinema-source-option');row.append(el('strong',option.source.name+(option.link?.id===ep.last_link_id?' · последний источник':'')+(option.link?.voice?` · ${option.link.voice}`:'')),el('p',option.reason|| (option.source.adapter==='html5'?'Попытаюсь восстановить позицию в доступном HTML5-плеере. Доступ к видео проверяется при запуске.':'Открытие вручную. Автоматический прогресс недоступен; сохрани позицию в карточке.')));if(!option.link&&option.source.enabled&&option.source.search_url)row.append(this.button('Найти на сайте ↗',()=>this.request({op:'source_search',source_id:option.source.id,query:item.title})));if(option.available&&option.link)row.append(this.button(option.source.adapter==='html5'?'Смотреть':'Открыть вручную',()=>{this.open(item,ep,option.link!.id);this.closeDialog();},'primary'));else row.append(this.button(option.source.enabled?'Привязать источник':'Настроить источник',()=>{if(option.source.enabled)this.addForm(item,undefined,ep,option.source.id);else this.sourceForm(option.source);}));dialog.append(row);}
    if(choices.provider_error)dialog.append(el('p',choices.provider_error,'cinema-muted'));
    if(choices.official){const official=choices.official;dialog.append(el('h3',`Официальные предложения · ${official.region}`),el('p',official.providers.map(p=>`${p.name} · ${{flatrate:'подписка',free:'бесплатно',ads:'с рекламой',rent:'аренда',buy:'покупка'}[p.type]??p.type}`).join(', ')||'В этом регионе предложения не найдены.'),el('p','Данные JustWatch через TMDB. Подписка, доступность и запуск видео проверяются в сервисе. Сохрани точную страницу сервиса один раз.','cinema-muted'));if(official.url)dialog.append(this.button('Открыть предложения сервисов ↗',()=>this.request({op:'provider_page',title_id:item.id})));}
    dialog.append(this.button('+ Привязать страницу или файл',()=>this.addForm(item,undefined,ep)),this.button('Настроить источники',()=>{this.closeDialog();this.changeTab('sources');}));
  }
  private sectionHeading(title:string,action?:HTMLElement):HTMLElement {const n=el('div','','cinema-section-heading');n.append(el('h2',title));if(action)n.append(action);return n;}
  private empty(message:string,label?:string,action?:()=>void):HTMLElement {const n=el('div','','cinema-empty');const symbol=el('span');symbol.innerHTML=icon('film');n.append(symbol,el('p',message));if(label&&action)n.append(this.button(label,action,'primary'));return n;}
  private poster(item:{title:string;poster_url:string;kind:string}):HTMLElement {const n=el('div','','cinema-poster');n.style.setProperty('--poster-hue',String([...item.title].reduce((sum,c)=>sum+c.charCodeAt(0),0)%360));if(item.poster_url){const image=el('img');image.src=item.poster_url;image.alt=`Постер: ${item.title}`;image.loading='lazy';image.referrerPolicy='no-referrer';image.onerror=()=>image.remove();n.append(image);}const placeholder=el('div','','cinema-poster-placeholder');const glyph=el('span');glyph.innerHTML=icon('film');placeholder.append(glyph,el('strong',item.title),el('small',item.kind==='series'?'СЕРИАЛ':'ФИЛЬМ'));n.append(placeholder);return n;}
  private progress(ep:CinemaEpisode|null):HTMLElement {const n=el('div','','cinema-progress');const p=el('progress');p.max=100;p.value=viewingProgress(ep);p.setAttribute('aria-label','Прогресс просмотра');n.append(p,el('span',`${Math.round(p.value)}%`));return n;}
  private card(item:CinemaTitle,wide=false):HTMLElement {
    const card=el('article','',wide?'cinema-card cinema-wide':'cinema-card');card.dataset.titleId=item.id;
    const poster=this.button(`Открыть ${item.title}`,()=>this.detail(item.id),'cinema-poster-button');poster.replaceChildren(this.poster(item));poster.setAttribute('aria-label',`Открыть ${item.title}`);
    const heart=this.button(item.favorite?'♥':'♡',()=>this.request({op:'update',id:item.id,values:{favorite:!item.favorite}}),'cinema-favorite');heart.setAttribute('aria-label',item.favorite?'Убрать из избранного':'В избранное');heart.setAttribute('aria-pressed',String(!!item.favorite));
    const body=el('div','','cinema-card-body');body.append(this.button(item.title,()=>this.detail(item.id),'cinema-title'),el('p',`${item.year||'Год неизвестен'} · ${item.kind==='series'?'Сериал':'Фильм'}`,'cinema-card-year'));
    const ratings=el('div','','cinema-ratings');ratings.append(el('span',item.metadata?.rating?`★ ${item.metadata.rating.toFixed(1)}`:'TMDB —','cinema-tmdb-rating'));
    if(item.user_rating)ratings.append(el('span',`★ ${item.user_rating.toFixed(1)}`,'cinema-personal-rating'));body.append(ratings,el('small',statuses[item.status],'cinema-library-badge'));
    if(wide||item.kind==='series')body.append(el('small',episodeCaption(item)));
    if(wide||item.status==='watching'){body.append(this.progress(item.current),el('small',`${playbackTime(item.current?.position??0)} из ${item.current?.duration?playbackTime(item.current.duration):'—'}`));}
    const actions=el('div','','cinema-card-actions');actions.append(this.button(item.status==='watching'?'▶ Продолжить':item.status==='watched'?'Пересмотреть':'Смотреть',()=>{if(item.status==='watched'){this.request({op:'replay',id:item.id});this.pendingWatch=item.id;}else this.open(item);},'primary'),this.button('Хочу посмотреть',()=>this.request({op:'update',id:item.id,values:{status:'planned'}})));
    const menu=el('details','','cinema-context');menu.append(el('summary','•••'));for(const [status,label]of Object.entries(statuses))menu.append(this.button(label,()=>this.request({op:'update',id:item.id,values:{status}})));menu.append(this.button(item.favorite?'Убрать из избранного':'В избранное',()=>this.request({op:'update',id:item.id,values:{favorite:!item.favorite}})),this.button('Удалить из медиатеки',()=>this.deleteTitle(item)));actions.append(menu);body.append(actions);card.append(poster,heart,body);return card;
  }
  private deleteTitle(item:CinemaTitle):void {const dialog=this.showDialog('Удалить из медиатеки?');dialog.append(el('p',`«${item.title}»: будут удалены прогресс и привязки этого названия.`),this.button('Удалить',()=>{this.request({op:'delete',id:item.id});this.closeDialog();}),this.button('Отмена',()=>this.closeDialog()));}
  private open(item:CinemaTitle,ep=item.current,linkId?:string,device?:string):void {this.request({op:linkId||device==='airplay'?'open':'options',title_id:item.id,...(ep?{episode_id:ep.id}:{}),...(linkId?{link_id:linkId}:{}),...(device?{device}:{})});}
  private showDialog(title:string,form=false):HTMLDialogElement {this.closeDialog();const dialog=el('dialog','','cinema-dialog');dialog.dataset.form=String(form);const heading=el('header');heading.append(el('h2',title),this.button('×',()=>this.closeDialog(),'cinema-close'));heading.lastElementChild?.setAttribute('aria-label','Закрыть');dialog.append(heading);dialog.addEventListener('close',()=>{dialog.remove();if(this.dialog===dialog)this.dialog=null;});this.root.append(dialog);this.dialog=dialog;dialog.showModal();return dialog;}
  private closeDialog():void {this.dialog?.close();this.dialog?.remove();this.dialog=null;}
  private detail(id:string,episodeId?:string,linkId?:string):void {
    const item=this.data?.items.find(v=>v.id===id);if(!item)return;
    const ep=item.episodes.find(v=>v.id===episodeId)??item.current;if(!ep)return;this.request({op:'focus',id});
    const link=ep.links.find(v=>v.id===(linkId??ep.last_link_id))??ep.links[0];this.selectedLink=link?.id??null;
    const dialog=this.showDialog(item.title);dialog.dataset.titleId=id;const summary=el('div','','cinema-detail-summary');summary.append(this.poster(item),el('p',item.description||`${item.kind==='series'?'Сериал':'Фильм'}${item.year?` · ${item.year}`:''}. Прогресс сохраняется отдельно для каждой серии.`));dialog.append(summary,el('p',this.metadata(item),'cinema-muted'));const ratings=el('div','','cinema-ratings');ratings.append(el('span',`TMDB: ${item.metadata?.rating?.toFixed(1)??'—'}/10`,'cinema-tmdb-rating'),el('span',`Моя оценка: ${item.user_rating??'—'}/10`,'cinema-personal-rating'));dialog.append(ratings,this.detailFavorite(item));const rating=this.select([['','Без оценки'],...Array.from({length:19},(_,i)=>[String(1+i*.5),`${1+i*.5}/10`] as [string,string])],String(item.user_rating??''));rating.dataset.personalRating='';rating.onchange=()=>this.request({op:'update',id,values:{user_rating:rating.value?Number(rating.value):null}});dialog.append(this.field('Моя оценка',rating));
    const choices=el('div','','cinema-detail-choices');const seasons=[...new Set([...item.episodes.map(v=>v.season),...(item.metadata?.seasons.map(v=>v.season)??[])])].sort((a,b)=>a-b);
    if(item.kind==='series'){
      const season=this.select(seasons.map(v=>[String(v),`Сезон ${v}`]),String(ep.season));season.onchange=()=>{const loaded=item.episodes.find(v=>v.season===Number(season.value));if(loaded)this.detail(id,loaded.id);else this.request({op:'season',id,season:Number(season.value)});};
      const episode=this.select(item.episodes.filter(v=>v.season===ep.season).map(v=>[v.id,`Серия ${v.episode}${v.name?` · ${v.name}`:''}${v.watched?' · ✓':''}`]),ep.id);episode.onchange=()=>this.detail(id,episode.value);choices.append(this.field('Сезон',season),this.field('Серия',episode));if(item.catalogue_key?.startsWith('tmdb:'))choices.append(this.button('Загрузить серии сезона',()=>this.request({op:'season',id,season:Number(season.value)})));
    }
    const source=this.select([...new Map(ep.links.map(v=>[v.source_id,v.source_name])).entries()],link?.source_id??'');source.onchange=()=>this.detail(id,ep.id,ep.links.find(v=>v.source_id===source.value)?.id);
    const voice=this.select(ep.links.filter(v=>v.source_id===link?.source_id).map(v=>[v.id,v.voice||'Озвучка сайта']),link?.id??'');voice.onchange=()=>{this.selectedLink=voice.value;};choices.append(this.field('Источник',source),this.field('Озвучка',voice));dialog.append(choices);
    const status=this.select(Object.entries(statuses),item.status);status.dataset.status='';status.onchange=()=>this.request({op:'update',id,values:{status:status.value}});dialog.append(this.field('Статус',status),this.progress(ep),el('p',`${episodeCaption(item,ep)} · ${playbackTime(ep.position)} из ${ep.duration?playbackTime(ep.duration):'—'}`));
    const devices=this.select((this.data?.devices??[]).filter(d=>d.id==='mac'||d.id==='airplay').map(d=>[d.id,d.name+(d.available?'':' · недоступно')]),this.data?.preferences.devices[this.data.location]??'mac');const location=this.data?.location??'general';
    devices.onchange=()=>{this.request({op:'preferences',values:{devices:{...this.data?.preferences.devices,[location]:devices.value}}});reason.textContent=this.data?.devices.find(d=>d.id===devices.value)?.reason??'';};const reason=el('p',this.data?.devices.find(d=>d.id===devices.value)?.reason??'','cinema-muted');dialog.append(this.field(`Устройство · ${{home:'Дом',dorm:'Общага',general:'Общий'}[location]??location}`,devices),reason);
    const actions=el('div','','cinema-actions');actions.append(this.button('▶ Продолжить просмотр',()=>this.open(item,ep,undefined,devices.value),'primary'),this.button('Следующая серия',()=>{const next=item.episodes[item.episodes.findIndex(v=>v.id===ep.id)+1];if(next){this.detail(item.id,next.id);this.open(item,next);}else this.feedback.textContent='Следующая серия ещё не добавлена. Укажи её страницу.';}),this.button('Добавить серию / источник',()=>this.addForm(item,undefined,ep)));dialog.append(actions);
    const manual=el('details');manual.append(el('summary','Изменить прогресс вручную'));const form=el('form','','cinema-detail-choices');const position=this.input(playbackTime(ep.position));const duration=this.input(playbackTime(ep.duration));const watched=this.input('','checkbox');watched.checked=!!ep.watched;form.append(this.field('Позиция · мин:сек',position),this.field('Длительность · мин:сек',duration),this.field('Серия просмотрена',watched));const save=this.button('Сохранить прогресс',()=>{});save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();try{dialog.dataset.form='true';this.request({op:'progress',episode_id:ep.id,position:parsePlaybackTime(position.value),duration:parsePlaybackTime(duration.value),watched:watched.checked,link_id:this.selectedLink});}catch(error){const message=el('p',String((error as Error).message),'cinema-form-error');form.append(message);}};manual.append(form);dialog.append(manual);
    dialog.append(el('p','HTML5: доступное видео на странице или в iframe того же сайта. Чужие iframe, защита и запрет автоматизации могут потребовать ручного режима.','cinema-muted'));
  }
  private addForm(existing?:CinemaTitle,metadata?:{title:string;description?:string;poster_url?:string},selectedEpisode?:CinemaEpisode,sourceId?:string):void {
    if(!this.data)return;
    const dialog=this.showDialog(existing?'Добавить серию или другой источник':'Добавить в кинотеку',true);const form=el('form');
    const name=this.input(existing?.title??metadata?.title??'');name.required=true;name.maxLength=200;const kind=this.select([['series','Сериал'],['movie','Фильм']],existing?.kind??'series');const year=this.input(String(existing?.year||''),'number');year.min='0';year.max='2200';
    const source=this.select([['','Позже · сохранить без источника'],...this.data.sources.filter(v=>v.enabled).map(v=>[v.id,v.name] as [string,string])],sourceId??'');
    const page=this.input();const updatePage=()=>{const local=this.data?.sources.find(v=>v.id===source.value)?.adapter==='local';page.type=local?'text':'url';page.placeholder=local?'/Users/…/film.mp4':'https://…/страница-серии';page.required=!!source.value;};source.onchange=updatePage;updatePage();const season=this.input(String(Math.max(1,selectedEpisode?.season??existing?.current?.season??1)),'number');season.min='1';season.max='999';const episode=this.input(String(Math.max(1,selectedEpisode?.episode??existing?.current?.episode??1)),'number');episode.min='1';episode.max='9999';
    const voice=this.input();voice.maxLength=100;const poster=this.input(existing?.poster_url??metadata?.poster_url??'','url');poster.placeholder='https://…/poster.jpg';const description=el('textarea');description.value=existing?.description??metadata?.description??'';description.maxLength=3000;
    form.append(this.field('Название',name),this.field('Тип',kind),this.field('Год · для различения ремейков',year),this.field('Источник',source),this.field('Точная страница фильма или серии',page));const episodes=el('div','','cinema-detail-choices');episodes.append(this.field('Сезон',season),this.field('Серия',episode));episodes.hidden=kind.value==='movie';kind.onchange=()=>{episodes.hidden=kind.value==='movie';};form.append(episodes,this.field('Озвучка · если есть',voice));
    const extra=el('details');extra.append(el('summary','Постер и описание'),this.field('Постер · необязательно',poster),this.field('Описание',description));form.append(extra,el('p','Для другой серии укажи её номер и точную страницу. Одинаковое название, тип и год объединяются в одну карточку.','cinema-muted'));const save=this.button('Сохранить в библиотеку',()=>{} ,'primary');save.type='submit';form.append(save);
    form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;this.request({op:'add',entry:{...(existing?{title_id:existing.id}:{}),title:name.value,kind:kind.value,year:Number(year.value)||0,source_id:source.value,url:source.value?page.value:'',season:Number(season.value),episode:Number(episode.value),voice:voice.value,poster_url:poster.value,description:description.value}});};dialog.append(form);
  }
  private sources():void {
    this.content.append(this.sectionHeading('Твои источники',this.button('+ Добавить источник',()=>this.sourceForm())));
    const grid=el('div','','cinema-sources');for(const source of this.data?.sources??[]){const card=el('article','','cinema-source-card');card.append(el('h3',source.name),el('p',source.adapter==='html5'?'HTML5 · зависит от доступности плеера':'Ручное открытие и прогресс'),el('small',source.enabled?'Включён · реальный сайт не проверен':'Отключён'),this.button('Настроить',()=>this.sourceForm(source)));grid.append(card);}this.content.append(grid);
    if(!this.data?.sources.length)this.content.append(this.empty('Добавь сайт, которым уже пользуешься. Адреса и доступность сервисов задаются тобой.'));
    const examples=el('details');examples.append(el('summary','Шаблоны сайтов'),el('span','Примеры пользовательских источников:'),this.button('+ Kinogo',()=>this.sourceForm(undefined,'Kinogo')),this.button('+ Filmix',()=>this.sourceForm(undefined,'Filmix')));this.content.append(examples,el('p','Эти примеры не означают поддержку конкретного домена или плеера. DRM, CAPTCHA и доступ к платному контенту не обходятся.','cinema-muted'));
    const integration=el('section','','cinema-settings');integration.append(el('h2','Интеграция TMDB'),el('p',this.data?.tmdb?.configured?'Ключ сохранён локально.':'TMDB ещё не подключён.','cinema-muted'));const apiForm=el('form');const credential=this.input('','password');credential.autocomplete='new-password';credential.maxLength=4096;apiForm.append(this.field('API-ключ или Read Access Token',credential),el('p','Ключ хранится в закрытом файле интеграций (доступ только владельцу), вне Git и экспорта настроек. Он не передаётся LLM.','cinema-muted'));const apiSave=this.button('Сохранить ключ',()=>{});apiSave.type='submit';apiForm.append(apiSave,this.button('Удалить ключ',()=>this.request({op:'tmdb',credential:''})));apiForm.onsubmit=e=>{e.preventDefault();if(credential.value.trim()){this.request({op:'tmdb',credential:credential.value});credential.value='';}};integration.append(apiForm);const credits=el('details');credits.append(el('summary','Об интеграции и источниках данных'));const logo=el('img');logo.src='/assets/tmdb-logo.svg';logo.alt='TMDB';logo.width=100;credits.append(logo,el('p','This product uses the TMDB API but is not endorsed or certified by TMDB.'),el('p','Предложения официальных сервисов: JustWatch через TMDB. Доступность видео проверяется отдельно.'));integration.append(credits);this.content.append(integration);
    const settings=el('details','','cinema-settings');settings.append(el('summary','Поведение просмотра'));const form=el('form');const prefs=this.data!.preferences;const threshold=this.input(String(Math.round(prefs.watched_threshold*100)),'number');threshold.min='50';threshold.max='100';threshold.required=true;const auto=this.input('','checkbox');auto.checked=prefs.auto_next;const startup=this.select([['continue','Продолжить просмотр'],['library','Моя библиотека']],prefs.startup);const tracking=this.input('','checkbox');tracking.checked=prefs.save_progress!==false;const region=this.input(prefs.watch_region??'EE');region.pattern='[A-Za-z]{2}';region.maxLength=2;const poll=this.input(String(prefs.poll_seconds),'number');poll.min='2';poll.max='60';form.append(this.field('Автоматически сохранять прогресс HTML5',tracking),this.field('Интервал сохранения · секунд',poll),this.field('Страна официальных сервисов · EE, US…',region),this.field('Считать просмотренным после · %',threshold),this.field('Автоматически открывать следующую добавленную серию',auto),this.field('При открытии раздела',startup),el('p','После запуска приложения воспроизведение начинается только по твоей команде.','cinema-muted'));const save=this.button('Сохранить настройки',()=>{});save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(form.reportValidity())this.request({op:'preferences',values:{save_progress:tracking.checked,poll_seconds:Number(poll.value),watch_region:region.value.toUpperCase(),watched_threshold:Number(threshold.value)/100,auto_next:auto.checked,startup:startup.value}});};settings.append(form);this.content.append(settings);
  }
  private sourceForm(existing?:CinemaSource,preset=''):void {
    const dialog=this.showDialog(existing?'Настроить источник':'Новый источник',true);const form=el('form');const name=this.input(existing?.name??preset);name.required=true;name.maxLength=80;const base=this.input(existing?.base_url??'','url');base.required=existing?.adapter!=='local';base.placeholder='https://адрес-сайта';const search=this.input(existing?.search_url??'');search.placeholder='https://адрес-сайта/search?q={query}';const category=this.select([['site','Сайт'],['streaming','Официальный стриминг'],['local','Локальные видеофайлы']],existing?.category??'site');const adapter=this.select([['local','Локальный файл · внешний плеер'],['manual','Ручной · открытие страницы'],['html5','HTML5 · управление через Safari']],existing?.adapter??'manual');const updateLocal=()=>{const local=adapter.value==='local';base.required=!local;base.disabled=local;search.disabled=local;if(local){base.value='';search.value='';category.value='local';}else if(category.value==='local')category.value='site';};adapter.onchange=updateLocal;category.onchange=()=>{adapter.value=category.value==='local'?'local':'manual';updateLocal();};updateLocal();const enabled=this.input('','checkbox');enabled.checked=existing?.enabled??true;
    form.append(this.field('Название',name),this.field('Тип источника',category),this.field('Адрес сайта',base),this.field('Адрес поиска · необязательно',search),this.field('Способ управления',adapter),this.field('Источник включён',enabled),el('p','Для HTML5 нужны разрешение Автоматизация → Safari и JavaScript из событий Apple в Safari. Чушпан не включает эти разрешения автоматически.','cinema-muted'));const save=this.button('Сохранить источник',()=>{} ,'primary');save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;this.request({op:'source',source:{id:existing?.id??'',name:name.value,base_url:base.value,search_url:search.value,adapter:adapter.value,category:category.value,enabled:enabled.checked}});};dialog.append(form);
  }
  private history(target:HTMLElement=this.content):void {
    target.append(this.sectionHeading('История просмотра'));const toolbar=el('div','','cinema-filters');const source=this.select([['all','Все источники'],...(this.data?.sources.map(s=>[s.name,s.name] as [string,string])??[])],this.sourceFilter);source.setAttribute('aria-label','Источник в истории');source.onchange=()=>{this.sourceFilter=source.value;this.render();};const date=this.input(this.dateFilter,'date');date.setAttribute('aria-label','Дата просмотра');date.onchange=()=>{this.dateFilter=date.value;this.render();};toolbar.append(source,date);target.append(toolbar);
    const rows=this.data!.history.filter(row=>(this.sourceFilter==='all'||row.source_name===this.sourceFilter)&&row.title.toLocaleLowerCase('ru').includes(this.query.toLocaleLowerCase('ru'))&&(!this.dateFilter||new Date(row.created_at).toLocaleDateString('sv-SE')===this.dateFilter));
    if(!rows.length)target.append(this.empty('Истории по выбранным фильтрам пока нет.'));
    const list=el('div','','cinema-history');for(const row of rows){const card=el('article');const info=el('div');info.append(this.button(row.title,()=>this.detail(row.title_id,row.episode_id),'cinema-title'),el('p',`${row.season?`Сезон ${row.season} · Серия ${row.episode} · `:''}${row.source_name} · ${events[row.event]??row.event}`));card.append(info,el('span',`${playbackTime(row.position)} / ${row.duration?playbackTime(row.duration):'—'}`),el('time',new Date(row.created_at).toLocaleString('ru-RU',{dateStyle:'short',timeStyle:'short'})));list.append(card);}target.append(list);
  }
}

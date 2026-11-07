import type {HudEvent} from './types';
import {icon} from './dashboard';
import {CinemaCatalogueView} from './cinema_catalogue';
import {posterActions} from './cinema_actions';
import type {CataloguePage,CatalogueConfig} from './cinema_catalogue';
import {CinemaPersonalView} from './cinema_personal';
import type {FilmProfile,PersonalRecommendations} from './cinema_personal';
import {CinemaEmbeddedPlayer} from './cinema_player';
import {CinemaViewer} from './cinema_viewer';
import {CinemaSourceDiagnostics} from './cinema_source_diagnostics';
import {browserDiagnosticsPanel,copyDiagnostic} from './cinema_browser_diagnostics';
import type {BrowserDiagnosticReport} from './cinema_browser_diagnostics';
import type {SourceDiagnosticReport} from './cinema_source_diagnostics';
import type {PlaybackSnapshot} from './cinema_viewer';
import type {EmbeddedPlayerState,EmbeddedControl} from './cinema_player';

export interface CinemaSource {id:string;name:string;base_url:string;search_url:string;adapter:'manual'|'html5'|'local'|'direct';category?:'site'|'streaming'|'local';enabled:boolean;priority?:number;preferred?:boolean;search_mode?:'manual'|'archive'|'sitemap';language?:string;access_type?:string;access_status?:string;embed_allowed?:boolean;health?:{status:string;checked_at?:string;last_success?:string;reason?:string};capabilities?:{mode:string;search:boolean;embedded:boolean;progress:boolean;content_types?:string[];formats?:string[];seasons?:boolean;episodes?:boolean;voices?:boolean;controls?:string[];verification_status?:string}}
export interface CinemaLink {id:string;episode_id:string;source_id:string;source_name:string;url:string;voice:string}
export interface CinemaEpisode {id:string;title_id:string;season:number;episode:number;position:number;duration:number;watched:number;last_viewed:string|null;last_link_id:string|null;name?:string;links:CinemaLink[]}
export interface CinemaMetadata {genre_ids?:number[];directors?:string[];cast?:string[];keywords?:string[];backdrop_url?:string;country_names?:string[];genres:string[];runtime:number;rating:number;seasons:{season:number;episodes:number;name:string}[];episode_count:number;original_title?:string;original_language?:string;countries?:string[];release_date?:string;popularity?:number}
export interface CatalogueTitle {library_id?:string;title:string;kind:'movie'|'series';year:number;poster_url:string;description:string;catalogue_key:string;metadata:CinemaMetadata}
export interface SourceResult {viewing_status?:string;browser_openable?:boolean;identity_status?:string;episode_status?:string;player_status?:string;control_status?:string;page_status?:string;video_type?:string;content_verified?:boolean;classification_reason?:string;previously_played?:boolean;configurable?:boolean;scope?:string;region_access?:string;voice?:string;new_source?:boolean;id:string;source_id:string;source_name:string;kind:string;url:string;availability:string;reason:string;embedded:boolean;language:string;access_type:string;access_status?:string;manual_allowed?:boolean;requires_confirmation?:boolean;allowed:boolean;match:number;capabilities:{mode:string}}
export interface SourceResults {request_id?:string;title_id:string;episode_id:string;results:SourceResult[];selected_id:string|null;message:string}
interface SourceOptions {title_id:string;episode_id:string;options:{source:CinemaSource;link:CinemaLink|null;available:boolean;reason:string}[];official?:{region:string;url:string;providers:{name:string;type:string}[]};provider_error?:string}
export interface CinemaTitle {id:string;title:string;kind:'movie'|'series';year:number;poster_url:string;description:string;status:'watching'|'planned'|'watched';watchlist?:number;manual_watched?:number|null;favorite:number;catalogue_key?:string;metadata?:CinemaMetadata;user_rating?:number|null;user_note?:string;completed_at?:string|null;added_at?:string;favorite_added_at?:string|null;source_memory?:{pinned_source_id?:string;last_source_id?:string;page_url?:string};episodes:CinemaEpisode[];current:CinemaEpisode|null}
interface CinemaHistory {id:number;episode_id:string;title:string;title_id:string;season:number;episode:number;position:number;duration:number;created_at:string;event:string;source_name:string;watched:number}
export interface CinemaPreferences {
  internet_search?:boolean;search_provider?:'tavily'|'brave'|'safari';prefer_free?:boolean;prefer_russian?:boolean;open_in_safari?:boolean;autoplay?:boolean;remember_voice?:boolean;offer_next?:boolean;resume_rewind_seconds?:number;watched_threshold:number;auto_next:boolean;startup:'continue'|'library';poll_seconds:number;devices:Record<string,'mac'|'airplay'>;
  watch_region?:string;save_progress?:boolean;recommendations_show_watched?:boolean;preferred_language?:string;free_only?:boolean;auto_search?:boolean;auto_open?:boolean;ask_paid?:boolean;auto_select_source?:boolean;use_last_source?:boolean;embedded_playback?:boolean;allow_browser?:boolean;personalize?:boolean;
  catalogue_filters?:Partial<import('./cinema_catalogue').CatalogueFilters>;russian_titles?:boolean;hide_animation?:boolean;hide_anime?:boolean;hide_documentary?:boolean;hide_watched?:boolean;hide_watchlist?:boolean;hide_saved?:boolean;default_sort?:string;cards_per_row?:number;resume_position?:boolean;recommend_ratings?:boolean;recommend_favorites?:boolean;recommend_history?:boolean;
}
export interface CinemaEvent {browser_diagnostics?:BrowserDiagnosticReport;source_diagnostics?:SourceDiagnosticReport;playback?:PlaybackSnapshot;internet_search?:{configured:boolean;provider:string;providers?:{tavily?:{configured:boolean;storage:string;state:string};brave?:{configured:boolean;storage:string}}};search_connection?:{provider:string;ok:boolean;authenticated?:boolean;code:string;message:string;usage?:number;limit?:number|null};cinema_stage?:{stage:string;status:string;message:string;title_id:string;episode_id:string};cinema_diagnostics?:{id:number;stage:string;status:string;message:string;created_at:string}[];safari_check?:{method:string;extension:string;automation:string;javascript?:string};next_episode?:{title_id:string;episode_id:string;season:number;episode:number;title:string;player?:string|null}|null;watch_progress?:{request_id:string;title_id:string;message:string};watch_confirmation?:{token:string;source_name:string;url:string;message:string};detail_updated?:string;similar?:{request_id:string;items:CatalogueTitle[]};library_export?:unknown;library_import_preview?:{token:string;titles:number;sources:number;events:number;existing:number};library_imported?:{added:number;preserved:number};source_action?:{result_id:string;message:string}|null;recommendation_dismissed?:string;recommendations_reset?:boolean;personal_result?:{request_id:string;profile?:FilmProfile;recommendations?:PersonalRecommendations;error?:string};personal_recommendations?:PersonalRecommendations;source_results?:SourceResults;embedded_player?:EmbeddedPlayerState;embedded_control?:EmbeddedControl;type:'cinema';items:CinemaTitle[];sources:CinemaSource[];history:CinemaHistory[];preferences:CinemaPreferences;location:string;devices:{id:string;name:string;available:boolean;reason:string}[];durable:boolean;session:{title_id:string;episode_id:string;mode:string}|null;message:string;error?:string;saved?:string;navigate?:boolean;matches?:CatalogueTitle[];tmdb?:{configured:boolean};catalogue_detail?:CatalogueTitle;source_options?:SourceOptions;active_tab?:string;season_loaded?:string;loaded_season?:number;catalogue_page?:CataloguePage;catalogue_config?:CatalogueConfig;catalogue_config_error?:string;catalogue_intent?:{query?:string;genre?:string}}

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
  private personalView:CinemaPersonalView;
  private embeddedPlayer:CinemaEmbeddedPlayer;
  private viewer:CinemaViewer;
  private sourceDiagnostics:CinemaSourceDiagnostics;
  private viewerToken='';
  private pendingWatch:string|null=null;
  private pendingFind=false;
  private sourceSequence=0;
  private sourceSignature='';
  private settingsSection='catalogue';
  private detailRequests=new Set<string>();
  private ratingReturn:(()=>void)|null=null;
  private detailTab='watch';
  private detailIdentity='';
  private relatedSequence=0;
  private pendingDetailKey='';
  private sourceRequest='';
  private dialog:HTMLDialogElement|null=null;
  private selectedLink:string|null=null;
  private search:HTMLInputElement;
  private sourceFilter='all';
  private dateFilter='';
  private first=true;
  private nextOffer=el('div','','cinema-next-offer');
  constructor(private send:(value:object)=>void,private chat:(text:string)=>void,private navigate:()=>void,private listen:()=>void) {
    const heading=el('header','','cinema-heading');const title=el('h1');title.innerHTML=icon('film');title.append(el('span','Медиатека'));
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
      remove:item=>this.deleteTitle(item),query:value=>{this.query=value;this.search.value=value;},settings:()=>{this.changeTab('integration');}});
    this.personalView=new CinemaPersonalView({send:value=>this.request(value),poster:item=>this.poster(item),history:target=>this.history(target),
      detail:item=>{if('id' in item)this.detail(item.id);else{const existing=this.data?.items.find(i=>i.catalogue_key===item.catalogue_key);if(existing)this.detail(existing.id);else this.request({op:'catalogue_detail',key:item.catalogue_key});}},
      change:(item,values)=>this.changeTitle(item,values),watch:item=>{const existing=this.libraryItem(item);if(existing)this.open(existing);else{this.pendingWatch=item.catalogue_key??null;this.request({op:'catalogue_add',key:item.catalogue_key});}}});
    this.embeddedPlayer=new CinemaEmbeddedPlayer(value=>this.request(value),this.root);
    this.viewer=new CinemaViewer(value=>this.request(value));
    this.sourceDiagnostics=new CinemaSourceDiagnostics(value=>this.request(value));
    const ai=this.button('✧ AI-поиск',()=>{composer.hidden=!composer.hidden;ai.setAttribute('aria-expanded',String(!composer.hidden));if(!composer.hidden)input.focus();},'cinema-ai-toggle');ai.setAttribute('aria-expanded','false');ai.setAttribute('aria-controls','cinema-ai-panel');
    const filters=this.button('Фильтры',()=>{this.changeTab('catalogue');this.catalogueView.openFilters();},'cinema-header-filters');filters.innerHTML=icon('settings');filters.setAttribute('aria-label','Фильтры каталога');filters.title='Фильтры каталога';heading.insertBefore(filters,mic);heading.insertBefore(ai,mic);
    composer.className='cinema-ai-panel';composer.id='cinema-ai-panel';composer.hidden=true;composer.setAttribute('aria-label','AI-поиск кино');input.placeholder='Найди интересный детектив на вечер';
    const close=()=>{composer.hidden=true;ai.setAttribute('aria-expanded','false');ai.focus();};
    const dismiss=this.button('×',close,'cinema-ai-close');dismiss.setAttribute('aria-label','Закрыть AI-поиск');composer.append(dismiss);
    composer.onsubmit=e=>{e.preventDefault();if(this.online&&input.value.trim()){this.chat(input.value.trim());input.value='';close();}};
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!composer.hidden){e.preventDefault();e.stopImmediatePropagation();close();}},true);
    this.root.append(heading,composer,this.tabs,this.feedback,this.nextOffer,this.viewer.root,this.content);
    document.addEventListener('cinema:settings',()=>{this.changeTab('sources');});
    document.addEventListener('cinema:integration',()=>this.changeTab('integration'));
    document.addEventListener('workspace:navigate',event=>{if((event as CustomEvent).detail==='cinema'&&this.online)this.request({op:'refresh'});});
    this.connection(false);
  }
  private button(label:string,action:()=>void,cls=''):HTMLButtonElement {const b=el('button',label,cls);b.type='button';b.disabled=!this.online;b.onclick=action;return b;}
  private field(label:string,input:HTMLElement):HTMLLabelElement {input.setAttribute('aria-label',label);const node=el('label','','cinema-field');node.append(el('span',label),input);return node;}
  private input(value='',type='text'):HTMLInputElement {const n=el('input');n.type=type;n.value=value;return n;}
  private select(options:[string,string][],value:string):HTMLSelectElement {const node=el('select');for(const [id,label]of options){const option=el('option',label);option.value=id;node.append(option);}node.value=value;return node;}
  private request(value:object):void {if(this.online){const request=value as {op:string;key?:string};if(request.op==='catalogue_detail')this.pendingDetailKey=request.key??'';if(request.op==='focus')this.pendingDetailKey='';if(['watch','open','watch_confirm','source_result_open','playback_switch'].includes((value as {op:string}).op)||((value as {op:string;action?:string}).op==='playback_control'&&(value as {action?:string}).action==='close'))this.embeddedPlayer?.prepareSwitch();this.send(value);}}
  connection(online:boolean):void {this.online=online;this.catalogueView.connection(online);this.personalView.connection(online);this.viewer.connection(online);if(!online)this.feedback.textContent='Нет соединения с Чушпаном. Последние данные могут устареть.';else this.request({op:'refresh'});this.root.querySelectorAll<HTMLInputElement|HTMLSelectElement|HTMLButtonElement>('input,select,button').forEach(n=>n.disabled=!online);this.sourceDiagnostics.connection(online);this.render();}
  receive(event:HudEvent):void {
    if(event.type!=='cinema')return;
    this.data=event;this.catalogueView.receive(event);this.personalView.receive(event);if(event.embedded_player)this.closeDialog(false);this.embeddedPlayer.receive(event);this.viewer.receive(event);this.sourceDiagnostics.receive(event);
    if(event.playback&&['PLAYING','PAUSED'].includes(event.playback.state)&&event.playback.token!==this.viewerToken){this.viewerToken=event.playback.token;this.closeDialog(false);}
    if(this.first){this.tab=event.preferences.startup==='library'?'planned':'watching';this.first=false;}
    this.feedback.textContent=event.error??event.message??'';this.feedback.dataset.error=String(!!event.error);
    if(event.error){this.pendingWatch=null;this.ratingReturn=null;}
    if(event.error&&this.dialog){let error=this.dialog.querySelector<HTMLElement>('.cinema-form-error');if(!error){error=el('p','','cinema-form-error');error.setAttribute('role','alert');this.dialog.append(error);}error.textContent=event.error;}
    if(event.saved&&this.dialog?.dataset.form==='true')this.closeDialog();
    if(event.navigate){this.tab=event.active_tab??'watching';this.navigate();if(event.active_tab==='catalogue'){if(event.catalogue_intent)this.catalogueView.intent(event.catalogue_intent);else if(event.matches?.length)this.catalogueView.legacy(event.matches);}}
    if(!event.durable)this.feedback.textContent='Долговременная SQLite недоступна. Изменения не сохраняются.';
    this.render();
    if(event.browser_diagnostics){const old=this.content.querySelector<HTMLElement>('[data-browser-diagnostics]');if(old){const selected=[...old.querySelectorAll<HTMLSelectElement>('select')].map(s=>s.value);const panel=browserDiagnosticsPanel(event.browser_diagnostics,event.sources,event.items,v=>this.request(v),this.online);[...panel.querySelectorAll<HTMLSelectElement>('select')].forEach((s,i)=>{if(selected[i]&&[...s.options].some(o=>o.value===selected[i]))s.value=selected[i];});old.replaceWith(panel);}}
    if(event.library_export)void this.exportLibrary(event.library_export).catch(()=>{this.feedback.textContent='Не удалось сохранить копию. Попробуй ещё раз.';});
    if(event.library_import_preview){const p=event.library_import_preview;const dialog=this.showDialog('Импорт медиатеки');dialog.append(el('p',`В копии: ${p.titles} названий, ${p.sources} источников, ${p.events} событий. В твоей медиатеке: ${p.existing} названий.`),el('p','Будут добавлены отсутствующие записи. Существующие оценки, заметки, статусы, ссылки и прогресс сохраняются. Настройки восстанавливаются только в пустую медиатеку.','cinema-muted'),this.button('Импортировать',()=>this.request({op:'library_import',token:p.token}),'primary'),this.button('Отмена',()=>this.closeDialog()));}
    if(event.library_imported)this.closeDialog();
    if(event.catalogue_detail&&event.catalogue_detail.catalogue_key===this.pendingDetailKey)this.catalogueDetail(event.catalogue_detail);
    if(event.similar&&this.dialog?.dataset.relatedRequest===event.similar.request_id)this.renderSimilar(event.similar.items);
    if(event.saved&&this.ratingReturn){const back=this.ratingReturn;this.ratingReturn=null;back();}
    if(event.watch_progress&&(!event.watch_progress.request_id||event.watch_progress.request_id===this.sourceRequest)){const progress=this.dialog?.querySelector('[data-watch-progress]');if(progress){progress.textContent=event.watch_progress.message;if(!event.watch_progress.message.endsWith('…'))progress.classList.remove('cinema-loading');}this.feedback.textContent=event.watch_progress.message;}
    if(event.watch_confirmation){const value=event.watch_confirmation;const dialog=this.showDialog(`Открыть ${value.source_name}?`);dialog.classList.add('cinema-compact-search');dialog.append(el('p',value.message),el('small',value.url,'cinema-result-url'),this.button('Открыть страницу сервиса',()=>{this.request({op:'watch_confirm',token:value.token});this.closeDialog();},'primary'),this.button('Отмена',()=>this.closeDialog()));}
    this.nextOffer.replaceChildren();if(event.next_episode){const next=event.next_episode;const item=event.items.find(i=>i.id===next.title_id);this.nextOffer.append(el('span',`Следующая: ${next.title} · сезон ${next.season}, серия ${next.episode}`),this.button('Продолжить следующую',()=>{if(item){if(next.player)this.request({op:'watch',title_id:item.id,episode_id:next.episode_id,player:next.player});else this.open(item,item.episodes.find(e=>e.id===next.episode_id));}},'primary'));}
    if(event.cinema_stage){const stage=event.cinema_stage;const progress=this.dialog?.querySelector('[data-watch-progress]');if(progress&&['OPEN_SAFARI','DETECT_PLAYER','RESTORE_POSITION','PLAY','FULLSCREEN'].includes(stage.stage))progress.textContent=stage.message;const log=this.content.querySelector<HTMLElement>('[data-cinema-diagnostics]');if(log){const line=el('p',`${stage.stage} · ${stage.status}: ${stage.message}`);log.prepend(line);while(log.childElementCount>40)log.lastElementChild?.remove();}}
    if(event.cinema_diagnostics){const log=this.content.querySelector<HTMLElement>('[data-cinema-diagnostics]');if(log)log.replaceChildren(...event.cinema_diagnostics.map(r=>el('p',`${new Date(r.created_at).toLocaleTimeString('ru-RU')} · ${r.stage} · ${r.status}: ${r.message}`)));}
    if(event.search_connection){const result=event.search_connection;const status=this.content.querySelector<HTMLElement>('[data-tavily-connection]');if(status){status.textContent=result.message+(result.usage!==undefined?` Использовано кредитов ключа: ${result.usage}${result.limit!=null?' / '+result.limit:''}.`:'');status.dataset.error=String(!result.ok);}this.feedback.textContent=result.message;this.feedback.dataset.error=String(!result.ok);}
    if(event.safari_check){const check=event.safari_check;const status=this.content.querySelector<HTMLElement>('[data-safari-status]');if(status)status.textContent=`${check.method}: Автоматизация — ${check.automation}; JavaScript — ${check.javascript??'не проверен'}. Safari Web Extension: не установлено.`;}
    if(event.source_options)this.sourceChoices(event.source_options);
    if(event.source_action&&this.dialog){const row=[...this.dialog.querySelectorAll<HTMLElement>('[data-result-id]')].find(n=>n.dataset.resultId===event.source_action!.result_id);if(row){let status=row.querySelector<HTMLElement>('[role=status]');if(!status){status=el('p','','cinema-muted');status.setAttribute('role','status');row.append(status);}status.textContent=event.source_action.message;}}
    if(event.source_results&&(!event.source_results.request_id||event.source_results.request_id===this.sourceRequest))this.sourceResults(event.source_results);
    if(event.season_loaded)this.detail(event.season_loaded,event.items.find(i=>i.id===event.season_loaded)?.episodes.find(ep=>ep.season===event.loaded_season)?.id);
    if(event.saved&&this.pendingWatch){const item=event.items.find(v=>v.id===event.saved);if(item){this.pendingWatch=null;if(this.pendingFind){this.pendingFind=false;if(this.dialog?.dataset.rich==='true')this.detail(item.id);this.open(item);}else this.open(item);}}
  }
  private changeTab(tab:string,render=true):void {
    if(this.tab!==tab){const scroller=this.root.closest<HTMLElement>('.workspace-page');if(scroller)this.scrollPositions.set(this.tab,scroller.scrollTop);this.tab=tab;}
    if(render)this.render();
  }
  private render():void {
    this.tabs.replaceChildren(...[['catalogue','search','Каталог'],['personal','activity','Для тебя'],['planned','bookmark','Хочу посмотреть'],['watching','play','Смотрю'],['watched','history','Просмотрено'],['favorites','heart','Избранное'],['profile','user','Мой кинопрофиль']].map(([id,glyph,label])=>{
      const b=this.button(label,()=>this.changeTab(id));b.innerHTML=icon(glyph);b.append(el('span',label));b.setAttribute('aria-current',this.tab===id?'page':'false');return b;
    }));
    const scroller=this.root.closest<HTMLElement>('.workspace-page');
    const top=this.renderedTab===this.tab?scroller?.scrollTop??0:this.scrollPositions.get(this.tab)??0;
    this.renderedTab=this.tab;
    if(!this.data){this.content.replaceChildren(this.empty(this.online?'Загружаю твою кинотеку…':'Кинотека появится после подключения к Чушпану.'));return;}
    if(this.tab!=='sources'&&this.tab!=='integration')delete this.content.dataset.settings;
    if(this.tab==='catalogue'){this.catalogue();}
    else if(this.tab==='profile'||this.tab==='personal'){this.personalView.mount(this.content,this.tab);}
    else if(this.tab==='sources'||this.tab==='integration'){const signature=JSON.stringify([this.data.sources,this.data.preferences,this.data.tmdb,this.data.internet_search,this.tab,this.data.items.length]);if(this.content.dataset.settings!=='true'||signature!==this.sourceSignature){this.content.replaceChildren();if(this.tab==='integration')this.tmdbIntegration(this.content);else this.sources();this.content.dataset.settings='true';this.sourceSignature=signature;}}
    else this.renderLibrary();
    if(scroller)scroller.scrollTop=top;
    this.syncDetail();
  }
  private renderLibrary():void {
    if(!this.data)return;
    const visible=this.data.items.filter(item=>(this.tab==='favorites'?!!item.favorite:this.tab==='planned'?(item.watchlist==null?item.status==='planned':!!item.watchlist):item.status===this.tab)
      &&(this.libraryKind==='all'||item.kind===this.libraryKind)
      &&item.title.toLocaleLowerCase('ru').includes(this.libraryQuery.toLocaleLowerCase('ru'))
      &&(this.tab!=='watched'||(!this.ratingMin&&this.ratingMax===10)||
        (item.user_rating!=null&&item.user_rating>=this.ratingMin&&item.user_rating<=this.ratingMax)));
    visible.sort((a,b)=>this.librarySort==='rating'?(b.user_rating??-1)-(a.user_rating??-1)||a.title.localeCompare(b.title,'ru'):
      this.librarySort==='title'?a.title.localeCompare(b.title,'ru'):this.librarySort==='year'?b.year-a.year||a.title.localeCompare(b.title,'ru'):
      (this.tab==='favorites'?b.favorite_added_at??b.added_at??'':this.tab==='watching'?b.current?.last_viewed??'':b.added_at??'')
        .localeCompare(this.tab==='favorites'?a.favorite_added_at??a.added_at??'':this.tab==='watching'?a.current?.last_viewed??'':a.added_at??'')||a.title.localeCompare(b.title,'ru'));
    const section=el('section','','cinema-library');
    const toolbar=el('div','','cinema-library-toolbar');const types=el('div','','cinema-type-switch');
    for(const [id,label] of [['all','Всё'],['movie','Фильмы'],['series','Сериалы']]){const b=this.button(label,()=>{this.libraryKind=id;this.render();});b.setAttribute('aria-pressed',String(this.libraryKind===id));types.append(b);}
    const sort=this.select([['recent',this.tab==='watching'?'По последнему просмотру':'По дате добавления'],['rating','По моей оценке'],['title','По названию'],['year','По году выпуска']],this.librarySort);sort.onchange=()=>{this.librarySort=sort.value;this.render();};
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
    if(this.data.session){const session=this.data.session;const toolbar=el('div','','cinema-player-bar');toolbar.append(el('span',session.mode==='embedded'?(this.data.preferences.save_progress===false?'Встроенный плеер · автосохранение выключено':'Встроенный плеер · прогресс сохраняется'):session.mode==='html5'?'Активный HTML5-плеер':'Ручной режим · сохрани прогресс в карточке'),...['Поставь фильм на паузу','Продолжай','Включи следующую серию','Какая это серия?'].map(command=>this.button(command,()=>this.request({op:'control',command}))));this.content.append(toolbar);}
  }
  private syncDetail():void {
    const item=this.data?.items.find(i=>(!!this.dialog?.dataset.titleId&&i.id===this.dialog.dataset.titleId)||(!!this.dialog?.dataset.catalogueKey&&i.catalogue_key===this.dialog.dataset.catalogueKey));
    if(!item||!this.dialog)return;
    const rating=this.dialog.querySelector<HTMLSelectElement>('[data-personal-rating]');if(rating)rating.value=String(item.user_rating??'');
    const status=this.dialog.querySelector<HTMLSelectElement>('[data-status]');if(status)status.value=item.status;
    const personal=this.dialog.querySelector('.cinema-personal-rating');if(personal)personal.textContent=`Моя оценка: ${item.user_rating??'—'}/10`;
    if(this.dialog.dataset.rich==='true')this.syncRichDetail(item);
    const heart=this.dialog.querySelector<HTMLButtonElement>('[data-detail-favorite]');if(heart){heart.textContent=item.favorite?'♥ В избранном':'♡ В избранное';heart.setAttribute('aria-pressed',String(!!item.favorite));}
  }
  private detailFavorite(item:CinemaTitle|CatalogueTitle):HTMLButtonElement {
    const current=()=>this.data?.items.find(i=>'id' in item?i.id===item.id:i.catalogue_key===item.catalogue_key);
    const b=this.button('♡ В избранное',()=>{const existing=current();this.request(existing?{op:'update',id:existing.id,values:{favorite:!existing.favorite}}:{op:'catalogue_add',key:item.catalogue_key,values:{favorite:true}});},'cinema-detail-favorite');b.dataset.detailFavorite='';b.setAttribute('aria-pressed',String(!!current()?.favorite));b.textContent=current()?.favorite?'♥ В избранном':'♡ В избранное';return b;
  }
  private metadata(item:{year:number;kind:string;metadata?:CinemaMetadata}):string {const m=item.metadata;return [item.kind==='series'?'Сериал':'Фильм',item.year||'',m?.genres.join(', '),m?.runtime?`${m.runtime} мин`:'',m?.rating?`★ ${m.rating.toFixed(1)}`:''].filter(Boolean).join(' · ');}
  private catalogue():void {this.catalogueView.mount(this.content);}

  private catalogueDetail(item:CatalogueTitle):void {
    const known=this.data?.items.find(i=>i.catalogue_key===item.catalogue_key);if(known){this.detail(known.id);return;}
    const dialog=this.showDialog(item.title);dialog.dataset.catalogueKey=item.catalogue_key;this.request({op:'catalogue_focus',key:item.catalogue_key});this.richDetail(dialog,item);
  }
  private richDetail(dialog:HTMLDialogElement,item:CinemaTitle|CatalogueTitle):void {
    const identity=('id' in item?item.id:item.catalogue_key)??item.title;if(this.detailIdentity!==identity){this.detailIdentity=identity;this.detailTab='watch';}
    dialog.dataset.rich='true';dialog.classList.add('cinema-rich-dialog');dialog.querySelector('header h2')!.textContent=item.kind==='series'?'Карточка сериала':'Карточка фильма';
    dialog.querySelector('.cinema-detail-summary')?.remove();dialog.querySelector('.cinema-ratings')?.remove();dialog.querySelector('[data-detail-favorite]')?.remove();
    const rating=dialog.querySelector<HTMLElement>('[data-personal-rating]');if(rating)rating.closest<HTMLElement>('label')!.hidden=true;
    const legacy=el('details','','cinema-playback-details');legacy.append(el('summary','Продолжение, озвучка и ручной прогресс'));
    for(const child of [...dialog.children].slice(1))legacy.append(child);
    const hero=el('section','','cinema-detail-hero');const background=el('img','','cinema-detail-backdrop');background.alt='';background.referrerPolicy='no-referrer';hero.append(background);
    const poster=this.poster(item);const body=el('div','','cinema-detail-intro');body.append(el('small',item.kind==='series'?'СЕРИАЛ':'ФИЛЬМ','cinema-profile-eyebrow'),el('h2',item.title),el('p',this.metadata(item),'cinema-detail-meta'));
    const ratings=el('div','','cinema-ratings');ratings.append(el('span',`★ TMDB: ${item.metadata?.rating?.toFixed(1)??'—'}/10`,'cinema-tmdb-rating'));
    const own=this.button('Оценить фильм',()=>this.ratingModal(item),'cinema-rating-edit');own.setAttribute('aria-label','Оценить фильм');own.append(el('span',`Моя оценка: ${'user_rating' in item?item.user_rating??'—':'—'}/10`,'cinema-personal-rating'));ratings.append(own);body.append(ratings);
    const actions=el('div','','cinema-detail-main-actions');actions.append(this.button('Найти и смотреть',()=>{const known=this.libraryItem(item);if(known)this.open(known,known.episodes.find(e=>e.id===dialog.dataset.episodeId)??known.current);else{this.pendingFind=true;this.pendingWatch=item.catalogue_key??null;this.request({op:'catalogue_add',key:item.catalogue_key});}},'primary'));
    actions.append(this.button('Трейлер',()=>this.request({op:'trailer',key:item.catalogue_key,title_id:'id' in item?item.id:undefined})));
    for(const [status,label]of Object.entries(statuses)){const b=this.button(label,()=>this.changeTitle(item,{status}));b.dataset.statusAction=status;actions.append(b);}actions.append(this.detailFavorite(item));body.append(actions);hero.append(poster,body);
    const tabs=el('div','','cinema-detail-tabs');tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','Подробности фильма');
    const watch=el('section','','cinema-detail-pane');watch.dataset.detailPane='watch';const sourceSlot=el('div','','cinema-watch-sources');sourceSlot.append(el('p','Нажми «Найти и смотреть», чтобы проверить подключённые источники.','cinema-muted'));watch.append(sourceSlot);
    for(const label of [...legacy.querySelectorAll('label')])if(['Сезон','Серия'].includes(label.querySelector('select')?.getAttribute('aria-label')??''))watch.insertBefore(label,sourceSlot);
    if('id' in item)watch.append(legacy);
    const info=el('section','','cinema-detail-pane cinema-rich-info');info.dataset.detailPane='info';
    const similar=el('section','','cinema-detail-pane cinema-similar-grid');similar.dataset.detailPane='similar';
    for(const [key,label]of [['watch','Где смотреть'],['info','О фильме'],['similar','Похожие']]){const b=this.button(label,()=>{this.detailTab=key;select();});b.setAttribute('role','tab');b.dataset.detailTab=key;tabs.append(b);}
    const select=()=>{for(const b of tabs.querySelectorAll<HTMLButtonElement>('button')){const active=b.dataset.detailTab===this.detailTab;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;}for(const pane of [watch,info,similar]){pane.hidden=pane.dataset.detailPane!==this.detailTab;pane.setAttribute('role','tabpanel');}if(this.detailTab==='similar'&&!similar.dataset.loaded){similar.dataset.loaded='true';similar.append(el('p','Загружаю похожие истории…','cinema-loading'),this.button('Повторить похожие',()=>{delete similar.dataset.loaded;select();}));if(item.catalogue_key?.startsWith('tmdb:')){const id=`similar-${++this.relatedSequence}`;dialog.dataset.relatedRequest=id;this.request({op:'similar',key:item.catalogue_key,request_id:id});}else similar.replaceChildren(el('p','Для похожих историй нужно название из каталога TMDB.','cinema-muted'));}};
    tabs.onkeydown=e=>{const buttons=[...tabs.querySelectorAll<HTMLButtonElement>('button')];const current=buttons.indexOf(document.activeElement as HTMLButtonElement);if(current<0||!['ArrowRight','ArrowLeft','Home','End'].includes(e.key))return;e.preventDefault();const index=e.key==='Home'?0:e.key==='End'?buttons.length-1:(current+(e.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;buttons[index].click();buttons[index].focus();};
    dialog.append(hero,tabs,watch,info,similar);this.syncRichDetail(item);select();
  }
  private libraryItem(item:CinemaTitle|CatalogueTitle):CinemaTitle|undefined {return this.data?.items.find(i=>'id' in item?i.id===item.id:'library_id' in item&&item.library_id?i.id===item.library_id:i.catalogue_key===item.catalogue_key);}
  private changeTitle(item:CinemaTitle|CatalogueTitle,values:object):void {const existing=this.libraryItem(item);this.request(existing?{op:'update',id:existing.id,values}:{op:'catalogue_add',key:item.catalogue_key,values});}
  private syncRichDetail(item:CinemaTitle|CatalogueTitle):void {
    const dialog=this.dialog;if(!dialog)return;const meta=item.metadata;
    const background=dialog.querySelector<HTMLImageElement>('.cinema-detail-backdrop');if(background){if(meta?.backdrop_url){if(background.getAttribute('src')!==meta.backdrop_url)background.src=meta.backdrop_url;background.hidden=false;}else background.hidden=true;}
    const line=dialog.querySelector('.cinema-detail-meta');if(line)line.textContent=this.metadata(item);
    const pane=dialog.querySelector<HTMLElement>('.cinema-rich-info');const ownNote=this.libraryItem(item)?.user_note;const signature=JSON.stringify([meta,item.description,ownNote]);
    if(pane&&pane.dataset.signature!==signature){pane.dataset.signature=signature;const dl=el('dl');for(const [label,value]of [['Год',String(item.year||'—')],['Жанры',meta?.genres.join(', ')],['Продолжительность',meta?.runtime?`${meta.runtime} мин`:'—'],['Страна производства',(meta?.country_names?.length?meta.country_names:meta?.countries)?.join(', ')],['Режиссёр',meta?.directors?.join(', ')],['В ролях',meta?.cast?.slice(0,12).join(', ')],['Оригинальное название',meta?.original_title]]){dl.append(el('dt',label??''),el('dd',value||'Пока нет данных'));}pane.replaceChildren(el('h3','О фильме'),el('p',item.description||'Описание пока отсутствует.'),dl);if('id' in item&&item.catalogue_key?.startsWith('tmdb:'))pane.append(this.button('Обновить сведения',()=>this.request({op:'title_details',id:item.id})));if(ownNote)pane.append(el('h3','Моя заметка'),el('blockquote',ownNote));}
    if('episodes' in item&&item.kind==='series'){const select=dialog.querySelector<HTMLSelectElement>('[aria-label="Сезон"]');if(select)for(const season of meta?.seasons??[])if(![...select.options].some(o=>o.value===String(season.season))){const option=el('option',season.name||'Сезон '+season.season);option.value=String(season.season);select.append(option);}}
    const existing=this.libraryItem(item);for(const b of dialog.querySelectorAll<HTMLButtonElement>('[data-status-action]'))b.setAttribute('aria-pressed',String(b.dataset.statusAction===existing?.status));
  }
  private renderSimilar(items:CatalogueTitle[]):void {
    const pane=this.dialog?.querySelector<HTMLElement>('[data-detail-pane=similar]');if(!pane)return;pane.replaceChildren();
    if(!items.length)pane.append(el('p','Похожих историй с текущими ограничениями пока не найдено.','cinema-muted'));
    for(const item of items.slice(0,12)){const card=el('article','','cinema-card');const poster=this.button(`Подробнее: ${item.title}`,()=>{const known=this.libraryItem(item);if(known)this.detail(known.id);else this.request({op:'catalogue_detail',key:item.catalogue_key});},'cinema-poster-button');poster.replaceChildren(this.poster(item));const body=el('div','','cinema-card-body');body.append(el('strong',item.title),el('p',`${item.year||'—'} · ★ ${item.metadata.rating.toFixed(1)}`));card.append(poster,body);pane.append(card);}
  }
  private ratingModal(item:CinemaTitle|CatalogueTitle):void {
    const existing=this.libraryItem(item);const dialog=this.showDialog('Моя оценка');dialog.dataset.rating='true';dialog.classList.add('cinema-rating-dialog');
    this.request(existing?{op:'focus',id:existing.id}:{op:'catalogue_focus',key:item.catalogue_key});const back=()=>{const known=this.libraryItem(item);if(known)this.detail(known.id);else this.catalogueDetail(item as CatalogueTitle);};
    const summary=el('div','','cinema-rating-summary');summary.append(this.poster(item),el('h3',item.title));const form=el('form');let selected=existing?.user_rating??8;const value=el('output',selected.toFixed(1),'cinema-rating-value');value.setAttribute('aria-live','polite');
    const stars=el('div','','cinema-rating-stars');stars.setAttribute('role','radiogroup');stars.setAttribute('aria-label','Оценка от 1 до 10');
    const range=this.input(String(selected),'range');range.min='1';range.max='10';range.step='.5';range.setAttribute('aria-label','Личная оценка');
    const choose=(rating:number)=>{selected=rating;value.textContent=rating.toFixed(1);range.value=String(rating);for(const input of stars.querySelectorAll<HTMLInputElement>('input')){input.checked=Number(input.value)===rating;input.closest('label')!.dataset.filled=String(Number(input.value)<=rating);}};
    for(let i=1;i<=10;i++){const star=el('div','','cinema-rating-star');for(const half of [i-.5,i]){if(half<1)continue;const label=el('label','★');label.className=Number.isInteger(half)?'full':'half';const input=this.input(String(half),'radio');input.name='cinema-rating';input.setAttribute('aria-label',`${half} из 10`);input.onchange=()=>choose(half);label.prepend(input);star.append(label);}stars.append(star);}range.oninput=()=>choose(Number(range.value));
    const note=el('textarea');note.value=existing?.user_note??'';note.maxLength=2000;note.rows=3;note.placeholder='Что запомнилось в этой истории?';form.append(value,stars,this.field('Выбранная оценка',range),this.field('Заметка · необязательно',note));choose(selected);
    const save=this.button('Сохранить оценку',()=>{},'primary');save.type='submit';form.append(save,this.button('Удалить оценку',()=>{this.ratingReturn=back;this.changeTitle(item,{user_rating:null,user_note:''});}),this.button('Отмена',back));
    form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;this.ratingReturn=back;this.changeTitle(item,{user_rating:selected,user_note:note.value});};dialog.append(summary,form);
  }
  private async exportLibrary(document:unknown):Promise<void> {
    const native=(window as unknown as {pywebview?:{api?:{save_cinema_library?:(document:unknown)=>Promise<{ok:boolean;cancelled?:boolean;message?:string}>}}}).pywebview?.api;
    if(native?.save_cinema_library){const result=await native.save_cinema_library(document);if(result.ok)this.feedback.textContent='Копия медиатеки сохранена.';else if(!result.cancelled)this.feedback.textContent=result.message??'Не удалось сохранить копию.';return;}
    const blob=new Blob([JSON.stringify(document,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const link=el('a');link.href=url;link.download='chushpan-cinema.json';link.click();window.setTimeout(()=>URL.revokeObjectURL(url),1000);this.feedback.textContent='Копия медиатеки подготовлена для скачивания.';
  }
  private sourceChoices(choices:SourceOptions):void {
    const item=this.data?.items.find(v=>v.id===choices.title_id);if(!item)return;const ep=item.episodes.find(v=>v.id===choices.episode_id);if(!ep)return;
    const dialog=this.watchTarget(item,`Смотреть · ${item.title}`);dialog.append(el('p',`${episodeCaption(item,ep)} · ${playbackTime(ep.position)}`,'cinema-muted'),this.button('Найти и смотреть',()=>this.findSources(item,ep),'primary'));
    if(!choices.options.length)dialog.append(el('p','Источники пока не подключены. Можно привязать страницу официального сервиса или локальный файл.'));
    for(const option of choices.options){const row=el('article','','cinema-source-option');row.append(el('strong',option.source.name+(option.link?.id===ep.last_link_id?' · последний источник':'')+(option.link?.voice?` · ${option.link.voice}`:'')),el('p',option.reason|| (option.source.adapter==='html5'?'Попытаюсь восстановить позицию в доступном HTML5-плеере. Доступ к видео проверяется при запуске.':'Открытие вручную. Автоматический прогресс недоступен; сохрани позицию в карточке.')));if(!option.link&&option.source.enabled&&option.source.search_url)row.append(this.button('Найти на сайте ↗',()=>this.request({op:'source_search',source_id:option.source.id,query:item.title})));if(option.available&&option.link)row.append(this.button(option.source.adapter==='html5'?'Смотреть':'Открыть вручную',()=>{this.open(item,ep,option.link!.id);this.closeDialog();},'primary'));else row.append(this.button(option.source.enabled?'Привязать источник':'Настроить источник',()=>{if(option.source.enabled)this.addForm(item,undefined,ep,option.source.id);else this.sourceForm(option.source);}));dialog.append(row);}
    if(choices.provider_error)dialog.append(el('p',choices.provider_error,'cinema-muted'));
    if(choices.official){const official=choices.official;dialog.append(el('h3',`Официальные предложения · ${official.region}`),el('p',official.providers.map(p=>`${p.name} · ${{flatrate:'подписка',free:'бесплатно',ads:'с рекламой',rent:'аренда',buy:'покупка'}[p.type]??p.type}`).join(', ')||'В этом регионе предложения не найдены.'),el('p','Данные JustWatch через TMDB. Подписка, доступность и запуск видео проверяются в сервисе. Сохрани точную страницу сервиса один раз.','cinema-muted'));if(official.url)dialog.append(this.button('Открыть предложения сервисов ↗',()=>this.request({op:'provider_page',title_id:item.id})));}
    dialog.append(this.button('+ Привязать страницу или файл',()=>this.addForm(item,undefined,ep)),this.button('Настроить источники',()=>{this.closeDialog();this.changeTab('sources');}));
  }
  private watchTarget(item:CinemaTitle,title:string):HTMLElement {
    const dialog=this.showDialog(title);dialog.classList.add('cinema-compact-search');dialog.dataset.titleId=item.id;return dialog;
  }
  private findSources(item:CinemaTitle,ep=item.current,alternative=false):void {
    const dialog=this.watchTarget(item,`Ищу источники · ${item.title}`);dialog.append(el('p','Проверяю подключённые сайты. Это может занять несколько секунд.','cinema-loading'),this.button('Повторить поиск',()=>this.findSources(item,ep,alternative)));
    this.sourceRequest=`sources-${++this.sourceSequence}`;this.request({op:'find_sources',title_id:item.id,episode_id:ep?.id,alternative,request_id:this.sourceRequest});
  }
  private sourceResults(choices:SourceResults):void {
    const item=this.data?.items.find(i=>i.id===choices.title_id);const ep=item?.episodes.find(e=>e.id===choices.episode_id);if(!item||!ep)return;
    const dialog=this.watchTarget(item,`Смотреть · ${item.title}`);if(dialog.tagName==='DIALOG')dialog.classList.add('cinema-compact-search');
    dialog.append(el('p',episodeCaption(item,ep),'cinema-muted'),el('p',choices.message));
    const state=(r:SourceResult)=>r.viewing_status??(['trailer','teaser','review','clip'].includes(r.video_type??'')?'trailer':r.kind==='page'&&r.match>=.95&&(r.availability==='available'||r.previously_played)?r.content_verified?'playable':'browser_only':'unverified');
    const groups:[string,string][]=[['playable','Доступен для просмотра'],['browser_only','Доступен только в браузере · плеер требует проверки'],['region_blocked','Недоступен в регионе'],['trailer','Найден только трейлер или фрагмент'],['unverified','Не удалось проверить'],['not_found','Точная страница не найдена']];
    for(const [group,label] of groups){
      const rows=choices.results.filter(r=>state(r)===group);if(!rows.length)continue;
      const container=group==='not_found'?el('details'):el('section');container.append(el(group==='not_found'?'summary':'h3',label,'cinema-result-group'));dialog.append(container);
      for(const result of rows){
        const row=el('article','','cinema-source-option cinema-source-line');row.dataset.selected=String(result.id===choices.selected_id);row.dataset.resultId=result.id;
        const exact=['playable','browser_only'].includes(group);const body=el('div');body.append(el('strong',result.source_name),el('small',({FREE:'Бесплатный доступ подтверждён',PAID:'Требуется оплата',MIXED:'Есть бесплатный и платный контент',UNKNOWN:'Стоимость просмотра не проверена'} as Record<string,string>)[result.access_status??'UNKNOWN']),el('p',result.reason,'cinema-muted'));
        if(result.classification_reason&&result.classification_reason!==result.reason)body.append(el('small',result.classification_reason));
        if(exact)body.append(el('small',result.url,'cinema-result-url'));if(result.embedded)body.append(el('small','Встроенный плеер · совместимое прямое видео'));row.append(body);
        const evidence=el('details');evidence.append(el('summary','Что проверено'),el('p',`Страница: ${result.page_status??result.availability}; произведение: ${result.identity_status==='confirmed'?'подтверждено':'не подтверждено'}; сезон и серия: ${result.episode_status==='confirmed'?'подтверждены':'не подтверждены'}; регион: ${result.region_access==='blocked'?'ограничен':result.region_access==='allowed'?'доступен':'не проверен'}; плеер и управление: ${result.player_status==='compatible'?'совместим':'требуют проверки'}. Воспроизведение ещё не подтверждено.`));body.append(evidence);
        if(exact){
          const watch=this.button(result.identity_status==='probable'?'Открыть вероятное совпадение':!result.content_verified?'Проверить плеер в Safari':result.requires_confirmation?'Открыть…':'Смотреть',()=>this.request({op:'source_result_open',result_id:result.id,...(result.identity_status==='probable'?{player:'browser'}:{})}),'primary');watch.disabled=!this.online||(!result.allowed&&!result.manual_allowed);row.append(watch);
          if(!result.content_verified&&result.identity_status!=='probable')row.append(this.button('Открыть страницу в браузере',()=>this.request({op:'source_result_open',result_id:result.id,player:'browser'})));
          const details=el('details');details.append(el('summary','Запомнить выбор'),this.button('Запомнить источник',()=>this.request({op:'source_result_remember',result_id:result.id})));if(item.kind==='series')details.append(this.button('Закрепить за сериалом',()=>this.request({op:'source_result_remember',result_id:result.id,pin:true})));body.append(details);
        }
        container.append(row);
      }
    }
    if(!choices.results.length)dialog.append(el('p','Подходящих сохранённых ссылок нет. Проверь настройку автоматического поиска.'));
    if(item.source_memory?.pinned_source_id)dialog.append(this.button('Открепить источник',()=>this.request({op:'source_unpin',title_id:item.id})));
    dialog.append(this.button('Карточка и прогресс',()=>this.detail(item.id,ep.id)),this.button('Повторить поиск',()=>this.findSources(item,ep)),this.button('Настроить источники',()=>{this.closeDialog();this.changeTab('sources');}));
  }
  private sectionHeading(title:string,action?:HTMLElement):HTMLElement {const n=el('div','','cinema-section-heading');n.append(el('h2',title));if(action)n.append(action);return n;}
  private empty(message:string,label?:string,action?:()=>void):HTMLElement {const n=el('div','','cinema-empty');const symbol=el('span');symbol.innerHTML=icon('film');n.append(symbol,el('p',message));if(label&&action)n.append(this.button(label,action,'primary'));return n;}
  private poster(item:{title:string;poster_url:string;kind:string}):HTMLElement {const n=el('div','','cinema-poster');n.style.setProperty('--poster-hue',String([...item.title].reduce((sum,c)=>sum+c.charCodeAt(0),0)%360));if(item.poster_url){const image=el('img');image.src=item.poster_url;image.alt=`Постер: ${item.title}`;image.loading='lazy';image.decoding='async';image.referrerPolicy='no-referrer';image.onerror=()=>image.remove();n.append(image);}const placeholder=el('div','','cinema-poster-placeholder');const glyph=el('span');glyph.innerHTML=icon('film');placeholder.append(glyph,el('strong',item.title),el('small',item.kind==='series'?'СЕРИАЛ':'ФИЛЬМ'));n.append(placeholder);return n;}
  private progress(ep:CinemaEpisode|null):HTMLElement {const n=el('div','','cinema-progress');const p=el('progress');p.max=100;p.value=viewingProgress(ep);p.setAttribute('aria-label','Прогресс просмотра');n.append(p,el('span',`${Math.round(p.value)}%`));return n;}
  private card(item:CinemaTitle,wide=false):HTMLElement {
    const card=el('article','',wide?'cinema-card cinema-wide':'cinema-card');card.dataset.titleId=item.id;
    const poster=this.button(`Открыть ${item.title}`,()=>this.detail(item.id),'cinema-poster-button');poster.replaceChildren(this.poster(item));poster.setAttribute('aria-label',`Открыть ${item.title}`);
    const artwork=el('div','','cinema-poster-wrap');artwork.append(poster,posterActions(()=>this.data?.items.find(i=>i.id===item.id),values=>this.request({op:'update',id:item.id,values}),()=>this.open(this.data?.items.find(i=>i.id===item.id)??item),this.online));
    const body=el('div','','cinema-card-body');body.append(this.button(item.title,()=>this.detail(item.id),'cinema-title'),el('p',`${item.year||'Год неизвестен'} · ${item.kind==='series'?'Сериал':'Фильм'}`,'cinema-card-year'));
    const ratings=el('div','','cinema-ratings');ratings.append(el('span',item.metadata?.rating?`★ ${item.metadata.rating.toFixed(1)}`:'TMDB —','cinema-tmdb-rating'));
    if(item.user_rating)ratings.append(el('span',`★ ${item.user_rating.toFixed(1)}`,'cinema-personal-rating'));body.append(ratings,el('small',item.status==='planned'&&item.watchlist===0?'':statuses[item.status],'cinema-library-badge'));
    if(wide||item.kind==='series')body.append(el('small',episodeCaption(item)));
    if(wide||item.status==='watching'){body.append(this.progress(item.current),el('small',`${playbackTime(item.current?.position??0)} из ${item.current?.duration?playbackTime(item.current.duration):'—'}`));}
    card.append(artwork,body);return card;
  }

  private deleteTitle(item:CinemaTitle):void {const dialog=this.showDialog('Удалить из медиатеки?');dialog.append(el('p',`«${item.title}»: будут удалены прогресс и привязки этого названия.`),this.button('Удалить',()=>{this.request({op:'delete',id:item.id});this.closeDialog();}),this.button('Отмена',()=>this.closeDialog()));}
  private open(item:CinemaTitle,ep=item.current,linkId?:string,device?:string):void {
    if(linkId||device==='airplay'){this.request({op:'open',title_id:item.id,episode_id:ep?.id,link_id:linkId,device});return;}
    const target=this.watchTarget(item,`Смотреть · ${item.title}`);if(target.tagName==='DIALOG')target.classList.add('cinema-compact-search');const progress=el('p',item.kind==='series'?'Ищем сериал…':'Ищем фильм…','cinema-loading');progress.dataset.watchProgress='';progress.setAttribute('role','status');target.append(progress,el('p',episodeCaption(item,ep),'cinema-muted'),this.button('Карточка и прогресс',()=>this.detail(item.id,ep?.id)));
    this.sourceRequest=`watch-${++this.sourceSequence}`;this.request({op:'watch',title_id:item.id,episode_id:ep?.id,request_id:this.sourceRequest});
  }
  private showDialog(title:string,form=false):HTMLDialogElement {
    this.closeDialog();this.sourceRequest='';
    const dialog=el('dialog','','cinema-dialog');dialog.dataset.form=String(form);
    const scroll=this.root.closest<HTMLElement>('.workspace-page');const position=scroll?.scrollTop??0;
    const focused=document.activeElement as HTMLElement|null;
    const heading=el('header');heading.append(el('h2',title),this.button('×',()=>this.closeDialog(),'cinema-close'));
    heading.lastElementChild?.setAttribute('aria-label','Закрыть');dialog.append(heading);
    // Native backdrop clicks target the dialog; padding inside its rectangle stays open.
    let outside=false;
    const isOutside=(e:MouseEvent)=>{const r=dialog.getBoundingClientRect();return e.target===dialog&&(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom);};
    dialog.addEventListener('pointerdown',e=>{outside=isOutside(e);e.stopPropagation();});
    dialog.addEventListener('click',e=>{e.stopPropagation();if(outside&&isOutside(e))this.closeDialog();outside=false;});
    dialog.addEventListener('cancel',e=>{e.preventDefault();this.closeDialog();});
    dialog.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();this.closeDialog();}});
    dialog.addEventListener('close',()=>{dialog.remove();if(this.dialog===dialog)this.dialog=null;if(focused?.isConnected)focused.focus({preventScroll:true});if(scroll)scroll.scrollTop=position;});
    this.root.append(dialog);this.dialog=dialog;dialog.showModal();return dialog;
  }
  private closeDialog(cancelSearch=true):void {if(cancelSearch&&this.sourceRequest.startsWith('watch-'))this.request({op:'watch_cancel'});this.pendingDetailKey='';if(this.dialog?.dataset.rich==='true'||this.dialog?.dataset.rating==='true')this.request({op:'clear_focus'});this.sourceRequest='';this.dialog?.close();this.dialog?.remove();this.dialog=null;}
  private detail(id:string,episodeId?:string,linkId?:string):void {
    const item=this.data?.items.find(v=>v.id===id);if(!item)return;
    const ep=item.episodes.find(v=>v.id===episodeId)??item.current;if(!ep)return;
    const link=ep.links.find(v=>v.id===(linkId??ep.last_link_id))??ep.links[0];this.selectedLink=link?.id??null;
    const dialog=this.showDialog(item.title);dialog.dataset.titleId=id;dialog.dataset.episodeId=ep.id;this.request({op:'focus',id});const summary=el('div','','cinema-detail-summary');summary.append(this.poster(item),el('p',item.description||`${item.kind==='series'?'Сериал':'Фильм'}${item.year?` · ${item.year}`:''}. Прогресс сохраняется отдельно для каждой серии.`));dialog.append(summary,el('p',this.metadata(item),'cinema-muted'));const ratings=el('div','','cinema-ratings');ratings.append(el('span',`TMDB: ${item.metadata?.rating?.toFixed(1)??'—'}/10`,'cinema-tmdb-rating'),el('span',`Моя оценка: ${item.user_rating??'—'}/10`,'cinema-personal-rating'));dialog.append(ratings,this.detailFavorite(item));const rating=this.select([['','Без оценки'],...Array.from({length:19},(_,i)=>[String(1+i*.5),`${1+i*.5}/10`] as [string,string])],String(item.user_rating??''));rating.dataset.personalRating='';rating.onchange=()=>this.request({op:'update',id,values:{user_rating:rating.value?Number(rating.value):null}});dialog.append(this.field('Моя оценка',rating));
    const choices=el('div','','cinema-detail-choices');const seasons=[...new Set([...item.episodes.map(v=>v.season),...(item.metadata?.seasons.map(v=>v.season)??[])])].sort((a,b)=>a-b);
    if(item.kind==='series'){
      const season=this.select(seasons.map(v=>[String(v),`Сезон ${v}`]),String(ep.season));season.onchange=()=>{const loaded=item.episodes.find(v=>v.season===Number(season.value));if(loaded)this.detail(id,loaded.id);else this.request({op:'season',id,season:Number(season.value)});};
      const episode=this.select(item.episodes.filter(v=>v.season===ep.season).map(v=>[v.id,`Серия ${v.episode}${v.name?` · ${v.name}`:''}${v.watched?' · ✓':''}`]),ep.id);episode.onchange=()=>this.detail(id,episode.value);choices.append(this.field('Сезон',season),this.field('Серия',episode));if(item.catalogue_key?.startsWith('tmdb:'))choices.append(this.button('Загрузить серии сезона',()=>this.request({op:'season',id,season:Number(season.value)})));
    }
    const source=this.select([...new Map(ep.links.map(v=>[v.source_id,v.source_name])).entries()],link?.source_id??'');source.onchange=()=>this.detail(id,ep.id,ep.links.find(v=>v.source_id===source.value)?.id);
    const voice=this.select(ep.links.filter(v=>v.source_id===link?.source_id).map(v=>[v.id,v.voice||'Озвучка сайта']),link?.id??'');voice.onchange=()=>{this.selectedLink=voice.value;};choices.append(this.field('Источник',source),this.field('Озвучка',voice));dialog.append(choices);
    const status=this.select(Object.entries(statuses),item.status);status.dataset.status='';status.onchange=()=>this.request({op:'update',id,values:{status:status.value}});dialog.append(this.button('Удалить из медиатеки',()=>this.deleteTitle(item)),this.field('Статус',status),this.progress(ep),el('p',`${episodeCaption(item,ep)} · ${playbackTime(ep.position)} из ${ep.duration?playbackTime(ep.duration):'—'}`));
    const devices=this.select((this.data?.devices??[]).filter(d=>d.id==='mac'||d.id==='airplay').map(d=>[d.id,d.name+(d.available?'':' · недоступно')]),this.data?.preferences.devices[this.data.location]??'mac');const location=this.data?.location??'general';
    devices.onchange=()=>{this.request({op:'preferences',values:{devices:{...this.data?.preferences.devices,[location]:devices.value}}});reason.textContent=this.data?.devices.find(d=>d.id===devices.value)?.reason??'';};const reason=el('p',this.data?.devices.find(d=>d.id===devices.value)?.reason??'','cinema-muted');dialog.append(this.field(`Устройство · ${{home:'Дом',dorm:'Общага',general:'Общий'}[location]??location}`,devices),reason);
    const actions=el('div','','cinema-actions');actions.append(this.button('Найти и смотреть',()=>this.findSources(item,ep),'primary'),this.button('▶ Продолжить просмотр',()=>this.open(item,ep,undefined,devices.value)),this.button('Следующая серия',()=>{const next=item.episodes[item.episodes.findIndex(v=>v.id===ep.id)+1];if(next){this.detail(item.id,next.id);this.open(item,next);}else this.feedback.textContent='Следующая серия ещё не добавлена. Укажи её страницу.';}),this.button('Добавить серию / источник',()=>this.addForm(item,undefined,ep)));dialog.append(actions);
    const manual=el('details');manual.append(el('summary','Изменить прогресс вручную'));const form=el('form','','cinema-detail-choices');const position=this.input(playbackTime(ep.position));const duration=this.input(playbackTime(ep.duration));const watched=this.input('','checkbox');watched.checked=!!ep.watched;form.append(this.field('Позиция · мин:сек',position),this.field('Длительность · мин:сек',duration),this.field('Серия просмотрена',watched));const save=this.button('Сохранить прогресс',()=>{});save.type='submit';form.append(save);form.onsubmit=e=>{e.preventDefault();try{dialog.dataset.form='true';this.request({op:'progress',episode_id:ep.id,position:parsePlaybackTime(position.value),duration:parsePlaybackTime(duration.value),watched:watched.checked,link_id:this.selectedLink});}catch(error){const message=el('p',String((error as Error).message),'cinema-form-error');form.append(message);}};manual.append(form);dialog.append(manual);
    dialog.append(el('p','HTML5: доступное видео на странице или в iframe того же сайта. Чужие iframe, защита и запрет автоматизации могут потребовать ручного режима.','cinema-muted'));
    this.richDetail(dialog,item);
    if(item.catalogue_key?.startsWith('tmdb:')&&this.data?.tmdb?.configured&&!this.detailRequests.has(item.id)){this.detailRequests.add(item.id);this.request({op:'title_details',id:item.id});}
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

  private sourceCards(target:HTMLElement):void {
    this.sourceDiagnostics.mount(target);
    target.append(this.sectionHeading('Источники просмотра',this.button('+ Добавить источник',()=>this.sourceForm())));
    const all=this.data?.sources??[];
    const move=(id:string,target:number)=>{const ids=all.map(v=>v.id);const from=ids.indexOf(id);if(from<0||target<0||target>=ids.length)return;ids.splice(from,1);ids.splice(target,0,id);this.request({op:'source_order',ids});};
    const grid=el('div','','cinema-sources');
    for(const [index,source]of all.entries()){
      const card=el('article','','cinema-source-card');card.draggable=this.online;card.dataset.sourceId=source.id;
      card.ondragstart=e=>e.dataTransfer?.setData('text/plain',source.id);card.ondragover=e=>e.preventDefault();
      card.ondrop=e=>{e.preventDefault();const id=e.dataTransfer?.getData('text/plain');if(id)move(id,index);};

      const header=el('header','','cinema-source-row');const rank=el('span',String(index+1),'cinema-source-rank');rank.setAttribute('aria-label',`Приоритет ${index+1}`);
      const info=el('div');info.append(el('h3',source.name+(source.preferred?' ★':'')));const state=el('small',source.preferred?'Предпочтительный · ':'');state.append(el('span',source.enabled?({available:'Сайт отвечает · видео проверяется отдельно',unavailable:'Недоступен',restricted:'Проверка ограничена',unknown:'Ещё не проверен'}[source.health?.status??'unknown']??'Не проверен'):'Отключён'));state.dataset.state=source.enabled?source.health?.status??'unknown':'disabled';info.append(state);
      const enabled=this.input('','checkbox');enabled.checked=source.enabled;enabled.disabled=!this.online;enabled.setAttribute('aria-label',`Источник ${source.name} включён`);enabled.onchange=()=>this.request({op:'source',source:{...source,capabilities:undefined,health:undefined,enabled:enabled.checked}});const toggle=el('label','','cinema-switch');toggle.append(enabled);header.append(rank,info,toggle);
      const more=el('details','','cinema-source-more');more.append(el('summary','Подробнее и настройка'),el('p',source.base_url||'Локальные файлы','cinema-source-address'),el('p',source.capabilities?.mode??'Ручной выбор'));
      const probe=this.data?.source_diagnostics?.rows.find(row=>row.source_id===source.id);
      const status=probe?.verification_status??source.capabilities?.verification_status??'not_verified';
      info.append(el('small',({working:'Работает · подтверждено плеером',partial:'Частично поддерживается',requires_authorization:'Требует авторизации',unavailable:'Недоступен',not_verified:'Не проверен'} as Record<string,string>)[status]??'Не проверен'));
      const caps=source.capabilities;if(caps)more.append(el('p',`${caps.search?'Есть поиск':'Ручной поиск'} · ${caps.embedded?'Встроенное видео':'Просмотр на сайте'} · ${caps.episodes?'Есть серии':'Серии выбираются вручную'} · ${caps.voices?'Есть выбор озвучки':'Озвучка зависит от плеера'}`));
      if(caps?.formats?.length)more.append(el('small',`Форматы: ${caps.formats.join(', ').toUpperCase()} · проверяются при запуске`));
      if(probe?.last_successful_at)more.append(el('small',`Полная проверка плеера: ${new Date(probe.last_successful_at).toLocaleString('ru-RU')}`));
      if(probe?.error_history?.length){const errors=el('details');errors.append(el('summary','История ошибок'));for(const error of [...probe.error_history].reverse())errors.append(el('p',`${error.checked_at?new Date(error.checked_at).toLocaleString('ru-RU')+' · ':''}${error.message}`));more.append(errors);}
      if(source.health?.last_success)more.append(el('small',`Сайт отвечал: ${new Date(source.health.last_success).toLocaleString('ru-RU')}`));
      if(source.health?.reason)more.append(el('p',source.health.reason,'cinema-muted'));
      const actions=el('div','','cinema-source-actions');
      actions.append(this.button('Настроить',()=>this.sourceForm(source)),this.button('Проверить',()=>{this.request({op:'source_check',id:source.id});this.feedback.textContent='Проверяю доступность источника…';}),
        this.button(source.preferred?'Снять предпочтение':'Сделать предпочтительным',()=>this.request({op:'source',source:{...source,capabilities:undefined,health:undefined,preferred:!source.preferred}})),
        this.button('↑',()=>move(source.id,index-1)),this.button('↓',()=>move(source.id,index+1)),
        this.button('Удалить источник',()=>{const dialog=this.showDialog('Удалить источник?');dialog.append(el('p',`«${source.name}»: ссылки этого сайта будут удалены. Фильмы, оценки, история и позиции сохранятся.`),this.button('Удалить',()=>{this.request({op:'source_delete',id:source.id});this.closeDialog();}),this.button('Отмена',()=>this.closeDialog()));}));
      actions.children[3].setAttribute('aria-label',`Повысить приоритет ${source.name}`);actions.children[4].setAttribute('aria-label',`Понизить приоритет ${source.name}`);more.append(actions,el('small','Перетащи карточку, чтобы изменить порядок.'));card.append(header,more);grid.append(card);

    }
    target.append(grid);
    if(!all.length)target.append(this.empty('Добавь сайт по адресу. Поддерживаются API Internet Archive и публичные карты сайтов с проверкой совпадений. KION подключается как официальная альтернатива при автоматическом поиске.'));
    const examples=el('details');examples.append(el('summary','Добавить известный источник'),this.button('+ Internet Archive',()=>this.sourceForm(undefined,'Internet Archive','https://archive.org')),this.button('+ KION',()=>this.sourceForm(undefined,'KION','https://kion.ru')),this.button('+ Kinogo',()=>this.sourceForm(undefined,'Kinogo')),this.button('+ Filmix',()=>this.sourceForm(undefined,'Filmix')));target.append(examples,el('p','Возможности зависят от сайта. DRM, CAPTCHA, авторизация и технические ограничения остаются в сервисе.','cinema-muted'));

    const form=this.preferenceForm(target,'Выбор источника');
    this.preferenceSwitch(form,'auto_search','Автоматически искать источники',true);
    this.preferenceSwitch(form,'auto_open','Автоматически открывать найденный вариант',true);
    this.preferenceSwitch(form,'ask_paid','Спрашивать перед переходом на платный сайт',true);
    this.preferenceSwitch(form,'auto_select_source','Автоматически выбирать лучший источник',true);
    this.preferenceSwitch(form,'use_last_source','Сначала проверять последний источник',true);
    this.preferenceSwitch(form,'free_only','Только подтверждённые бесплатные источники',false);
    const region=this.input(this.data?.preferences.watch_region??'EE');region.maxLength=2;region.pattern='[A-Za-z]{2}';region.dataset.preference='watch_region';form.append(this.field('Страна поиска · EE по умолчанию',region));this.finishPreferences(form);
  }
  private tmdbIntegration(target:HTMLElement):void {
    target.append(this.button('‹ Настройки медиатеки',()=>this.changeTab('sources')));
    const integration=el('section','','cinema-settings');integration.append(el('h2','Интеграция TMDB'),el('p',this.data?.tmdb?.configured?'Ключ сохранён локально.':'TMDB ещё не подключён.','cinema-muted'));const apiForm=el('form');const credential=this.input('','password');credential.autocomplete='new-password';credential.maxLength=4096;apiForm.append(this.field('API-ключ или Read Access Token',credential),el('p','Ключ хранится в закрытом файле интеграций (доступ только владельцу), вне Git и экспорта настроек. Он не передаётся LLM.','cinema-muted'));const apiSave=this.button('Сохранить ключ',()=>{});apiSave.type='submit';apiForm.append(apiSave,this.button('Удалить ключ',()=>this.request({op:'tmdb',credential:''})));apiForm.onsubmit=e=>{e.preventDefault();if(credential.value.trim()){this.request({op:'tmdb',credential:credential.value});credential.value='';}};integration.append(apiForm);const credits=el('details');credits.append(el('summary','Об интеграции и источниках данных'));const logo=el('img');logo.src='/assets/tmdb-logo.svg';logo.alt='TMDB';logo.width=100;credits.append(logo,el('p','This product uses the TMDB API but is not endorsed or certified by TMDB.'),el('p','Предложения официальных сервисов: JustWatch через TMDB. Доступность видео проверяется отдельно.'));integration.append(credits);target.append(integration);

  }
  private preferenceForm(target:HTMLElement,title:string):HTMLFormElement {
    const section=el('section','','cinema-preference-card');section.append(el('h3',title));const form=el('form');section.append(form);target.append(section);return form;
  }
  private preferenceSwitch(form:HTMLFormElement,key:keyof CinemaPreferences,label:string,fallback:boolean):void {
    const input=this.input('','checkbox');input.checked=(this.data?.preferences[key] as boolean|undefined)??fallback;input.dataset.preference=key;const field=this.field(label,input);field.className='cinema-switch';form.append(field);
  }
  private finishPreferences(form:HTMLFormElement):void {
    const save=this.button('Сохранить настройки',()=>{},'primary');save.type='submit';form.append(save);
    form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;const values:Record<string,string|number|boolean>={};for(const input of form.querySelectorAll<HTMLInputElement|HTMLSelectElement>('[data-preference]')){const key=input.dataset.preference!;values[key]=(input as HTMLInputElement).type==='checkbox'?(input as HTMLInputElement).checked:(input as HTMLInputElement).type==='number'||key==='cards_per_row'?Number(input.value):key==='watch_region'?input.value.toUpperCase():input.value;}this.request({op:'preferences',values});};
  }
  private sources():void {
    const shell=el('section','','cinema-media-settings');const header=el('header');header.append(el('small','ТВОЙ КИНОТЕАТР'),el('h2','Настройки медиатеки'));const nav=el('nav');nav.setAttribute('aria-label','Настройки медиатеки');const pane=el('div','','cinema-settings-pane');
    for(const [key,title]of [['catalogue','Каталог'],['sources','Источники просмотра'],['playback','Воспроизведение'],['search','Поиск и Safari'],['recommendations','Рекомендации'],['privacy','Приватность и данные']]){const b=this.button(title,()=>{this.settingsSection=key;for(const button of nav.querySelectorAll('button'))button.setAttribute('aria-current',button===b?'page':'false');this.settingsPane(pane);});b.setAttribute('aria-current',this.settingsSection===key?'page':'false');nav.append(b);}shell.append(header,nav,pane);this.content.append(shell);this.settingsPane(pane);
  }
  private settingsPane(target:HTMLElement):void {
    target.replaceChildren();const prefs=this.data!.preferences;
    if(this.settingsSection==='sources'){this.sourceCards(target);return;}
    if(this.settingsSection==='search'){this.searchSettings(target);return;}
    if(this.settingsSection==='privacy'){
      const summary=el('section','','cinema-preference-card');summary.append(el('h3','Твои локальные данные'),el('p',`${this.data!.items.length} названий · ${this.data!.sources.length} источников. Оценки, заметки, история и позиции хранятся в SQLite на этом Mac.`),this.button('Посмотреть записи',()=>{const dialog=this.showDialog('Локальная медиатека');for(const item of this.data!.items.slice(0,200))dialog.append(el('p',`${item.title} · ${statuses[item.status]} · ${item.user_rating??'без оценки'}${item.user_note?' · '+item.user_note:''}`));if(this.data!.items.length>200)dialog.append(el('p','Первые 200 записей. Полная медиатека доступна в экспорте.'));}));target.append(summary);
      const cache=el('section','','cinema-preference-card');cache.append(el('h3','Кеш'),el('p','Очистка кеша сохраняет оценки, избранное, историю и ключ интеграции.','cinema-muted'),this.button('Очистить кеш TMDB',()=>this.request({op:'clear_cinema_cache',scope:'tmdb'})),this.button('Очистить кеш поиска источников',()=>this.request({op:'clear_cinema_cache',scope:'sources'})));target.append(cache);
      const backup=el('section','','cinema-preference-card');backup.append(el('h3','Копия медиатеки'),el('p','Включает заметки, историю, источники и личные ссылки. Ключ TMDB и другие данные ассистента в копию не входят. Храни файл приватно.','cinema-muted'),this.button('Экспортировать медиатеку',()=>this.request({op:'library_export'})));
      const file=this.input('','file');file.accept='.json,application/json';file.onchange=async()=>{const selected=file.files?.[0];if(!selected)return;if(selected.size>5000000){this.feedback.textContent='Выбери JSON-файл до 5 МБ.';return;}try{this.request({op:'library_import_preview',document:JSON.parse(await selected.text())});}catch{this.feedback.textContent='Не удалось прочитать копию медиатеки.';}};backup.append(this.field('Импортировать JSON-копию',file));target.append(backup);return;
    }
    const titles:Record<string,string>={catalogue:'Фильтрация каталога',playback:'Воспроизведение',recommendations:'Рекомендации и профиль'};const form=this.preferenceForm(target,titles[this.settingsSection]);
    if(this.settingsSection==='catalogue'){
      for(const [key,label,fallback]of [['russian_titles','Только названия на русском',true],['hide_animation','Скрывать мультфильмы',true],['hide_anime','Скрывать аниме',true],['hide_documentary','Скрывать документальные фильмы',false]] as [keyof CinemaPreferences,string,boolean][])this.preferenceSwitch(form,key,label,fallback);
      const sort=this.select([['popularity','По популярности'],['rating','По рейтингу TMDB'],['date','По дате выхода'],['title','По названию']],prefs.default_sort??'popularity');sort.dataset.preference='default_sort';
      const columns=this.select([['0','Автоматически'],...Array.from({length:6},(_,i)=>[String(i+3),String(i+3)])] as [string,string][],String(prefs.cards_per_row??0));columns.dataset.preference='cards_per_row';form.append(this.field('Сортировка по умолчанию',sort),this.field('Карточек в строке на большом экране',columns),el('p','Ограничения применяются к каталогу и новым рекомендациям. Личные списки сохраняются полностью. В поиске можно временно снять ограничения.','cinema-muted'));
    }else if(this.settingsSection==='playback'){
      for(const [key,label,fallback]of [['embedded_playback','Использовать встроенный плеер, если доступен',true],['allow_browser','Открывать в Safari, если встроенный плеер недоступен',true],['resume_position','Продолжать с последнего места',true],['save_progress','Автоматически сохранять прогресс',true],['auto_next','Открывать следующую добавленную серию',false]] as [keyof CinemaPreferences,string,boolean][])this.preferenceSwitch(form,key,label,fallback);
      const language=this.input(prefs.preferred_language??'ru');language.maxLength=40;language.dataset.preference='preferred_language';
      const poll=this.input(String(prefs.poll_seconds),'number');poll.min='2';poll.max='60';poll.dataset.preference='poll_seconds';
      const threshold=this.input(String(prefs.watched_threshold),'number');threshold.min='.5';threshold.max='1';threshold.step='.01';threshold.dataset.preference='watched_threshold';
      const startup=this.select([['continue','Смотрю'],['library','Хочу посмотреть']],prefs.startup);startup.dataset.preference='startup';
      form.append(this.field('Предпочитаемый язык',language),this.field('Интервал автосохранения · секунд',poll),this.field('Порог просмотренности · от 0.5 до 1',threshold),this.field('Стартовая вкладка',startup),el('p','При перезапуске видео само не запускается. Позиция сохраняется только при доступе к реальному плееру; для внешнего просмотра есть ручная коррекция.','cinema-muted'));
    }else{
      for(const [key,label,fallback]of [['personalize','Персональные рекомендации',true],['recommend_ratings','Учитывать мои оценки',true],['recommend_favorites','Учитывать избранное',true],['recommend_history','Учитывать историю просмотров',true],['recommendations_show_watched','Больше названий для повторного просмотра',false]] as [keyof CinemaPreferences,string,boolean][])this.preferenceSwitch(form,key,label,fallback);
      form.append(el('p','Локальный алгоритм использует метаданные TMDB без платных AI API. Просмотренное выделяется в отдельную подборку.','cinema-muted'),this.button('Сбросить рекомендательный профиль',()=>{const dialog=this.showDialog('Сбросить рекомендательный профиль?');dialog.append(el('p','Оценки, заметки, избранное и история сохранятся.'),this.button('Сбросить профиль',()=>{this.request({op:'recommendation_reset'});this.closeDialog();}),this.button('Отмена',()=>this.closeDialog()));}));
    }this.finishPreferences(form);
  }
  private searchSettings(target:HTMLElement):void {
    const form=this.preferenceForm(target,'Поиск и продолжение просмотра');
    for(const [key,label,fallback]of [['internet_search','Автоматический поиск в интернете',true],['prefer_free','Предпочитать бесплатные источники',true],['prefer_russian','Предпочитать русскую озвучку',true],['use_last_source','Сначала использовать сохранённый источник',true],['remember_voice','Запоминать источник и озвучку',true],['open_in_safari','Предпочитать Safari встроенному плееру',false],['autoplay','Автоматически продолжать воспроизведение',true],['save_progress','Сохранять прогресс автоматически',true],['offer_next','Предлагать следующую серию',true],['auto_next','Автоматически открывать следующую совместимую серию',false]] as [keyof CinemaPreferences,string,boolean][])this.preferenceSwitch(form,key,label,fallback);
    const rewind=this.input(String(this.data?.preferences.resume_rewind_seconds??3),'number');rewind.min='0';rewind.max='30';rewind.dataset.preference='resume_rewind_seconds';form.append(this.field('При продолжении отступать назад · секунд',rewind));this.finishPreferences(form);
    const providerForm=this.preferenceForm(target,'Поисковый провайдер');const provider=this.select([['tavily','Tavily · с бесплатным резервом Safari'],['brave','Brave · с бесплатным резервом Safari'],['safari','Safari · поиск без API-ключа']],this.data?.preferences.search_provider??'tavily');provider.dataset.preference='search_provider';providerForm.append(this.field('Провайдер интернет-поиска',provider),el('p','Проверенные сохранённые ссылки открываются без нового поиска. При недоступности выбранного API используется поиск в отдельном окне Safari без ключа. Другой платный API автоматически не вызывается.','cinema-muted'));this.finishPreferences(providerForm);
    const tavily=el('section','','cinema-preference-card');const keychain=this.data?.internet_search?.providers?.tavily;
    tavily.append(el('h3','Tavily Search API'),el('p',keychain?.state==='locked'?'Ключ сохранён, но доступ к Keychain заблокирован.':keychain?.state==='unavailable'?'macOS Keychain временно недоступен.':keychain?.configured?'Ключ сохранён в macOS Keychain.':'Добавь ключ Tavily в macOS Keychain.','cinema-muted'),el('p','Один запрос basic на поиск: русское, оригинальное и альтернативное названия, год, сезон и серия. Поиск может расходовать кредиты Tavily. Найденная страница отдельно проверяется; воспроизведение подтверждается в Safari.','cinema-muted'));
    const tavilyForm=el('form');const tavilyKey=this.input('','password');tavilyKey.autocomplete='new-password';tavilyKey.maxLength=512;tavilyKey.required=true;tavilyKey.spellcheck=false;tavilyForm.append(this.field('Ключ Tavily API',tavilyKey));const tavilySave=this.button('Сохранить ключ Tavily',()=>{},'primary');tavilySave.type='submit';tavilyForm.append(tavilySave,this.button('Удалить ключ Tavily',()=>{tavilyKey.value='';this.request({op:'tavily_credentials',credential:''});}));tavilyForm.onsubmit=e=>{e.preventDefault();if(!tavilyForm.reportValidity())return;const value=tavilyKey.value.trim();tavilyKey.value='';this.request({op:'tavily_credentials',credential:value});};tavily.append(tavilyForm);
    const connection=el('p','Проверка подключения ещё не выполнена.','cinema-muted');connection.dataset.tavilyConnection='';connection.setAttribute('role','status');tavily.append(this.button('Проверить подключение',()=>{connection.textContent='Проверяем ключ Tavily…';this.request({op:'tavily_check'});}),connection,el('p','Проверка использует /usage без поискового запроса. Ключ не возвращается в интерфейс, не попадает в Git, логи или экспорт медиатеки. macOS может запросить доступ к Связке ключей.','cinema-muted'));target.append(tavily);
    const optional=el('details','','cinema-preference-card');optional.append(el('summary','Brave Search API · необязательно'));const api=el('div');api.append(el('p',this.data?.internet_search?.providers?.brave?.configured?'Ключ Brave сохранён локально.':'Ключ Brave не подключён.','cinema-muted'));
    const credentials=el('form');const secret=this.input('','password');secret.autocomplete='new-password';secret.maxLength=512;secret.required=true;credentials.append(this.field('Ключ Brave Search API',secret));const save=this.button('Сохранить ключ Brave',()=>{},'primary');save.type='submit';credentials.append(save,this.button('Удалить ключ Brave',()=>{secret.value='';this.request({op:'search_credentials',credential:''});}));credentials.onsubmit=e=>{e.preventDefault();if(!credentials.reportValidity())return;const value=secret.value.trim();secret.value='';this.request({op:'search_credentials',credential:value});};api.append(credentials,el('p','Существующий ключ Brave и его настройки сохранены. Для использования выбери Brave выше. Ключ остаётся в закрытом файле интеграций, вне Git и экспорта.','cinema-muted'));optional.append(api);target.append(optional);
    const safari=el('section','','cinema-preference-card');safari.append(el('h3','Разрешения Safari'),el('p','Используется поддерживаемый мост Apple Events. Safari Web Extension Чушпана не установлено; разрешения расширения не запрашиваются.','cinema-muted'),el('p','Разреши Чушпану Автоматизация → Safari. В Safari: Настройки → Дополнения → Показывать функции для веб-разработчиков, затем Разработчик → Разрешить JavaScript из событий Apple. Автовоспроизведение разрешай только нужному сайту.','cinema-muted'));
    const status=el('p','Проверка ещё не выполнена.','cinema-muted');status.dataset.safariStatus='';status.setAttribute('role','status');safari.append(status,this.button('Проверить интеграцию с Safari',()=>this.request({op:'safari_check'})),this.button('Запросить доступ к Safari',()=>this.request({op:'safari_access'})));target.append(safari);
    const diagnostics=el('details','','cinema-preference-card');diagnostics.append(el('summary','Диагностика поиска и воспроизведения'),el('p','SEARCH → VERIFY → OPEN_SAFARI → DETECT_PLAYER → RESTORE_POSITION → PLAY → SAVE_PROGRESS','cinema-muted'),this.button('Показать последние этапы',()=>this.request({op:'cinema_diagnostics'})));const log=el('div','','cinema-diagnostic-log');log.dataset.cinemaDiagnostics='';log.setAttribute('aria-live','polite');diagnostics.append(log);const copyStatus=el('span');diagnostics.append(this.button('Копировать этапы',()=>{void copyDiagnostic(this.data?.cinema_diagnostics??log.textContent,copyStatus);}),copyStatus);target.append(diagnostics,browserDiagnosticsPanel(this.data?.browser_diagnostics,this.data?.sources??[],this.data?.items??[],v=>this.request(v),this.online));
  }
  private sourceForm(existing?:CinemaSource,preset='',presetURL=''):void {
    const dialog=this.showDialog(existing?'Настроить источник':'Новый источник',true);const form=el('form');
    const name=this.input(existing?.name??preset);name.maxLength=80;
    const base=this.input(existing?.base_url??presetURL,'url');base.required=existing?.adapter!=='local';base.placeholder='https://адрес-сайта';
    const search=this.input(existing?.search_url??'');search.placeholder='Необязательно · адрес поиска с {query}';
    const category=this.select([['site','Сайт'],['streaming','Официальный стриминг'],['local','Локальные видеофайлы']],existing?.category??'site');
    const adapter=this.select([['manual','Ручной · открытие страницы'],['html5','HTML5 · управление через Safari'],['direct','Прямое видео · встроенный плеер'],['local','Локальный файл · внешний плеер']],existing?.adapter??(presetURL==='https://archive.org'?'direct':'manual'));
    const mode=this.select([['auto','Определить автоматически'],['manual','Ручной выбор на сайте'],['archive','Официальный API Internet Archive'],['sitemap','Публичная карта сайта · с проверкой robots.txt']],existing?.search_mode==='manual'?'auto':existing?.search_mode??'auto');
    const embed=this.input('','checkbox');embed.checked=existing?.embed_allowed??(presetURL==='https://archive.org');
    const updateLocal=()=>{const local=adapter.value==='local';base.required=!local;base.disabled=local;search.disabled=local;mode.disabled=local;embed.required=adapter.value==='direct';if(local){base.value='';search.value='';category.value='local';}else if(category.value==='local')category.value='site';};
    adapter.onchange=updateLocal;category.onchange=()=>{adapter.value=category.value==='local'?'local':'manual';updateLocal();};updateLocal();
    const enabled=this.input('','checkbox');enabled.checked=existing?.enabled??true;
    const preferred=this.input('','checkbox');preferred.checked=existing?.preferred??false;
    const language=this.input(existing?.language??'');language.maxLength=40;language.placeholder='ru, en…';
    const access=this.select([['UNKNOWN','UNKNOWN · условия не определены'],['FREE','FREE · бесплатный доступ подтверждён'],['PAID','PAID · требуется оплата'],['MIXED','MIXED · бесплатный и платный контент']],existing?.access_status??'UNKNOWN');
    form.append(this.field('Адрес сайта',base),this.field('Название · можно оставить пустым',name),this.field('Тип источника',category),this.field('Поиск на сайте',mode),this.field('Адрес поисковой страницы · необязательно',search),this.field('Способ просмотра',adapter),this.field('Источник разрешает встраивание прямого видео',embed),this.field('Язык источника',language),this.field('Тип доступа · если известен',access),this.field('Предпочтительный источник',preferred),this.field('Источник включён',enabled),el('p','Открытые фильмы Sintel и эпизоды Caminandes проверяются через API Internet Archive по названию, автору, лицензии и конкретному файлу. Это не подтверждает все записи Archive. Прямое видео работает только с разрешённым совместимым потоком. Для HTML5 через Safari нужны разрешения Автоматизации и JavaScript из событий Apple. Карты сайта проверяются с учётом robots.txt. Неизвестная стоимость не означает бесплатный доступ.','cinema-muted'));
    const save=this.button('Сохранить источник',()=>{},'primary');save.type='submit';form.append(save);
    form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;const host=base.value?new URL(base.value).hostname:'';const searchMode=adapter.value==='local'?'manual':mode.value==='auto'?(host==='archive.org'?'archive':['kion.ru','filmix.gg','kinogo.is','hdrezka.sh'].includes(host)?'sitemap':'manual'):mode.value;
      this.request({op:'source',source:{id:existing?.id??'',name:name.value.trim()||host||'Мои файлы',base_url:base.value,search_url:search.value||(searchMode==='archive'?'https://archive.org/search?query={query}':''),adapter:adapter.value,category:category.value,enabled:enabled.checked,priority:existing?.priority??this.data?.sources.length??0,preferred:preferred.checked,search_mode:searchMode,embed_allowed:embed.checked,language:language.value,access_status:access.value,access_type:'unknown'}});};dialog.append(form);
  }
  private history(target:HTMLElement=this.content):void {
    target.append(this.sectionHeading('История просмотра'));const toolbar=el('div','','cinema-filters');const source=this.select([['all','Все источники'],...(this.data?.sources.map(s=>[s.name,s.name] as [string,string])??[])],this.sourceFilter);source.setAttribute('aria-label','Источник в истории');source.onchange=()=>{this.sourceFilter=source.value;target.replaceChildren();this.history(target);};const date=this.input(this.dateFilter,'date');date.setAttribute('aria-label','Дата просмотра');date.onchange=()=>{this.dateFilter=date.value;target.replaceChildren();this.history(target);};toolbar.append(source,date);target.append(toolbar);
    const rows=this.data!.history.filter(row=>(this.sourceFilter==='all'||row.source_name===this.sourceFilter)&&row.title.toLocaleLowerCase('ru').includes(this.query.toLocaleLowerCase('ru'))&&(!this.dateFilter||new Date(row.created_at).toLocaleDateString('sv-SE')===this.dateFilter));
    if(!rows.length)target.append(this.empty('Истории по выбранным фильтрам пока нет.'));
    const list=el('div','','cinema-history');for(const row of rows){const card=el('article');const info=el('div');info.append(this.button(row.title,()=>this.detail(row.title_id,row.episode_id),'cinema-title'),el('p',`${row.season?`Сезон ${row.season} · Серия ${row.episode} · `:''}${row.source_name} · ${events[row.event]??row.event}`));card.append(info,el('span',`${playbackTime(row.position)} / ${row.duration?playbackTime(row.duration):'—'}`),el('time',new Date(row.created_at).toLocaleString('ru-RU',{dateStyle:'short',timeStyle:'short'})));list.append(card);}target.append(list);
  }
}

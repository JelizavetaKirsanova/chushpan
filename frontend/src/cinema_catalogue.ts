import type {CatalogueTitle, CinemaEvent, CinemaTitle} from './cinema';
import {CinemaFilterDrawer,activeFilterCount} from './cinema_filters';
import {posterActions,syncPosterActions} from './cinema_actions';

export interface CatalogueFilters {
  feed:string;query:string;kind:string;genre:string;genres:string[];genre_mode:'or'|'and';year:number;rating:number;
  country:string;original_language:string;sort:string;year_min:number;year_max:number;russian_titles?:boolean;hide_animation?:boolean;hide_anime?:boolean;hide_documentary?:boolean;relax_restrictions:boolean;
}
export interface CataloguePage {
  request_id:string;feed?:string;page?:number;items?:CatalogueTitle[];next_page?:number|null;
  total_pages?:number;total_results?:number;sort_scope?:string;filtered_total?:boolean;
  scanned_through?:number;empty_reason?:string|null;unavailable_genres?:string[];
  message?:string;error?:string;retry_after?:number;
}
export interface CatalogueConfig {
  feeds:{id:string;name:string}[];genres:string[];countries:{id:string;name:string}[];
  languages:{id:string;name:string}[];
  genres_by_kind?:{movie:string[];series:string[]};
}
export const catalogueFeeds:[string,string][]=[
  ['trending','Сейчас в тренде'],['popular_movies','Популярные фильмы'],
  ['popular_series','Популярные сериалы'],['new','Новинки'],['upcoming','Скоро выйдут'],
  ['top','Лучшие по рейтингу'],['scifi','Фантастика'],['comedy','Комедии'],
  ['horror','Ужасы'],['thriller','Триллеры'],['mystery','Детективы'],
  ['drama','Драмы'],['animation','Мультфильмы'],['recommended','Рекомендовано для меня'],
];
const blankFilters=():CatalogueFilters=>({feed:'browse',query:'',kind:'all',genre:'',genres:[],genre_mode:'or',year:0,rating:0,country:'',original_language:'',sort:'popularity',year_min:0,year_max:0,relax_restrictions:false});
const node=<K extends keyof HTMLElementTagNameMap>(tag:K,text='',cls=''):HTMLElementTagNameMap[K]=>{const e=document.createElement(tag);e.textContent=text;e.className=cls;return e;};
const statuses:Record<string,string>={planned:'Хочу посмотреть',watching:'Смотрю',watched:'Просмотрено'};

/** Keep the order of accepted pages. Loading a page never re-sorts earlier cards. */
export class CatalogueWindow {
  readonly pages=new Map<number,CataloguePage>();
  readonly pending=new Map<number,string>();
  generation=0;
  private sequence=0;
  next:number|null=1;
  error='';
  retryAt=0;
  failedPage:number|null=null;
  emptyPages=0;
  reset():void {this.generation++;this.pages.clear();this.pending.clear();this.next=1;this.error='';this.retryAt=0;this.failedPage=null;this.emptyPages=0;}
  request(page:number):string|null {
    if(this.pending.has(page)||this.pages.has(page)||Date.now()<this.retryAt)return null;
    const id=`catalogue-${this.generation}-${++this.sequence}`;this.pending.set(page,id);this.error='';return id;
  }
  accept(event:CataloguePage):boolean {
    const page=[...this.pending].find(([,id])=>id===event.request_id)?.[0];
    if(page===undefined)return false;
    this.pending.delete(page);
    if(event.error){this.failedPage=page;this.error=event.error;this.retryAt=Date.now()+(event.retry_after??0)*1000;return true;}
    this.failedPage=null;this.pages.set(page,event);
    const last=this.pages.get(Math.max(...this.pages.keys()))!;
    this.next=last.next_page??null;
    this.emptyPages=event.items?.length?0:this.emptyPages+1;
    return true;
  }
  items():CatalogueTitle[] {
    const unique=new Map<string,CatalogueTitle>();
    for(const [,page] of [...this.pages].sort(([a],[b])=>a-b))for(const item of page.items??[])if(!unique.has(item.catalogue_key))unique.set(item.catalogue_key,item);
    return [...unique.values()];
  }
}

interface Callbacks {
  send:(value:object)=>void;detail:(item:CatalogueTitle)=>void;watch:(item:CatalogueTitle)=>void;
  change:(item:CatalogueTitle,values:object)=>void;remove:(item:CinemaTitle)=>void;
  poster:(item:CatalogueTitle)=>HTMLElement;settings:()=>void;query?:(value:string)=>void;
}
interface Preview {items:CatalogueTitle[];loading:boolean;loaded:boolean;error:string;retryAt:number;message:string;request?:string}

export class CinemaCatalogueView {
  private root=node('div','','cinema-catalogue');
  private filters=blankFilters();
  private data:CinemaEvent|null=null;
  private config:CatalogueConfig|null=null;
  private configured=false;
  private configRequested=false;
  private configError='';
  private online=false;
  private mode:'home'|'list'='list';
  private previews=new Map<string,Preview>();
  private window=new CatalogueWindow();
  private previewSequence=0;
  private debounce:number|undefined;
  private retryTimer:number|undefined;
  private observer:IntersectionObserver|null=null;
  private rows=new Map<string,HTMLElement>();
  private results=node('div','','cinema-grid cinema-catalogue-grid');
  private footer=node('div','','cinema-catalogue-footer');
  private heading=node('div','','cinema-catalogue-heading');
  private sentinel=node('div','','cinema-sentinel');
  private configSlot=node('div');
  private drawer=new CinemaFilterDrawer();
  private librarySignature='';
  private mounted=false;
  constructor(private callbacks:Callbacks) {}
  connection(online:boolean):void {
    this.online=online;
    if(!online){this.window.pending.clear();for(const p of this.previews.values()){p.loading=false;p.request=undefined;}this.configRequested=false;window.clearTimeout(this.debounce);this.debounce=undefined;}
    if(this.root.isConnected){this.render();this.start();}
    this.root.querySelectorAll<HTMLButtonElement>('.cinema-poster-actions button').forEach(button=>button.disabled=!online);
  }
  receive(data:CinemaEvent):void {
    const previous=this.data;
    this.data=data;this.configured=!!data.tmdb?.configured;
    const prefs=data.preferences;
    if(!previous&&prefs.catalogue_filters)this.filters={...this.filters,...prefs.catalogue_filters};
    const needsPolicyReset=['russian_titles','hide_animation','hide_anime','hide_documentary'].some(key=>(this.filters as any)[key]!==((prefs as any)[key]??(key!=='hide_documentary')))||!!previous&&previous.preferences.default_sort!==prefs.default_sort;
    const policyChanged=previous&&['russian_titles','hide_animation','hide_anime','hide_documentary','default_sort'].some(key=>(previous.preferences as any)[key]!==((prefs as any)[key]));
    if(!previous||policyChanged){
      for(const key of ['russian_titles','hide_animation','hide_anime','hide_documentary'] as const)this.filters[key]=prefs[key]??(key!=='hide_documentary');
      if(!previous||previous.preferences.default_sort!==prefs.default_sort)this.filters.sort=prefs.default_sort??'popularity';
      if(policyChanged&&needsPolicyReset){this.previews.clear();this.window.reset();if(this.root.isConnected){this.renderFilters();this.renderBody();this.start();}}
    }
    this.root.style.setProperty('--cinema-columns',prefs.cards_per_row?String(prefs.cards_per_row):'');this.root.dataset.columns=String(prefs.cards_per_row??0);

    if(data.catalogue_config){this.config=data.catalogue_config;this.configError='';this.renderFilters();}
    if(data.catalogue_config_error){this.configError=data.catalogue_config_error;this.configRequested=false;this.renderFilters();}
    const page=data.catalogue_page;
    if(page){
      const preview=[...this.previews.values()].find(p=>p.request===page.request_id);
      if(preview){preview.loading=false;preview.request=undefined;preview.error=page.error??'';preview.loaded=!page.error;preview.retryAt=Date.now()+(page.retry_after??0)*1000;preview.items=(page.items??[]).slice(0,12);preview.message=page.message??'';this.renderPreviews();}
      else if(this.window.accept(page)){this.renderList();if(!page.error)this.observe();}
    }
    if(!previous||previous.tmdb?.configured!==data.tmdb?.configured){this.previews.clear();this.window.reset();this.configRequested=false;if(this.root.isConnected){this.render();this.start();}if(!previous&&data.matches?.length)this.legacy(data.matches);}
    const signature=JSON.stringify(data.items.map(i=>[i.id,i.status,i.watchlist,i.favorite,i.user_rating]));
    if(signature!==this.librarySignature){this.librarySignature=signature;if(this.root.isConnected){this.renderPreviews();this.renderList();}}
    if(previous&&previous.preferences.recommendations_show_watched!==data.preferences.recommendations_show_watched){this.previews.delete('recommended');if(this.filters.feed==='recommended'){this.window.reset();this.loadPage(1);}this.renderPreviews();this.observe();}
  }
  mount(parent:HTMLElement):void {
    if(this.root.parentElement!==parent){parent.replaceChildren(this.root);if(!this.mounted){this.render();this.mounted=true;}this.renderPreviews();this.renderList();this.start();this.observe();}
  }
  search(query:string,debounce=true):void {
    window.clearTimeout(this.debounce);this.debounce=undefined;this.filters.query=query.trim();this.filters.feed='browse';this.mode='list';this.window.reset();
    if(this.root.isConnected)this.render();
    const run=()=>{this.debounce=undefined;if(this.mode==='list')this.loadPage(1);else this.start();};
    if(debounce)this.debounce=window.setTimeout(run,350);else run();
  }
  legacy(matches:CatalogueTitle[]):void {
    // Voice recommendations still use the original cinema snapshot contract.
    this.mode='list';this.filters=blankFilters();this.window.reset();
    const id=this.window.request(1)!;this.window.accept({request_id:id,page:1,items:matches,next_page:null,total_pages:1});
    if(this.root.isConnected)this.render();
  }
  private start():void {
    if(!this.online||!this.configured||!this.root.isConnected)return;
    if(!this.configRequested&&!this.config){this.configRequested=true;this.callbacks.send({op:'catalogue_config'});}
    if(this.mode==='home'){this.loadPreview('trending');this.observe();}
    else if(this.debounce===undefined&&!this.window.pages.size&&!this.window.pending.size&&!this.window.error)this.loadPage(1);
  }
  intent(value:{query?:string;genre?:string}):void {this.filters={...this.defaults(),...value,genres:value.genre?[value.genre]:[],genre:''};this.callbacks.query?.(this.filters.query);this.mode='list';this.window.reset();if(this.root.isConnected){this.render();this.loadPage(1);}}
  private button(label:string,action:()=>void,cls=''):HTMLButtonElement {const b=node('button',label,cls);b.type='button';b.disabled=!this.online;b.onclick=action;return b;}
  private select(label:string,options:[string,string][],value:string,change:(value:string)=>void):HTMLElement {
    const field=node('label','','cinema-field');field.append(node('span',label));const input=node('select');input.setAttribute('aria-label',label);input.disabled=!this.online;
    for(const [id,name]of options){const option=node('option',name);option.value=id;input.append(option);}input.value=value;input.onchange=()=>change(input.value);field.append(input);return field;
  }
  private saveFilters():void {const f=this.filters;const catalogue_filters={kind:f.kind,genres:f.genres,genre_mode:f.genre_mode,year:f.year,year_min:f.year_min,year_max:f.year_max,rating:f.rating,country:f.country,original_language:f.original_language};if(JSON.stringify(this.data?.preferences.catalogue_filters)!==JSON.stringify(catalogue_filters))this.callbacks.send({op:'preferences',values:{catalogue_filters}});}
  private changed():void {this.saveFilters();window.clearTimeout(this.debounce);this.mode='list';this.window.reset();this.renderBody();this.debounce=window.setTimeout(()=>{this.debounce=undefined;this.loadPage(1);},300);}

  private defaults():CatalogueFilters {const p=this.data?.preferences;return {...blankFilters(),sort:p?.default_sort??'popularity',russian_titles:p?.russian_titles??true,hide_animation:p?.hide_animation??true,hide_anime:p?.hide_anime??true,hide_documentary:p?.hide_documentary??false};}
  private applyFilters(filters:CatalogueFilters):void {
    this.filters=filters;this.saveFilters();
    const values:Record<string,boolean>={};for(const key of ['russian_titles','hide_animation','hide_anime','hide_documentary'] as const)if(filters[key]!==this.data?.preferences[key])values[key]=filters[key]??(key!=='hide_documentary');
    if(Object.keys(values).length)this.callbacks.send({op:'preferences',values});
    window.clearTimeout(this.debounce);this.debounce=undefined;this.mode='list';this.window.reset();this.renderFilters();this.renderBody();this.loadPage(1);const scroll=this.root.closest<HTMLElement>('.workspace-page');if(scroll)scroll.scrollTop=0;
  }
  openFilters():void {if(this.online)this.drawer.open(this.root,this.filters,this.config,()=>this.defaults(),filters=>this.applyFilters(filters));}
  private renderFilters():void {
    const form=node('form','','cinema-catalogue-filters');
    const types=node('div','','cinema-type-switch');types.setAttribute('aria-label','Тип контента');
    for(const [id,label]of [['all','Всё'],['movie','Фильмы'],['series','Сериалы']]){const b=this.button(label,()=>{this.filters.kind=id;this.changed();this.renderFilters();});b.setAttribute('aria-pressed',String(this.filters.kind===id));types.append(b);}
    form.append(types,this.select('Сортировка',[['popularity','По популярности'],['rating','По рейтингу'],['date','По дате выхода'],['title','По названию']],this.filters.sort,v=>{this.filters.sort=v;this.changed();}));
    const open=this.button('Фильтры',()=>this.drawer.open(this.root,this.filters,this.config,()=>this.defaults(),filters=>this.applyFilters(filters)),'cinema-filter-trigger');open.append(node('span',String(activeFilterCount(this.filters)),'cinema-filter-count'));open.setAttribute('aria-label',`Фильтры · ${activeFilterCount(this.filters)}`);
    form.append(open,this.button('Подборки',()=>{this.mode='home';this.render();this.start();}));
    const submit=this.button('Найти',()=>{},'primary');submit.type='submit';form.append(submit);form.onsubmit=e=>{e.preventDefault();this.applyFilters({...this.filters});};
    this.configSlot.replaceChildren(form,node('div','','cinema-genre-chips'));this.renderGenreChips();
    if(this.configError)this.configSlot.append(node('p',this.configError,'cinema-muted'),this.button('Повторить справочники',()=>this.callbacks.send({op:'catalogue_config'})));
  }
  private renderGenreChips():void {
    const chips=this.configSlot.querySelector('.cinema-genre-chips');if(!chips)return;chips.replaceChildren();
    const remove=(label:string,fn:()=>void)=>chips.append(this.button(label+' ×',()=>{fn();this.applyFilters({...this.filters});},'cinema-genre-chip'));
    for(const genre of this.filters.genres)remove(genre,()=>this.filters.genres=this.filters.genres.filter(g=>g!==genre));
    for(const [key,label]of [['russian_titles','Русские названия'],['hide_animation','Без мультфильмов'],['hide_anime','Без аниме'],['hide_documentary','Без документальных']] as const)if(this.filters[key])remove(label,()=>this.filters[key]=false);
    if(this.filters.year_min||this.filters.year_max)remove(`${this.filters.year_min||'…'}–${this.filters.year_max||'…'}`,()=>{this.filters.year_min=0;this.filters.year_max=0;});
    if(this.filters.rating)remove(`Рейтинг от ${this.filters.rating}`,()=>this.filters.rating=0);
    if(this.filters.country)remove(this.filters.country,()=>this.filters.country='');
    if(this.filters.relax_restrictions)remove('Поиск без ограничений',()=>this.filters.relax_restrictions=false);
  }
  private render():void {
    this.observer?.disconnect();this.rows.clear();this.root.replaceChildren();
    const hero=node('header','','cinema-catalogue-hero');hero.append(node('small','ВЫБЕРИ СВОЮ ИСТОРИЮ'),node('h2','Что посмотрим сегодня?'),node('p','Открывай новое и собирай свою медиатеку.'));
    const actions=node('div','','cinema-actions');actions.append(this.button('Весь каталог',()=>this.openFeed('browse')),this.button('Для меня',()=>this.openFeed('recommended')));hero.append(actions);this.root.append(hero,this.configSlot);this.renderFilters();this.renderBody();
  }
  private renderBody():void {
    this.root.dataset.mode=this.mode;
    this.root.querySelector('.cinema-catalogue-body')?.remove();const body=node('div','','cinema-catalogue-body');this.root.append(body);this.observer?.disconnect();this.rows.clear();
    if(!this.configured){body.append(node('p','Подключи TMDB для поиска и подборок. Личная медиатека остаётся доступна.','cinema-empty'),this.button('Подключить TMDB',this.callbacks.settings));return;}
    if(this.mode==='home'){
      for(const [id,title]of catalogueFeeds.filter(([id])=>id!=='animation'||this.data?.preferences.hide_animation===false)){const section=node('section','','cinema-shelf');section.dataset.feed=id;const heading=node('header','','cinema-section-heading');const arrows=node('div','','cinema-shelf-controls');const track=node('div','','cinema-carousel');track.setAttribute('aria-label',title);track.tabIndex=0;
        arrows.append(this.button('‹',()=>track.scrollBy({left:-track.clientWidth*.8,behavior:'smooth'})),this.button('›',()=>track.scrollBy({left:track.clientWidth*.8,behavior:'smooth'})),this.button('Смотреть всё',()=>this.openFeed(id)));arrows.children[0]?.setAttribute('aria-label',`Назад: ${title}`);arrows.children[1]?.setAttribute('aria-label',`Дальше: ${title}`);heading.append(node('h2',title),arrows);section.append(heading,node('p','','cinema-shelf-message'),track);body.append(section);this.rows.set(id,section);}
      this.renderPreviews();this.observe();
    }else{body.append(this.heading,this.results,this.footer,this.sentinel);this.renderList();this.observe();}
    const credit=node('p','','cinema-muted cinema-catalogue-credit');const logo=node('img');logo.src='/assets/tmdb-logo.svg';logo.alt='TMDB';logo.width=100;credit.append(logo,node('span','This product uses the TMDB API but is not endorsed or certified by TMDB. Метаданные не подтверждают доступность видео.'));body.append(credit);
    const sites=this.data?.sources.filter(s=>s.enabled&&s.search_url)??[];if(sites.length){const details=node('details','','cinema-source-search');details.append(node('summary','Поиск на подключённых сайтах'));for(const source of sites)details.append(this.button(`Искать в ${source.name} ↗`,()=>this.callbacks.send({op:'source_search',source_id:source.id,query:this.filters.query})));body.append(details);}
  }
  private openFeed(feed:string):void {window.clearTimeout(this.debounce);this.debounce=undefined;this.filters={...this.filters,feed,sort:feed==='top'||feed==='recommended'?'rating':this.filters.sort};if(feed!=='browse'){this.filters.query='';this.callbacks.query?.('');}this.mode='list';this.window.reset();this.renderFilters();this.renderBody();this.loadPage(1);this.heading.scrollIntoView({block:'start',behavior:'smooth'});}
  private loadPreview(feed:string):void {
    if(!this.online||!this.configured||this.mode!=='home'||!this.root.isConnected)return;
    const state=this.previews.get(feed)??{items:[],loading:false,loaded:false,error:'',retryAt:0,message:''};
    if(state.loading||state.loaded||Date.now()<state.retryAt)return;
    if([...this.previews.values()].filter(p=>p.loading).length>=2)return;
    state.loading=true;state.error='';state.request=`preview-${++this.previewSequence}`;this.previews.set(feed,state);this.renderPreviews();
    this.callbacks.send({op:'catalogue_page',request_id:state.request,filters:{...blankFilters(),feed,sort:feed==='top'||feed==='recommended'?'rating':'popularity'}});
  }
  private loadPage(page:number,automatic=false):void {
    if(!this.online||!this.configured||this.mode!=='list'||!this.root.isConnected||this.debounce!==undefined)return;
    if(automatic&&(this.window.error||this.window.emptyPages>=3||this.window.pending.size))return;
    const id=this.window.request(page);if(!id)return;
    this.renderList();this.callbacks.send({op:'catalogue_page',request_id:id,filters:{...this.filters,page}});
  }
  private observe():void {
    this.observer?.disconnect();
    if(typeof window.IntersectionObserver!=='function')return;
    this.observer=new window.IntersectionObserver(entries=>{
      for(const entry of entries)if(entry.isIntersecting){const feed=(entry.target as HTMLElement).dataset.feed;if(feed)this.loadPreview(feed);else if(this.window.next)this.loadPage(this.window.next,true);}
    },{root:this.root.closest('.workspace-page'),rootMargin:'650px 0px'});
    if(this.mode==='home'){for(const [id,row]of this.rows)if(!this.previews.get(id)?.loaded&&!this.previews.get(id)?.loading&&!this.previews.get(id)?.error)this.observer.observe(row);}
    else if(this.window.next&&!this.window.error&&this.window.emptyPages<3&&!this.window.pending.size)this.observer.observe(this.sentinel);
  }
  private renderPreviews():void {
    if(this.mode!=='home')return;
    for(const [id,section]of this.rows){const state=this.previews.get(id);const track=section.querySelector<HTMLElement>('.cinema-carousel')!;const offset=track.scrollLeft;track.setAttribute('aria-busy',String(!!state?.loading));
      const message=section.querySelector<HTMLElement>('.cinema-shelf-message')!;message.textContent=state?.message??'';
      if(state?.loaded){this.reconcile(track,this.recommendationFilter(state.items,id));if(!track.childElementCount)track.append(node('p','В этой подборке пока ничего не найдено.','cinema-muted'));}
      else if(state?.error){track.replaceChildren();track.append(node('p',state.error,'cinema-form-error'),this.button('Повторить',()=>{if(state){state.error='';this.loadPreview(id);this.observe();}}));}
      else{track.replaceChildren();for(let i=0;i<5;i++)track.append(node('div','','cinema-poster-skeleton'));if(!state?.loading)track.append(this.button('Загрузить подборку',()=>this.loadPreview(id)));}
      track.scrollLeft=offset;
    }
    // Once one visible shelf settles, observe shelves that were held by the concurrency limit.
    this.observe();
  }
  private recommendationFilter(items:CatalogueTitle[],feed=this.filters.feed):CatalogueTitle[] {
    if(feed!=='recommended')return items;
    const watched=this.data?.items.filter(i=>i.status==='watched')??[];
    const normalized=(value:string)=>value.toLocaleLowerCase('ru').trim().replace(/\s+/g,' ');
    return items.filter(item=>!watched.some(seen=>seen.catalogue_key===item.catalogue_key||
      (seen.kind===item.kind&&(!seen.year||seen.year===item.year)&&normalized(seen.title)===normalized(item.title))));
  }
  private renderList():void {
    if(this.mode!=='list'||!this.root.contains(this.results))return;
    const items=this.recommendationFilter(this.window.items());
    this.reconcile(this.results,items);
    const title=this.filters.query?`Поиск: ${this.filters.query}`:catalogueFeeds.find(([id])=>id===this.filters.feed)?.[1]??'Весь каталог';this.heading.replaceChildren(this.button('‹ Подборки',()=>{this.mode='home';this.filters=blankFilters();this.callbacks.query?.('');this.window.reset();this.render();this.start();}),node('h2',title));
    const pages=[...this.window.pages.keys()].sort((a,b)=>a-b);const last=pages.length?this.window.pages.get(pages.at(-1)!)!:null;
    if(pages.length){this.heading.append(node('p',`Проверены страницы ${pages[0]}–${last?.scanned_through??pages.at(-1)}${last?.total_pages?` из ${last.total_pages}`:''} · Показано: ${items.length}${last?.filtered_total&&last.total_results!==undefined?` из ${last.total_results}`:''}`,'cinema-muted'));if(pages[0]!>1)this.heading.append(this.button('Предыдущая страница',()=>this.loadPage(pages[0]!-1)));}
    if(last?.unavailable_genres?.length)this.heading.append(node('p',`Для выбранного типа нет жанров: ${last.unavailable_genres.join(', ')}. Фильтры сохранены; выбери другой тип или измени жанры.`,'cinema-muted'));
    if(last?.message)this.heading.append(node('p',last.message,'cinema-muted'));
    if(last&&!items.length&&!this.window.error){const f=this.filters;const constraints=[...f.genres,f.russian_titles?'названия с русскими буквами':'',f.hide_animation?'без мультфильмов':'',f.hide_anime?'без аниме':'',f.hide_documentary?'без документальных':'',f.rating?`рейтинг от ${f.rating}`:'',f.country,f.original_language,f.year||f.year_min||f.year_max?'диапазон годов':''].filter(Boolean);if(constraints.length)this.heading.append(node('p',`Результаты могли исключить: ${constraints.join(', ')}. Измени фильтры или продолжи загрузку следующих страниц.`,'cinema-muted'));}
    if(last?.sort_scope==='page'||last?.sort_scope==='loaded')this.heading.append(node('small','Поиск и смешанные подборки упорядочены внутри каждой страницы. Новые результаты добавляются в конец.','cinema-muted'));
    this.footer.replaceChildren();this.results.setAttribute('aria-busy',String(!!this.window.pending.size));this.footer.setAttribute('role','status');
    if(this.window.pending.size){this.footer.append(node('span','Загружаю следующую страницу…','cinema-loading'));if(!items.length)for(let i=0;i<6;i++)this.results.append(node('div','','cinema-poster-skeleton'));}
    else if(this.window.error){this.footer.append(node('p',this.window.error,'cinema-form-error'));const wait=Math.max(0,Math.ceil((this.window.retryAt-Date.now())/1000));const retry=this.button(wait?`Повторить через ${wait} с`:'Повторить загрузку',()=>this.loadPage(this.window.failedPage??this.window.next??1));retry.disabled=!this.online||!!wait;this.footer.append(retry);window.clearTimeout(this.retryTimer);if(wait)this.retryTimer=window.setTimeout(()=>this.renderList(),Math.min(wait*1000,1000));}
    else if(this.window.next){this.footer.append(this.button('Загрузить ещё',()=>this.loadPage(this.window.next!)));if(!items.length)this.footer.prepend(node('p','На проверенных страницах совпадений нет. Можно продолжить поиск.','cinema-muted'));}
    else if(pages.length)this.footer.append(node('p',items.length?'Все доступные страницы этой подборки загружены.':'По выбранным фильтрам ничего не найдено.','cinema-muted'));
  }
  private reconcile(container:HTMLElement,items:CatalogueTitle[]):void {
    const existing=new Map([...container.querySelectorAll<HTMLElement>(':scope > [data-catalogue-key]')].map(card=>[card.dataset.catalogueKey!,card]));
    const keys=new Set(items.map(i=>i.catalogue_key));
    for(const child of [...container.children])if(!keys.has((child as HTMLElement).dataset.catalogueKey!))child.remove();
    for(const [index,item] of items.entries()){
      let card=existing.get(item.catalogue_key);
      if(!card)card=this.card(item);
      else this.updateCard(card,item);
      // Avoid moving an existing node: focus, poster decoding and native scroll anchoring survive.
      if(container.children[index]!==card)container.insertBefore(card,container.children[index]??null);
    }
  }
  private updateCard(card:HTMLElement,item:CatalogueTitle):void {
    const existing=this.data?.items.find(i=>i.catalogue_key===item.catalogue_key);
    const signature=JSON.stringify([existing?.status,existing?.watchlist,existing?.favorite,existing?.user_rating]);
    if(card.dataset.library===signature)return;
    card.dataset.library=signature;
    syncPosterActions(card.querySelector<HTMLElement>('.cinema-poster-actions')!,existing);
    card.querySelector('.cinema-library-badge')!.textContent=existing&&(existing.status!=='planned'||existing.watchlist!==0)?statuses[existing.status]:'';
    card.querySelector('.cinema-personal-rating')!.textContent=existing?.user_rating?`★ ${existing.user_rating.toFixed(1)}`:'';
  }

  private card(item:CatalogueTitle):HTMLElement {
    const card=node('article','','cinema-card cinema-catalogue-card');card.dataset.catalogueKey=item.catalogue_key;
    const poster=this.button(`Подробнее: ${item.title}`,()=>this.callbacks.detail(item),'cinema-poster-button');poster.replaceChildren(this.callbacks.poster(item));
    const current=()=>this.data?.items.find(i=>i.catalogue_key===item.catalogue_key);
    const artwork=node('div','','cinema-poster-wrap');artwork.append(poster,posterActions(current,values=>this.callbacks.change(item,values),()=>this.callbacks.watch(item),this.online));
    const body=node('div','','cinema-card-body');
    body.append(this.button(item.title,()=>this.callbacks.detail(item),'cinema-title'),node('p',`${item.year||'Год неизвестен'} · ${item.kind==='series'?'Сериал':'Фильм'}`,'cinema-card-year'));
    const ratings=node('div','','cinema-ratings');ratings.append(node('span',item.metadata.rating?`★ ${item.metadata.rating.toFixed(1)}`:'TMDB —','cinema-tmdb-rating'),node('span','','cinema-personal-rating'));body.append(ratings,node('small','','cinema-library-badge'));
    card.append(artwork,body);this.updateCard(card,item);return card;
  }
}

import type {CinemaEvent, CinemaTitle, CatalogueTitle} from './cinema';

export interface FilmProfile {
  watched_movies:number;watched_series:number;favorites:number;rated_count:number;average:number|null;
  distribution:{rating:number;count:number}[];genres:{name:string;count:number}[];
  directors:{name:string;count:number}[];cast:{name:string;count:number}[];
  top:CinemaTitle[];least:CinemaTitle[];insights:string[];
  trend:{month:string;count:number;genre:string;rating:number|null}[];
  metadata_coverage:number;positive_count:number;
}
export interface PersonalRecommendations {
  samples:number;message:string;errors:string[];mood:string;
  shelves:{id:string;name:string;items:(CatalogueTitle&{reason:string})[]}[];
}
const make=<K extends keyof HTMLElementTagNameMap>(tag:K,text='',className=''):HTMLElementTagNameMap[K]=>{
  const n=document.createElement(tag);n.textContent=text;n.className=className;return n;
};
interface PersonalCallbacks {
  send:(value:object)=>void;poster:(item:CatalogueTitle|CinemaTitle)=>HTMLElement;
  detail:(item:CatalogueTitle|CinemaTitle)=>void;change:(item:CatalogueTitle,values:object)=>void;
  history:(target:HTMLElement)=>void;
}

/** Keep profile/recommendation DOM mounted across periodic player snapshots. */
export class CinemaPersonalView {
  private root=make('section','','cinema-personal');
  private profile:FilmProfile|null=null;
  private recommendations:PersonalRecommendations|null=null;
  private mode='';
  private mood='any';
  private online=false;
  private pending='';
  private sequence=0;
  private error='';
  private items:CinemaTitle[]=[];
  private librarySignature='';
  constructor(private callbacks:PersonalCallbacks){}
  connection(value:boolean):void {this.online=value;this.root.querySelectorAll<HTMLButtonElement|HTMLSelectElement>('button,select').forEach(n=>n.disabled=!value);}
  private button(label:string,fn:()=>void):HTMLButtonElement {const n=make('button',label);n.type='button';n.disabled=!this.online;n.onclick=fn;return n;}
  mount(container:HTMLElement,mode:string):void {
    const changed=this.mode!==mode;this.mode=mode;
    if(this.root.parentElement!==container)container.replaceChildren(this.root);
    if(changed)this.render();
    if(!this.pending&&!(mode==='profile'?this.profile:this.recommendations))this.load();
  }
  private load(enrich=false):void {
    if(!this.online)return;
    this.pending=`personal-${++this.sequence}`;this.error='';
    this.callbacks.send({op:enrich?'personal_enrich':this.mode==='profile'?'profile':'recommendations',request_id:this.pending,mood:this.mood});this.render();
  }
  receive(event:CinemaEvent):void {
    this.items=event.items;
    if(event.personal_recommendations){this.recommendations=event.personal_recommendations;this.pending='';this.mood=event.personal_recommendations.mood;this.render();}
    const signature=JSON.stringify([this.items.map(i=>[i.id,i.status,i.favorite,i.user_rating]),event.preferences.personalize,event.preferences.free_only,event.preferences.recommendations_show_watched,event.preferences.recommend_ratings,event.preferences.recommend_favorites,event.preferences.recommend_history,event.preferences.russian_titles,event.preferences.hide_animation,event.preferences.hide_anime,event.preferences.hide_documentary]);
    const changed=signature!==this.librarySignature;this.librarySignature=signature;
    if(event.personal_result?.request_id===this.pending){
      this.pending='';this.error=event.personal_result.error??'';
      if(event.personal_result.profile)this.profile=event.personal_result.profile;
      if(event.personal_result.recommendations)this.recommendations=event.personal_result.recommendations;
      this.render();
    }else if(changed&&this.mode){
      this.profile=null;
      if(!event.personal_recommendations)this.recommendations=null;
      // Remove watched titles immediately; refresh the algorithm on explicit demand.
      this.render();
      if(this.mode==='profile'&&!this.pending)this.load();
    }
    if(event.recommendation_dismissed&&this.recommendations){for(const shelf of this.recommendations.shelves)shelf.items=shelf.items.filter(i=>i.catalogue_key!==event.recommendation_dismissed);this.render();}
    if(event.recommendations_reset){this.recommendations=null;this.pending='';this.render();if(this.mode==='personal')this.load();}
  }
  private render():void {
    const header=make('header','','cinema-section-heading');header.append(make('h2',this.mode==='profile'?'Мой кинопрофиль':'Для тебя'));
    const refresh=this.button(this.mode==='profile'?'Обновить метаданные':'Обновить подборки',()=>this.load(this.mode==='profile'));refresh.disabled=!this.online||!!this.pending;header.append(refresh);
    this.root.replaceChildren(header);
    if(this.pending){const loading=make('p','Подбираю истории…','cinema-loading');loading.textContent=this.mode==='profile'?'Собираю твой кинопрофиль…':'Подбираю истории…';loading.setAttribute('role','status');this.root.append(loading);}
    if(this.error){const error=make('p',this.error,'cinema-form-error');error.setAttribute('role','alert');this.root.append(error,this.button('Повторить',()=>this.load()));}
    if(this.mode==='profile')this.renderProfile();else this.renderRecommendations();
  }
  private bars(title:string,values:{name:string;count:number}[]):HTMLElement {
    const panel=make('section','','cinema-profile-panel');panel.append(make('h3',title));
    if(!values.length)panel.append(make('p','Пока мало данных. Добавь оценки и обнови метаданные.','cinema-muted'));
    const max=Math.max(1,...values.map(v=>v.count));
    for(const value of values){const row=make('div','','cinema-chart-row');const bar=make('div','','cinema-chart-bar');bar.style.setProperty('--value',`${value.count/max*100}%`);row.append(make('span',value.name),bar,make('strong',String(value.count)));row.setAttribute('aria-label',`${value.name}: ${value.count}`);panel.append(row);}
    return panel;
  }
  private posterShelf(title:string,items:CinemaTitle[]):HTMLElement {
    const section=make('section','','cinema-shelf');section.append(make('h3',title));const row=make('div','','cinema-carousel');
    for(const item of items){const card=make('article','','cinema-card');const poster=this.button(`Открыть ${item.title}`,()=>this.callbacks.detail(item));poster.className='cinema-poster-button';poster.replaceChildren(this.callbacks.poster(item));const body=make('div','','cinema-card-body');body.append(make('strong',item.title),make('p',`Твоя оценка ${item.user_rating}/10`));card.append(poster,body);row.append(card);}section.append(row);return section;
  }
  private renderProfile():void {
    const p=this.profile;if(!p)return;
    const hero=make('div','','cinema-profile-hero');hero.append(make('span','ТВОИ ИСТОРИИ · ТВОЙ ВКУС','cinema-profile-eyebrow'),make('h3','Кино, в котором узнаёшь себя'),make('p','Каждая оценка помогает Чушпану понять, какие истории захочется увидеть следующими.'));
    const stats=make('div','','cinema-profile-stats');
    for(const [value,label]of [[p.watched_movies,'просмотренных фильмов'],[p.watched_series,'просмотренных сериалов'],[p.average??'—','средняя оценка'],[p.favorites,'в избранном']]){const stat=make('article');stat.append(make('strong',String(value)),make('span',String(label)));stats.append(stat);}this.root.append(hero,stats);
    const insight=make('section','','cinema-profile-panel cinema-profile-insight');insight.append(make('h3','Что Чушпан понял о моих вкусах'),...p.insights.map(v=>make('p',v)),make('small',`Метаданные состава: ${p.metadata_coverage} из ${p.positive_count} понравившихся названий. Выводы учитывают только твою медиатеку.`));this.root.append(insight);
    const charts=make('div','','cinema-profile-charts');charts.append(this.bars(`Твои оценки · ${p.rated_count}`,p.distribution.map(v=>({name:v.rating===10?'10':`${v.rating}–${v.rating}.5`,count:v.count}))),this.bars('Любимые жанры',p.genres),this.bars('Любимые режиссёры',p.directors),this.bars('Любимые актёры',p.cast));this.root.append(charts);
    this.root.append(this.bars('Активность просмотров',p.trend.map(month=>({name:month.month,count:month.count}))));
    const trend=make('section','','cinema-profile-panel');trend.append(make('h3','Как менялись твои предпочтения'),make('p','Уникальные названия по журналу просмотров; жанр чаще встречался в этом месяце. Средняя оценка относится только к оценкам, поставленным в указанный месяц.','cinema-muted'));
    const timeline=make('div','','cinema-profile-timeline');for(const month of p.trend){const row=make('article');row.append(make('time',month.month),make('strong',`${month.count} историй`),make('span',month.genre||'Жанр пока неизвестен'),make('small',month.rating===null?'Нет новых оценок':`Новые оценки: ${month.rating}/10`));timeline.append(row);}if(!p.trend.length)timeline.append(make('p','История появится после первых просмотров.','cinema-muted'));trend.append(timeline);this.root.append(trend);
    if(p.top.length)this.root.append(this.posterShelf('Самые высокие оценки',p.top));
    if(p.least.length)this.root.append(this.posterShelf('Не совпало с твоим вкусом',p.least));
    const history=make('details');history.append(make('summary','История просмотров'));const holder=make('div');this.callbacks.history(holder);history.append(holder);this.root.append(history);
  }
  private renderRecommendations():void {
    const controls=make('label','','cinema-field');controls.append(make('span','Какое настроение?'));const select=make('select');select.setAttribute('aria-label','Настроение');for(const [id,label]of [['any','Любое'],['calm','Лёгкое и спокойное'],['tense','Напряжённое'],['adventure','Хочется приключений']]){const option=make('option',label);option.value=id;select.append(option);}select.value=this.mood;select.disabled=!this.online||!!this.pending;select.onchange=()=>{this.mood=select.value;this.load();};controls.append(select);this.root.append(controls);
    const data=this.recommendations;if(!data)return;this.root.append(make('p',data.message,'cinema-muted'));
    for(const error of data.errors)this.root.append(make('p',error,'cinema-form-error'));
    for(const shelf of data.shelves){const section=make('section','','cinema-shelf');section.append(make('h3',shelf.name));const row=make('div','','cinema-carousel');
      const watched=new Set(this.items.filter(i=>i.status==='watched').map(i=>i.catalogue_key));
      for(const item of shelf.items.filter(i=>shelf.id==='rewatch'||!watched.has(i.catalogue_key))){const card=make('article','','cinema-card cinema-personal-card');const poster=this.button(`Открыть ${item.title}`,()=>this.callbacks.detail(item));poster.className='cinema-poster-button';poster.replaceChildren(this.callbacks.poster(item));const body=make('div','','cinema-card-body');body.append(make('strong',item.title),make('p',`${item.year||'—'} · ★ ${item.metadata.rating.toFixed(1)}`),make('p',item.reason,'cinema-recommendation-reason'));
        const actions=make('div','','cinema-personal-actions');actions.append(this.button('Хочу посмотреть',()=>this.callbacks.change(item,{status:'planned'})),this.button('Уже смотрела',()=>this.callbacks.change(item,{status:'watched'})),this.button('Не интересно',()=>{this.callbacks.send({op:'recommendation_dismiss',key:item.catalogue_key});}));body.append(actions);card.append(poster,body);row.append(card);}
      if(!row.children.length)section.append(make('p',shelf.id==='now'?'Пока нет сохранённых источников с недавней успешной проверкой. Найди источник в карточке и сохрани его.':'Пока недостаточно данных для этой подборки. Оценки и избранное помогут её наполнить.','cinema-muted'));else section.append(row);this.root.append(section);
    }
  }
}

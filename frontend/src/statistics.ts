import {icon} from './dashboard';
import {seconds} from './workspace_state';

type Times = Record<'stt'|'llm'|'tools'|'tts'|'total',number|null>;
type Metrics = {requests:number;response_s:number|null;executed:number;success_pct:number|null;known_operations:number;unconfirmed_operations:number};
type Diagnostic = {request_id:string;created_at:string;kind:string;category:string;model:string|null;status:string;times:Times;errors:string[]};
export interface StatisticsData {
  start:string;end:string;timezone:string;unit:string;collection_enabled:boolean;metrics:Metrics;previous:Metrics;
  buckets:{key:string;label:string;requests:number;latency:Times}[];
  distribution:Record<string,number>;requests:Diagnostic[];
  scenarios:{id:string;name:string;status:string;started_at:string;duration_s?:number;error?:string;steps:{path:string;status:string;duration_s?:number;error?:string;reason?:string;result?:unknown}[]}[];
  apps:{name:string;seconds:number}[];
}
const el=<K extends keyof HTMLElementTagNameMap>(tag:K,text='',cls=''):HTMLElementTagNameMap[K]=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
const categories:Record<string,string>={conversations:'Обычные разговоры',devices:'Управление устройствами',scenarios:'Сценарии',search:'Поиск информации',other:'Другие команды'};
const statuses:Record<string,string>={completed:'Завершено',success:'Успешно',failed:'Ошибка',partial_success:'Частично',running:'В работе',cancelled:'Отменено',interrupted:'Прервано',unknown:'Не подтверждено',unverified:'Не подтверждено',awaiting_confirmation:'Ждёт подтверждения',rejected:'Отклонено'};
const series:[keyof Times,string,string][]=[['stt','Распознавание речи','#d49df4'],['llm','Первый токен LLM','#b05df2'],['tools','Инструменты','#eea0c8'],['tts','Генерация речи','#8b9df5'],['total','Полное время','#78d9ca']];
const colors=['#ac50ed','#ce94ef','#e88cbd','#8251ae','#54406e'];
const tabs=[['overview','Обзор','grid'],['performance','Производительность','activity'],['conversations','Разговоры','chat'],['commands','Команды','bolt'],['scenarios','Сценарии','sparkles'],['apps','Приложения','monitor'],['reports','Отчёты','file']];
function button(text:string,action:()=>void,cls=''):HTMLButtonElement {const n=el('button',text,cls);n.type='button';n.onclick=action;return n;}
function glyph(name:string):HTMLElement {const n=el('span','','statistics-icon');n.innerHTML=icon(name);return n;}
function empty(title:string,hint:string):HTMLElement {const n=el('div','','statistics-empty');n.append(glyph('chart'),el('strong',title),el('p',hint));return n;}
function svg(tag:string,attrs:Record<string,string>):SVGElement {const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value]of Object.entries(attrs))n.setAttribute(key,value);return n;}

export class StatisticsDashboard {
  private root=document.getElementById('statistics-root')!;
  private data:StatisticsData|null=null;
  private tab='overview';
  private activeSeries=new Set(series.map(([key])=>key));
  private sort='recent';
  private online=false;
  private content=el('div','','statistics-content');
  private cards=el('div','','statistics-kpis');
  private navigation=el('nav','','statistics-tabs');
  private status=el('p','Подключись к Чушпану, чтобы увидеть статистику.','statistics-status');
  private periods=el('div','','statistics-periods');
  private custom=el('form','','statistics-custom');
  private start=el('input');
  private end=el('input');
  constructor(private query:(days:number,start?:string,end?:string)=>void,private trace:(id:string)=>void,private openCollection:()=>void=()=>{}) {
    const heading=el('header','','statistics-heading');const title=el('div','','page-heading');title.append(el('h1','Статистика Чушпана'),el('p','Аналитика использования, активности и эффективности'));
    const name=el('div','','statistics-title');name.append(glyph('chart'),title);heading.append(name,this.periods);
    this.periods.setAttribute('aria-label','Период статистики');
    for(const [days,label]of [[1,'Сегодня'],[7,'7 дней'],[30,'30 дней'],[90,'90 дней'],[-2,'Всё время'],[-1,'Произвольный период']] as const){const b=button(label,()=>{if(days===-1){this.custom.hidden=!this.custom.hidden;b.setAttribute('aria-expanded',String(!this.custom.hidden));if(!this.custom.hidden)this.start.focus();}else this.changePeriod(days);});b.dataset.days=String(days);b.setAttribute('aria-pressed',String(days===1));if(days===-1)b.setAttribute('aria-expanded','false');this.periods.append(b);}
    for(const [input,label]of [[this.start,'Начало периода'],[this.end,'Конец периода']] as const){input.type='date';input.required=true;input.setAttribute('aria-label',label);const field=el('label',label);field.append(input);this.custom.append(field);}
    this.custom.append(button('Применить',()=>{if(!this.custom.reportValidity())return;if(this.end.value<this.start.value||(+new Date(this.end.value)-+new Date(this.start.value))/86400000>365){this.status.textContent='Выбери от 1 до 366 дней: начало должно быть раньше конца.';return;}this.changePeriod(-1,this.start.value,this.end.value);}));this.custom.onsubmit=e=>e.preventDefault();this.custom.hidden=true;
    this.navigation.setAttribute('role','tablist');this.navigation.setAttribute('aria-label','Разделы статистики');
    for(const [id,label,name]of tabs){const b=button('',()=>this.selectTab(id));b.id=`statistics-tab-${id}`;b.setAttribute('role','tab');b.setAttribute('aria-controls',`statistics-pane-${id}`);b.dataset.tab=id;b.append(glyph(name),el('span',label));this.navigation.append(b);}
    this.navigation.onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const index=tabs.findIndex(([id])=>id===this.tab);const next=e.key==='Home'?0:e.key==='End'?tabs.length-1:(index+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;this.selectTab(tabs[next][0]);(this.navigation.children[next] as HTMLElement).focus();};
    this.status.setAttribute('role','status');
    this.root.append(heading,this.custom,this.cards,this.navigation,this.status,this.content);
    const diagnostics=document.getElementById('statistics-diagnostics')!;
    diagnostics.setAttribute('role','tabpanel');diagnostics.id='statistics-pane-performance';diagnostics.setAttribute('aria-labelledby','statistics-tab-performance');
    this.root.append(diagnostics);
    this.selectTab('overview');this.render();
  }
  connection(online:boolean):void {this.online=online;this.status.textContent=online?'Загружаю статистику…':'Нет соединения. Последние данные могут устареть.';this.root.querySelectorAll<HTMLButtonElement>('.statistics-periods button,.statistics-custom button').forEach(b=>b.disabled=!online);}
  loading():void {this.status.textContent='Загружаю статистику за выбранный период…';this.root.setAttribute('aria-busy','true');this.cards.setAttribute('aria-busy','true');}
  error(message:string):void {this.status.textContent=message;this.root.removeAttribute('aria-busy');this.cards.removeAttribute('aria-busy');}
  receive(data:StatisticsData):void {this.data=data;this.root.removeAttribute('aria-busy');this.cards.removeAttribute('aria-busy');this.status.textContent=`${new Date(data.start).toLocaleDateString('ru-RU',{timeZone:data.timezone})} — ${new Date(+new Date(data.end)-1).toLocaleDateString('ru-RU',{timeZone:data.timezone})} · ${data.timezone} · сохранённые события${this.online?'':' · нет соединения'}`;this.render();}
  showPerformance():void {this.selectTab('performance');}
  private changePeriod(days:number,start?:string,end?:string):void {for(const b of this.periods.querySelectorAll<HTMLButtonElement>('button'))b.setAttribute('aria-pressed',String(Number(b.dataset.days)===days));this.custom.hidden=true;this.periods.querySelector('[data-days="-1"]')?.setAttribute('aria-expanded','false');this.data=null;this.loading();this.render();this.query(days,start,end);}
  private selectTab(tab:string):void {this.tab=tab;for(const b of this.navigation.querySelectorAll<HTMLButtonElement>('button')){const active=b.dataset.tab===tab;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;}document.getElementById('statistics-pane-performance')!.hidden=tab!=='performance';this.content.hidden=tab==='performance';this.content.id=tab==='performance'?'statistics-secondary-content':`statistics-pane-${tab}`;this.content.setAttribute('role','tabpanel');this.content.setAttribute('aria-labelledby',`statistics-tab-${tab}`);this.renderContent();}
  private render():void {this.renderCards();this.renderContent();}
  private renderCards():void {
    const m=this.data?.metrics,previous=this.data?.previous;
    const values:[keyof Metrics,string,string,string][]=[...(this.data?.collection_enabled===false?[]:[['requests','Обращения к Чушпану','chat',m?String(m.requests):'—'] as [keyof Metrics,string,string,string]]),['response_s','Средняя задержка ответа','activity',seconds(m?.response_s)],...(this.data?.collection_enabled===false?[]:[['executed','Выполненные команды и сценарии','bolt',m?String(m.executed):'—'] as [keyof Metrics,string,string,string],['success_pct','Успешные операции','check',m?.success_pct==null?'—':`${m.success_pct.toFixed(0)} %`] as [keyof Metrics,string,string,string]])];
    this.cards.replaceChildren(...values.map(([key,label,name,value])=>{const card=el('article','','statistics-kpi');const body=el('div');body.append(el('small',label),el('strong',value));const foot=el('div','','statistics-kpi-foot');let delta='Нет сравнения';const before=previous?.[key],after=m?.[key];if(before!=null&&after!=null&&before>0){const change=(after-before)/before*100;delta=`${change>0?'+':''}${change.toFixed(0)} %`;foot.dataset.trend=change===0?'neutral':((key==='response_s'?change<0:change>0)?'good':'bad');}else if(before===0&&after!=null&&after>0)delta='Первые события';foot.append(el('span',delta));const bars=el('span','','statistics-mini');bars.setAttribute('aria-hidden','true');const recent=key==='requests'&&this.data?.metrics.requests?this.data.buckets.slice(-12):[];const max=Math.max(1,...recent.map(b=>b.requests));for(const b of recent){const bar=el('i');bar.style.height=`${b.requests?3+17*b.requests/max:0}px`;bars.append(bar);}foot.append(bars);body.append(foot);card.append(glyph(name),body);if(key==='response_s')card.title='До содержательного звука для голоса, до текста для чата. Только завершённые измеренные ответы.';if(key==='success_pct'||key==='executed')card.title=`Подтверждённые результаты команд и сценариев. Без подтверждения: ${m?.unconfirmed_operations??0}. Разговоры в процент не входят.`;return card;}));
  }
  private card(title:string,note:string):HTMLElement {const card=el('section','','statistics-card');card.append(el('h2',title),el('p',note,'statistics-caption'));return card;}
  private renderContent():void {
    if(this.tab==='performance')return;
    this.content.replaceChildren();
    if(!this.data){const state=empty('Статистика ждёт первых событий','Показатели обращений используют журнал аналитики, который выключен по умолчанию. Время ответа, этапы и результаты сценариев показываются отдельно из локальных диагностических таймингов.');state.append(button('Настроить сбор событий',this.openCollection,'primary'));this.content.append(state);return;}
    if(!this.data.collection_enabled){const card=this.card('Сбор событий выключен','Счётчики обращений и операций, история, категории и отчёты скрыты, пока сбор не включён в настройках. Сохранённые тайминги остаются доступны во вкладке «Производительность».');card.append(button('Настроить сбор событий',this.openCollection,'primary'));this.content.append(card);return;}
    if(this.tab==='overview'){
      if(!this.data.metrics.requests){const card=this.card('Пока нет данных за выбранный период','Обращения и распределение операций появятся, если включить сбор событий и использовать Чушпана. Время ответов и этапов всё равно доступно во вкладке «Производительность» по локальным диагностическим таймингам.');card.append(empty('Начни с разговора или команды','Выбери другой период, чтобы посмотреть ранее сохранённую активность. История запусков доступна во вкладке «Сценарии».'));card.append(button('Настроить сбор событий',this.openCollection,'primary'));this.content.append(card);return;}
      const grid=el('div','','statistics-grid');grid.append(this.activity(),this.distribution(),this.latency());this.content.append(grid);
    }else if(this.tab==='scenarios')this.renderScenarios();
    else if(this.tab==='apps')this.renderApps();
    else if(this.tab==='reports')this.renderReports();
    else this.renderRequests(this.tab==='conversations'?['conversations']:['devices','search','other']);
  }
  private activity():HTMLElement {
    const data=this.data!;const card=this.card('Активность по времени',`Количество обращений · ${data.unit==='hour'?'по часам':data.unit==='month'?'по месяцам':'по дням'}`);
    if(!data.metrics.requests){card.append(empty('Пока нет обращений','Поговори с Чушпаном — здесь появится ритм твоей активности.'));return card;}
    const graph=el('div','','statistics-bars');graph.setAttribute('role','img');graph.setAttribute('aria-label','Количество обращений по времени');const max=Math.max(1,...data.buckets.map(b=>b.requests));
    for(const [i,b]of data.buckets.entries()){const col=el('div','','statistics-bar-column');const bar=el('span','','statistics-bar');bar.style.height=`${b.requests/max*128}px`;col.title=`${b.label} · ${b.requests} обращений`;bar.append(el('span',String(b.requests),'statistics-bar-value'));col.append(bar,el('small',i%Math.max(1,Math.ceil(data.buckets.length/10))===0?b.label:'\u00a0'));graph.append(col);}card.append(graph);return card;
  }
  private distribution():HTMLElement {
    const data=this.data!;const card=this.card('Распределение операций','Количество запросов по основному типу операции');const total=Object.values(data.distribution).reduce((a,b)=>a+b,0);
    if(!total){card.append(empty('Операций пока нет','Распределение появится после разговоров и команд.'));return card;}
    const layout=el('div','','statistics-donut-layout');const donut=el('div','','statistics-donut');let offset=0;const stops=Object.entries(categories).map(([key],i)=>{const start=offset;offset+=(data.distribution[key]??0)/total*100;return `${colors[i]} ${start}% ${offset}%`;});donut.style.background=`conic-gradient(${stops.join(',')})`;donut.setAttribute('role','img');donut.setAttribute('aria-label',`${total} запросов, распределение по типу операции`);const center=el('div');center.append(el('strong',String(total)),el('small','запросов'));donut.append(center);const legend=el('div','','statistics-donut-legend');for(const [i,[key,label]]of Object.entries(categories).entries()){const row=el('div');const dot=el('i');dot.style.background=colors[i];row.append(dot,el('span',label),el('strong',`${Math.round((data.distribution[key]??0)/total*100)} %`));row.title=`${data.distribution[key]??0} запросов`;legend.append(row);}layout.append(donut,legend);card.append(layout);return card;
  }
  private latency():HTMLElement {
    const data=this.data!;const card=this.card('Время ответа','Средняя задержка этапов за интервал · секунды. Пропуски означают отсутствие измерений.');card.classList.add('statistics-latency');const legend=el('div','','statistics-series');
    for(const [key,label,color]of series){const b=button(label,()=>{if(this.activeSeries.has(key))this.activeSeries.delete(key);else this.activeSeries.add(key);card.replaceWith(this.latency());});b.style.setProperty('--series-color',color);b.setAttribute('aria-pressed',String(this.activeSeries.has(key)));legend.append(b);}card.append(legend);
    if(!data.buckets.some(b=>series.some(([key])=>b.latency[key]!=null))){card.append(empty('Нет измерений времени ответа','Измеренные этапы появятся после завершённых запросов. Отсутствующее время не заменяется нулём.'));return card;}
    if(!this.activeSeries.size){card.append(empty('Все серии скрыты','Выбери этап в легенде, чтобы показать его на графике.'));return card;}
    const selected=series.filter(([key])=>this.activeSeries.has(key));const max=Math.max(.1,...data.buckets.flatMap(b=>selected.map(([key])=>b.latency[key]??0)));const chart=svg('svg',{viewBox:'0 0 900 200',role:'img','aria-label':'Задержка этапов ответа в секундах',class:'statistics-line-chart'});
    const x=(i:number)=>55+i*825/Math.max(1,data.buckets.length-1),y=(v:number)=>155-v/max*130;
    for(let i=0;i<4;i++){const value=max*i/3;chart.append(svg('line',{x1:'55',x2:'880',y1:String(y(value)),y2:String(y(value)),class:'statistics-gridline'}));const label=svg('text',{x:'43',y:String(y(value)+4),'text-anchor':'end'});label.textContent=value.toFixed(1);chart.append(label);}
    for(const [i,b]of data.buckets.entries())if(i%Math.max(1,Math.ceil(data.buckets.length/9))===0){const label=svg('text',{x:String(x(i)),y:'185','text-anchor':'middle'});label.textContent=b.label;chart.append(label);}
    for(const [key,label,color]of selected){let path='',connected=false;for(const [i,b]of data.buckets.entries()){const v=b.latency[key];if(v==null){connected=false;continue;}path+=`${connected?'L':'M'}${x(i)},${y(v)} `;connected=true;const dot=svg('circle',{cx:String(x(i)),cy:String(y(v)),r:'3',fill:color});const title=svg('title',{});title.textContent=`${b.label} · ${label}: ${seconds(v)}`;dot.append(title);chart.append(dot);}chart.append(svg('path',{d:path,fill:'none',stroke:color,'stroke-width':'2.5','stroke-linejoin':'round'}));}card.append(chart);return card;
  }
  private renderRequests(allowed:string[]):void {
    const card=this.card(this.tab==='conversations'?'История обращений':'Команды и операции','Сохранённые запросы за выбранный период. Нажми на строку для подробной трассы.');const sort=el('select');sort.setAttribute('aria-label','Сортировка запросов');for(const [value,label]of [['recent','Сначала новые'],['slow','Самые медленные']]){const option=el('option',label);option.value=value;sort.append(option);}sort.value=this.sort;sort.onchange=()=>{this.sort=sort.value;this.renderContent();};card.append(sort);const rows=this.data!.requests.filter(r=>allowed.includes(r.category)).sort((a,b)=>this.sort==='slow'?(b.times.total??-1)-(a.times.total??-1):b.created_at.localeCompare(a.created_at));
    if(!rows.length)card.append(empty('За этот период запросов нет','История появится после соответствующих обращений к Чушпану.'));
    else{const list=el('div','','statistics-request-list');for(const row of rows){const b=button('',()=>this.trace(row.request_id),'statistics-request');b.append(el('time',new Date(row.created_at).toLocaleString('ru-RU',{timeZone:this.data!.timezone})),el('span',categories[row.category]),el('span',row.model??'Локальная команда'),el('strong',seconds(row.times.total)),el('small',statuses[row.status]??row.status));list.append(b);}card.append(list);}this.content.append(card);
  }
  private renderScenarios():void {
    const card=this.card('История сценариев','Все сохранённые запуски за выбранный период, включая ошибки и незавершённые действия.');
    for(const run of this.data!.scenarios){const details=el('details','','statistics-receipt');details.append(el('summary',`${run.name} · ${statuses[run.status]??run.status} · ${new Date(run.started_at).toLocaleString('ru-RU',{timeZone:this.data!.timezone})} · ${seconds(run.duration_s)}`));if(run.error)details.append(el('p',run.error,'statistics-error'));for(const step of run.steps){const line=el('div','','statistics-step');line.append(el('p',`${step.path} · ${statuses[step.status]??step.status} · ${seconds(step.duration_s)}`));if(step.reason)line.append(el('p',step.reason));if(step.error)line.append(el('p',step.error,'statistics-error'));if(step.result){const technical=el('details');technical.append(el('summary','Подробности интеграции'),el('pre',JSON.stringify(step.result,null,2)));line.append(technical);}details.append(line);}card.append(details);}
    if(!this.data!.scenarios.length)card.append(empty('Сценарии ещё не запускались','Здесь появятся результаты выполнения и сведения о каждом шаге.'));this.content.append(card);
  }
  private renderApps():void {
    const card=this.card('Активность приложений','Время активного использования · только ранее собранные локальные события');const apps=this.data!.apps;const max=Math.max(1,...apps.map(a=>a.seconds));for(const app of apps){const row=el('div','','statistics-app');const track=el('span','','statistics-app-track');const fill=el('i');fill.style.width=`${app.seconds/max*100}%`;track.append(fill);row.append(el('span',app.name),track,el('strong',`${Math.round(app.seconds/60)} мин`));card.append(row);}if(!apps.length)card.append(empty('Учёт приложений пока пуст','Данные появятся, если учёт активности разрешён в настройках. Открытие этой вкладки не включает сбор.'));this.content.append(card);
  }
  private renderReports():void {
    const card=this.card('Отчёт за выбранный период','Экспорт сохранённых показателей, измерений и результатов операций');card.append(el('p',`Обращений: ${this.data!.metrics.requests} · подтверждённых операций: ${this.data!.metrics.executed} · без подтверждения: ${this.data!.metrics.unconfirmed_operations}`),button('Скачать JSON',()=>this.download('json')),button('Скачать CSV',()=>this.download('csv')));this.content.append(card);
  }
  private download(format:string):void {
    const data=this.data!;const csv=(v:unknown)=>`"${String(v??'').replaceAll('"','""')}"`;
    const content=format==='json'?JSON.stringify(data,null,2):[['Время','Тип','Модель','Статус','STT, с','LLM, с','Tools, с','TTS, с','Всего, с'],...data.requests.map(r=>[r.created_at,categories[r.category],r.model,r.status,...series.map(([key])=>r.times[key])])].map(row=>row.map(csv).join(',')).join('\r\n');
    const url=URL.createObjectURL(new Blob([format==='csv'?'\uFEFF'+content:content],{type:format==='json'?'application/json':'text/csv;charset=utf-8'}));const a=el('a');a.href=url;a.download=`chushpan-statistics.${format}`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
}

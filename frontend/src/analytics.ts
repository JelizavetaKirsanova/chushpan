import type {HudEvent} from './types';
import {ModulePage,card,date,duration,el,formFields,grid,metric,row,seconds,type ModuleChannel} from './module_ui';
export interface AnalyticsPolicy {enabled:boolean;categories:string[];retention_days:number;excluded_apps:string[];development_apps:string[];study_apps:string[];}
export interface AnalyticsApplication {bundle:string;name:string;seconds:number;}
export interface AnalyticsSummary {count:number;voice:number;text:number;mean_s:number|null;p50_s:number|null;p95_s:number|null;failed:number;active_s:number;idle_s:number;system_s:number;legacy_s:number;background_s:number|null;development_s:number;study_s:number;switches:number;apps:AnalyticsApplication[];system_apps:AnalyticsApplication[];legacy_apps:AnalyticsApplication[];categories:Record<string,number>;daily:{day:string;requests:number;active_s:number}[];providers:Record<string,number>;commands:{name:string;category?:string;count:number;failed:number;confirmed:number}[];stages:{name:string;mean_s:number;p95_s:number}[];latencies:{at:number;seconds:number|null}[];events:number;}
export interface AnalyticsData {summary:AnalyticsSummary;previous:AnalyticsSummary;policy:AnalyticsPolicy;insights:string[];dropped:number;error:string|null;query:{period?:string;start?:string;end?:string;device?:string};timezone:string;devices:string[];categories:Record<string,string>;}
const categoryLabels:Record<string,string>={development:'Разработка (выбрано тобой)',study:'Учёба (выбрано тобой)',other:'Другое'};
const colors=['#bb7ffa','#8399f3','#e88fc3','#77649e','#69bdbe'];
const applicationBarWidths=new Map<string,number>();
const appColor=(bundle:string):string=>{const hash=[...bundle].reduce((value,char)=>(value*31+char.charCodeAt(0))|0,0);return colors[Math.abs(hash)%colors.length];};
const exactDuration=(value:number):string=>{const total=Math.round(Math.max(0,value));const hours=Math.floor(total/3600);const minutes=Math.floor(total%3600/60);const seconds=total%60;return hours?`${hours} ч ${minutes} м ${seconds} с`:minutes?`${minutes} м ${seconds} с`:`${seconds} с`;};

export function barChart(values:{label:string;value:number;detail:string}[],title:string):HTMLElement {
  const node=card(title);const plot=el('div','','module-bars');const selected=el('p','Нажми на столбец, чтобы посмотреть значение.','chart-detail');const max=Math.max(1,...values.map(v=>v.value));
  if(!values.length){node.append(el('p','Нет событий за выбранный период.','muted'));return node;}
  values.forEach((value,index)=>{const button=el('button','','chart-bar');button.type='button';button.setAttribute('aria-label',value.label+': '+value.detail);button.title=value.detail;const track=el('span','','chart-bar-track');const fill=el('span','','chart-bar-fill');fill.style.width=`${Math.max(0,Math.min(100,value.value/max*100))}%`;fill.style.background=colors[index%colors.length];track.append(fill);button.append(el('span',value.label,'chart-bar-label'),track,el('small',value.detail));button.onclick=()=>{selected.textContent=value.label+' · '+value.detail;plot.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));};plot.append(button);});node.append(plot,selected);return node;
}
export function applicationTimeChart(apps:AnalyticsApplication[],systemApps:AnalyticsApplication[]=[],legacyApps:AnalyticsApplication[]=[]):HTMLElement {
  const node=card('Измеренное время приложений');
  const renderRows=(items:AnalyticsApplication[],container:HTMLElement,scope:string)=>{
    const valueOf=(app:AnalyticsApplication)=>Number.isFinite(app.seconds)?Math.max(0,app.seconds):0;
    const sorted=[...items].sort((a,b)=>valueOf(b)-valueOf(a)||a.name.localeCompare(b.name,'ru'));
    const maximum=Math.max(0,...sorted.map(valueOf));
    const total=sorted.reduce((sum,app)=>sum+valueOf(app),0);
    sorted.forEach(app=>{
      const value=valueOf(app);const relative=maximum?value/maximum*100:0;
      const share=total?value/total*100:0;const percent=new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1}).format(share);
      const accent=appColor(app.bundle);const key=`${scope}:${app.bundle}`;const previous=applicationBarWidths.get(key);
      const animate=previous!==undefined&&previous!==relative&&typeof window.requestAnimationFrame==='function';
      const item=el('div','','application-time-row');item.title=`${app.name} · ${exactDuration(value)} · ${percent}% от времени списка`;
      const identity=el('span','','application-time-identity');
      const icon=el('span',[...app.name.trim()][0]?.toLocaleUpperCase('ru-RU')||'•','application-time-icon');
      icon.setAttribute('aria-hidden','true');icon.style.setProperty('--app-accent',accent);
      const name=el('span',app.name,'application-time-name');name.title=app.name;
      identity.append(icon,name);
      const track=el('span','','application-time-track');track.setAttribute('role','progressbar');
      track.setAttribute('aria-label',`${app.name}: ${exactDuration(value)}, ${percent}% от времени списка`);
      track.setAttribute('aria-valuemin','0');track.setAttribute('aria-valuemax','100');track.setAttribute('aria-valuenow',String(relative));
      track.title=`${exactDuration(value)} · ${percent}% от времени списка`;
      const fill=el('span','','application-time-fill');fill.style.width=`${animate?previous:relative}%`;fill.style.setProperty('--app-accent',accent);track.append(fill);
      const time=el('strong',exactDuration(value),'application-time-value');
      item.append(identity,track,time);container.append(item);
      if(animate)window.requestAnimationFrame(()=>{fill.style.width=`${relative}%`;});
      applicationBarWidths.set(key,relative);
    });
  };
  if(apps.length){const list=el('div','','application-time-list');renderRows(apps,list,'apps');node.append(list);}
  else node.append(el('p','Нет измерений приложений за выбранный период.','muted'));
  if(systemApps.length){
    const details=el('details','','application-system-details');const total=systemApps.reduce((sum,app)=>sum+Math.max(0,app.seconds),0);
    details.append(el('summary',`Системные процессы · ${exactDuration(total)}`));const list=el('div','','application-time-list application-system-list');renderRows(systemApps,list,'system');details.append(list);node.append(details);
  }
  if(legacyApps.length){
    const details=el('details','','application-system-details');const total=legacyApps.reduce((sum,app)=>sum+Math.max(0,app.seconds),0);
    details.append(el('summary',`История без проверки простоя · ${exactDuration(total)}`));const list=el('div','','application-time-list application-system-list');renderRows(legacyApps,list,'legacy');details.append(list);node.append(details);
  }
  node.append(el('p','Учтено время приложения на переднем плане. После минуты без ввода оно относится к простою. Системные процессы, включая loginwindow, показаны отдельно. История до определения простоя сохранена отдельно. Фоновое время не измеряется.','application-time-note'));
  return node;
}
export function donut(categories:Record<string,number>):HTMLElement {
  const node=card('Распределение активности');const total=Object.values(categories).reduce((sum,v)=>sum+v,0);if(!total){node.append(el('p','Сбор активности приложений выключен или измерений ещё нет.','muted'));return node;}
  const plot=el('div','','module-donut');let start=0;const stops=Object.entries(categories).map(([,value],index)=>{const end=start+value/total*100;const segment=`${colors[index%colors.length]} ${start}% ${end}%`;start=end;return segment;});plot.style.background=`conic-gradient(${stops.join(',')})`;const center=el('span',duration(total));plot.append(center);plot.setAttribute('role','img');plot.setAttribute('aria-label',`Измеренная активность ${duration(total)}`);const legend=el('div','','donut-legend');Object.entries(categories).forEach(([key,value],index)=>{const button=el('button',`${categoryLabels[key]||key} · ${duration(value)}`);button.style.setProperty('--segment',colors[index%colors.length]);button.onclick=()=>{center.textContent=duration(value);plot.setAttribute('aria-label',`${categoryLabels[key]||key}: ${duration(value)}`);};legend.append(button);});node.append(row(plot,legend));return node;
}

export class AnalyticsView extends ModulePage<AnalyticsData> {
  private querySelection:AnalyticsData['query']|null=null;
  receive(event:HudEvent):boolean {const accepted=super.receive(event);if(accepted&&'error' in event&&event.error&&this.tab!=='collection'&&this.state){this.querySelection=this.state.query;this.render();}return accepted;}
  private query(value:AnalyticsData['query']):void {const selected={...(this.querySelection??this.state?.query??{period:'today'}),...value};if(selected.period!=='custom'){delete selected.start;delete selected.end;}this.querySelection=selected;this.send({op:'query',...selected});}
  openCollection():void {this.tab='collection';this.render();}

  constructor(post:(channel:ModuleChannel,value:object)=>void,navigate:(view:string)=>void){super('analytics','analytics','Аналитика','Реальные события Чушпана и Mac. Сбор данных включаешь и настраиваешь ты.',post,navigate,[['overview','Обзор'],['assistant','Чушпан'],['apps','Приложения'],['study','Учёба'],['devices','Устройства'],['media','Медиатека'],['reports','Отчёты'],['collection','Сбор данных']]);}
  protected refresh():void {this.send({op:'query',...(this.querySelection??this.state?.query??{period:'today'})});}
  protected render():void {
    if(!this.begin())return;const data=this.state!,summary=data.summary;const query=this.querySelection??data.query;
    const period=el('select');period.setAttribute('aria-label','Период аналитики');for(const [value,label]of [['today','Сегодня'],['week','7 дней'],['month','30 дней'],['90days','90 дней'],['all','Всё время'],['custom','Свой период']]){const option=el('option',label);option.value=value;period.append(option);}period.value=query.period||'today';period.onchange=()=>{if(period.value==='custom'){const {form,read}=formFields([{name:'start',label:'Начало',kind:'date',required:true,value:query.start},{name:'end',label:'Конец',kind:'date',required:true,value:query.end}]);const modal=this.openDialog('Свой период аналитики',form);modal.addEventListener('close',()=>{period.value=(this.querySelection??data.query).period||'today';});const end=form.elements.namedItem('end') as HTMLInputElement;form.oninput=()=>end.setCustomValidity('');const close=el('button','Отмена');close.type='button';close.onclick=()=>modal.close();form.append(row(this.button('Выбрать',()=>{const values=read();const days=(+new Date(String(values.end))-+new Date(String(values.start)))/86400000;end.setCustomValidity(days<0||days>365?'Выбери от 1 до 366 дней: конец не должен быть раньше начала.':'');if(form.reportValidity()){this.query({period:'custom',...values});modal.close();}}),close));form.onsubmit=e=>e.preventDefault();}else this.query({period:period.value});};
    const device=el('select');device.setAttribute('aria-label','Устройство аналитики');for(const value of ['',...data.devices]){const option=el('option',value||'Все устройства');option.value=value;device.append(option);}device.value=query.device||'';device.onchange=()=>this.query({device:device.value});
    this.toolbar.replaceChildren(period,device,this.button('Обновить',()=>this.refresh()),this.navigation('Диагностика задержек','performance'));
    if(!data.policy.enabled){const info=card('Сбор данных выключен','Статистика не заполняется демонстрационными значениями. Старые измерения, если они есть, остаются до очистки или окончания срока хранения.');info.append(this.button('Настроить сбор',()=>{this.tab='collection';this.render();},true));this.content.append(info);}
    if(data.error)this.content.append(card('Часть измерений недоступна',data.error));if(data.dropped)this.content.append(card('Пропущенные события',`${data.dropped}. Очередь ограничена, чтобы не задерживать голосовой конвейер.`));
    if(this.tab==='collection'){this.collection();return;}
    if(!summary.events)this.content.append(card('За выбранный период данных пока нет',data.policy.enabled?'Сбор включён. Здесь появятся фактически сохранённые события выбранных категорий.':'Сбор выключен; измерений за этот период нет.'));
    if(this.tab==='overview'||this.tab==='reports')this.content.append(grid(metric('Активность Mac',summary.active_s?duration(summary.active_s):'Нет измерений'),metric('Обращения',String(summary.count)),metric('Выбранные приложения разработки',summary.development_s?duration(summary.development_s):'Нет измерений'),metric('Первый ответ',seconds(summary.mean_s))));
    if(this.tab==='overview'){
      this.content.append(grid(donut(summary.categories),barChart(summary.daily.map(d=>({label:new Date(d.day+'T12:00').toLocaleDateString('ru-RU',{weekday:'short',day:'numeric'}),value:d.requests,detail:`${d.requests} обращений · ${duration(d.active_s)}`})),'Обращения по дням')));this.insights();
    }else if(this.tab==='assistant'){
      this.content.append(grid(metric('Голосовые',String(summary.voice)),metric('Текстовые',String(summary.text)),metric('P50',seconds(summary.p50_s)),metric('P95',seconds(summary.p95_s))));
      this.content.append(barChart(summary.latencies.filter(r=>r.seconds!=null).map(r=>({label:date(r.at),value:r.seconds!,detail:seconds(r.seconds)})),'Задержка первого ответа'),barChart(summary.stages.map(s=>({label:s.name,value:s.mean_s,detail:`Среднее ${seconds(s.mean_s)} · P95 ${seconds(s.p95_s)}`})),'Этапы конвейера'),barChart(Object.entries(summary.providers).map(([name,count])=>({label:name,value:count,detail:`${count} запросов`})),'Наблюдавшиеся провайдеры'),card('Расходы API','Достоверные тарифы не подключены. Стоимость не оценивается.'));
    }else if(this.tab==='apps'||this.tab==='study'){
      this.content.append(grid(metric('Активность на переднем плане',summary.active_s?duration(summary.active_s):'Нет измерений'),metric('Простой',summary.idle_s?duration(summary.idle_s):'Нет измерений'),metric('Системные процессы',summary.system_s?duration(summary.system_s):'Нет измерений'),metric('Переключения приложений',String(summary.switches))));
      const apps=summary.apps.filter(app=>this.tab!=='study'||data.policy.study_apps.includes(app.bundle)||data.policy.development_apps.includes(app.bundle));
      this.content.append(applicationTimeChart(apps,summary.system_apps,summary.legacy_apps),card('Как это считается','Измеряется только приложение на переднем плане; интервалы не складывают время нескольких одновременно запущенных процессов. Простой определяется по отсутствию ввода в течение минуты. История без данных о простое сохранена отдельно. Фоновое время не собирается. Категории учёбы и разработки задаются вручную; время в IDE само по себе не считается продуктивностью.'),this.button('Выбрать приложения',()=>{this.tab='collection';this.render();}));
    }else if(this.tab==='devices'||this.tab==='media'){
      const selected=summary.commands.filter(r=>r.category?(this.tab==='media'?r.category==='media.event':r.category==='devices.command'):(this.tab==='media'?r.name.startsWith('media')||r.name.includes('music'):!r.name.startsWith('media')&&!r.name.includes('music')));this.content.append(grid(metric('Команды',String(selected.reduce((n,r)=>n+r.count,0))),metric('Подтверждено',String(selected.reduce((n,r)=>n+r.confirmed,0))),metric('Ошибки',String(selected.reduce((n,r)=>n+r.failed,0)))));this.content.append(barChart(selected.map(r=>({label:r.name,value:r.count,detail:`${r.count} запусков · ${r.confirmed} подтверждено · ${r.failed} ошибок`})),'Результаты команд'));
      if(this.tab==='media')this.content.append(card('Время просмотра','Время проигрывания нельзя вывести из числа команд. Подтверждённый прогресс хранится в медиатеке; длительность просмотра этой статистикой не подменяется.'),this.navigation('Открыть медиатеку','cinema'));
    }else{
      this.content.append(grid(...([['today','Сегодня'],['week','За неделю'],['month','За месяц']] as [string,string][]).map(([period,title])=>{const node=card(title);node.append(this.button('Открыть отчёт',()=>this.query({period})));return node;})));this.insights();
      const exportCard=card('Экспорт выбранного периода',`Часовой пояс: ${data.timezone}. Экспортируются минимальные события без переписок и экранов. PDF-экспорт пока недоступен.`);exportCard.append(row(this.button('JSON',()=>this.send({op:'export',format:'json'})),this.button('CSV',()=>this.send({op:'export',format:'csv'}))));this.content.append(exportCard);
    }
  }
  private insights():void {const data=this.state!;const node=card('Наблюдения');for(const insight of data.insights)node.append(el('p',insight));if(!data.insights.length)node.append(el('p','Для сравнения с предыдущим периодом пока недостаточно измерений.','muted'));this.content.append(node);}
  private collection():void {
    const data=this.state!,policy=data.policy;const {form,read}=formFields([{name:'enabled',label:'Собирать минимальную локальную статистику',kind:'checkbox',value:policy.enabled},{name:'retention_days',label:'Хранить события (дней)',kind:'number',value:policy.retention_days,min:1,max:3650},{name:'excluded_apps',label:'Исключённые приложения (имена или bundle ID, по одному в строке)',kind:'textarea',value:policy.excluded_apps.join('\n'),max:12000},{name:'development_apps',label:'Приложения разработки (bundle ID, по одному в строке)',kind:'textarea',value:policy.development_apps.join('\n'),max:12000},{name:'study_apps',label:'Учебные приложения (bundle ID, по одному в строке)',kind:'textarea',value:policy.study_apps.join('\n'),max:12000}]);
    const categories=el('fieldset');categories.append(el('legend','Категории сбора'));for(const [key,label]of Object.entries(data.categories)){const input=el('input');input.name='category';input.type='checkbox';input.value=key;input.checked=policy.categories.includes(key);const wrapper=el('label',label,'module-check');wrapper.append(input);categories.append(wrapper);}form.prepend(categories);form.append(this.button('Сохранить настройки',()=>{if(!form.reportValidity())return;const value=read();const split=(key:string)=>String(value[key]).split('\n').map(v=>v.trim()).filter(Boolean);this.send({op:'policy',value:{enabled:value.enabled,retention_days:Number(value.retention_days),categories:[...categories.querySelectorAll<HTMLInputElement>('input:checked')].map(i=>i.value),excluded_apps:split('excluded_apps'),development_apps:split('development_apps'),study_apps:split('study_apps')}});},true));form.onsubmit=e=>e.preventDefault();const node=card('Что записывается','Только после твоего включения: время, категория и тип события, длительность, статус, выбранное приложение, а для событий Чушпана — провайдер, модель, тип команды и числовые интервалы этапов. Устройства обозначаются локальным идентификатором. Тексты диалогов и окон, URL, снимки экрана и нажатия клавиш не записываются. Срок хранения и категории можно изменить ниже. Отключение прекращает новые события; сохранённые данные удаляются отдельно. Диагностические тайминги запросов хранятся отдельно и нужны для графиков производительности.');node.append(form,this.button('Очистить статистику',()=>{if(confirm('Удалить все события личной аналитики? Диалоги, память и диагностические трассы останутся.'))this.send({op:'clear',confirmed:true});}));this.content.append(node);
  }
}

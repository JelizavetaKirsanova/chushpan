import {ModulePage,card,date,el,empty,formFields,grid,metric,row,type FormField,type ModuleChannel} from './module_ui';

export interface MemoryItem {id:number;content:string;category:string;source:string;confidence:number;confirmed:number;sensitive:number;created_at:string;updated_at:string;confirmed_at?:string|null;revision:number;project_id:number|null;expires_at:string|null;status:string;}
interface ConversationItem {id:number;title:string;preview:string;memory_disabled?:number;}
interface MemoryPolicy {automatic?:string;excluded?:string[];temporary_days?:number;action_learning?:boolean;action_retention_days?:number;context_chars?:number;sensitive_context?:boolean;}
export interface MemoryData {
  items:MemoryItem[];projects:{id:number;name:string;description:string;memories:number;updated_at:string}[];
  total:number;used_today:number;categories:Record<string,string>;pending:{id:string;content:string}[];
  access:{id:number;content:string;at:string;purpose:string;request_id:string}[];fts:boolean;policy:MemoryPolicy;
  conversations:ConversationItem[];bindings:{project_id:number;conversation_id:number}[];
  page?:number;counts?:Record<string,number>;confirmed?:number;unconfirmed?:number;
  entities?:{id:number;name:string;memories:number}[];entity_links?:{memory_id:number;entity_id:number}[];
  relations?:{memory_id:number;related_id:number;kind:string;content:string;related_content:string}[];
  evidence?:{memory_id:number;conversation_id:number;message_id:number}[];
  versions?:{memory_id:number;content:string;revision:number;changed_at:string}[];
  summaries?:{conversation_id:number;content:string;updated_at:string}[];
  selected_conversation?:number|null;message_offset?:number;messages?:{id:number;role:string;content:string;created_at:string}[];
  history_matches?:{id:number;conversation_id:number;content:string;created_at:string}[];
  actions?:{id:number;conversation_id:number;intent:string;tool:string;state:string;error:string|null;correction:string|null;created_at:string}[];
  import_preview?:{token:string;count:number;sensitive:number;items:{content:string;category:string}[]}|null;
  backups?:{name:string;bytes:number}[];storage?:{facts:number;messages:number;bytes:number};
}
const actionLabels:Record<string,string>={sent:'Команда отправлена',acknowledged:'Устройство ответило',confirmed:'Результат подтверждён',unverified:'Результат не подтверждён',error:'Ошибка'};

export class MemoryLibraryView extends ModulePage<MemoryData> {
  private query='';private project='';private category='';private sort='recent';private entity:number|null=null;private page=0;private searchTimer=0;
  constructor(post:(channel:ModuleChannel,value:object)=>void,navigate:(view:string)=>void){
    super('memory_library','memory','Память','То, что помогает продолжать разговоры и учитывать твои предпочтения.',post,navigate,[['overview','Обзор'],['items','Воспоминания'],['relations','Связи'],['history','История'],['projects','Проекты'],['manage','Настройки памяти']]);
  }
  protected refresh():void {this.send({op:'list',query:this.query,project:this.project?Number(this.project):null,category:this.category,sort:this.sort,entity:this.entity,page:this.page});}
  protected render():void {
    if(!this.begin())return;const data=this.state!;
    const search=el('input');search.type='search';search.placeholder=this.tab==='history'?'Найти в разговорах':'Найти воспоминание';search.setAttribute('aria-label','Поиск воспоминаний');search.value=this.query;search.maxLength=200;
    search.oninput=()=>{this.query=search.value;this.page=0;clearTimeout(this.searchTimer);this.searchTimer=window.setTimeout(()=>this.refresh(),250);};
    const project=this.select('Проект памяти',[['','Все проекты'],...data.projects.map(p=>[String(p.id),p.name] as [string,string])],this.project,value=>{this.project=value;this.entity=null;this.page=0;this.refresh();});
    const category=this.select('Категория памяти',[['','Все категории'],...Object.entries(data.categories)],this.category,value=>{this.category=value;this.page=0;this.refresh();});
    const sort=this.select('Сортировка воспоминаний',[['recent','Недавно обновлены'],['oldest','Сначала старые'],['confidence','Сначала подтверждённые']],this.sort,value=>{this.sort=value;this.page=0;this.refresh();});
    this.toolbar.replaceChildren(search,project,category,sort,this.button('+ Воспоминание',()=>this.edit(),true));
    if(this.entity!==null)this.toolbar.append(this.button('Снять фильтр связи',()=>{this.entity=null;this.refresh();}));
    if(this.tab==='overview'){
      this.content.append(grid(metric('Воспоминания',String(data.total)),metric('Подтверждены',String(data.confirmed??data.items.filter(i=>i.confirmed).length)),metric('Ожидают подтверждения',String(data.unconfirmed??0)),metric('Использовано сегодня',String(data.used_today))));
      const categories=row(...Object.entries(data.counts??{}).map(([key,count])=>this.button(`${data.categories[key]??key} · ${count}`,()=>{this.category=key;this.page=0;this.tab='items';this.refresh();})));
      this.content.append(categories);
      if(data.pending.length){const suggestions=card('Предложения для сохранения',`${data.pending.length} записей ожидают твоего решения.`);suggestions.append(this.button('Рассмотреть предложения',()=>this.settings('memory')));this.content.append(suggestions);}
      this.content.append(el('h2','Недавние воспоминания','module-subtitle'));this.items(data.items.slice(0,8));
    }else if(this.tab==='items'){
      this.items(data.items);const prev=this.button('Предыдущие',()=>{this.page=Math.max(0,this.page-1);this.refresh();});prev.disabled=this.page===0;
      const next=this.button('Следующие',()=>{this.page++;this.refresh();});next.disabled=data.items.length<100;
      this.content.append(row(prev,el('span',`Страница ${this.page+1}`),next));
    }else if(this.tab==='relations')this.relations();
    else if(this.tab==='history')this.history();
    else if(this.tab==='projects'){
      this.content.append(row(this.button('+ Создать проект',()=>this.newProject(),true)));
      const cards=data.projects.map(project=>{const node=card(project.name,project.description||'Пространство памяти');node.append(el('p',`${project.memories} воспоминаний · ${date(project.updated_at)}`),row(this.button('Открыть',()=>{this.project=String(project.id);this.page=0;this.tab='items';this.refresh();}),this.button('Привязать разговор',()=>this.bindProject(project.id))));return node;});
      this.content.append(cards.length?grid(...cards):empty('Пока нет проектов','Создай пространство для учёбы или личного проекта.'));
    }else this.manage();
  }
  private select(label:string,options:[string,string][],value:string,change:(value:string)=>void):HTMLSelectElement {
    const select=el('select');select.setAttribute('aria-label',label);for(const [key,name]of options){const option=el('option',name);option.value=key;select.append(option);}select.value=value;select.onchange=()=>change(select.value);return select;
  }
  private items(items:MemoryItem[]):void {
    if(!items.length){this.content.append(empty('Пока пусто','Добавь воспоминание или попробуй другой поисковый запрос.'));return;}
    this.content.append(grid(...items.map(item=>{const node=card(this.state!.categories[item.category]||item.category);const body=el('button',item.content,'memory-open');body.onclick=()=>this.details(item);const expired=item.expires_at&&Date.parse(item.expires_at)<Date.now();node.append(body,el('small',`${date(item.updated_at)} · ${item.status==='stale'?'Неактуально':expired?'Срок истёк':item.confirmed?'Подтверждено':'Ожидает подтверждения'}${item.sensitive?' · Чувствительное':''}`),el('small',this.source(item.source)),row(this.button('Подробнее',()=>this.details(item))));return node;})));
  }
  private source(value:string):string {
    const labels:Record<string,string>={current_user_statement:'Твоя реплика',explicit_user:'Твоя просьба запомнить',explicit_correction:'Твоё исправление',desktop_library:'Добавлено вручную',user_import:'Импортировано',ui_confirmed:'Подтверждено тобой',legacy_unknown:'Источник старой записи неизвестен'};
    try{const source=JSON.parse(value) as {kind?:string};return labels[source.kind??'']??'Твоя настройка';}catch{return labels[value]??'Локальная запись';}
  }
  private details(item:MemoryItem):void {
    const data=this.state!,node=el('div');node.append(el('p',item.content,'memory-full'));const details=el('dl','','module-details');
    for(const [label,value]of [['Категория',data.categories[item.category]||item.category],['Проект',data.projects.find(p=>p.id===item.project_id)?.name||'Общая память'],['Источник',this.source(item.source)],['Уверенность',`${Math.round(item.confidence*100)}%`],['Создано',date(item.created_at)],['Обновлено',date(item.updated_at)],['Последнее подтверждение',item.confirmed_at?date(item.confirmed_at):'Дата не записана'],['Актуально до',item.expires_at?date(item.expires_at):'Без ограничения']])details.append(el('dt',label),el('dd',value));
    node.append(details,el('p','Подтверждение означает твоё утверждение или согласие. Независимая проверка не подразумевается.'));
    const modal=this.openDialog('Воспоминание',node);
    for(const ev of data.evidence?.filter(e=>e.memory_id===item.id)??[])node.append(this.button(`Связанный разговор ${ev.conversation_id}`,()=>{modal.close();this.tab='history';this.send({op:'conversation_open',id:ev.conversation_id});}));
    for(const version of data.versions?.filter(v=>v.memory_id===item.id)??[])node.append(card(`Версия ${version.revision} · ${date(version.changed_at)}`,version.content));
    node.append(row(this.button('Редактировать',()=>{modal.close();this.edit(item);}),this.button('Подтвердить',()=>{this.send({op:'confirm',id:item.id,revision:item.revision});modal.close();}),this.button('Связать',()=>{modal.close();this.link(item);}),this.button('Удалить',()=>{if(confirm('Удалить это воспоминание и его производные записи: '+item.content+'?')){this.send({op:'delete',id:item.id,revision:item.revision,confirmed:true});modal.close();}})));
  }
  private edit(item?:MemoryItem):void {
    const names=this.state!.entities?.filter(e=>this.state!.entity_links?.some(l=>l.memory_id===item?.id&&l.entity_id===e.id)).map(e=>e.name).join(', ')??'';
    const fields:FormField[]=[{name:'content',label:'Что запомнить',kind:'textarea',value:item?.content,max:300,required:true},{name:'category',label:'Категория',kind:'select',value:item?.category||'profile',options:Object.entries(this.state!.categories)},{name:'project',label:'Проект',kind:'select',value:item?.project_id??'',options:[['','Общая память'],...this.state!.projects.map(p=>[String(p.id),p.name] as [string,string])]},{name:'entities',label:'Связанные имена и названия через запятую',value:names,max:500},{name:'expires_at',label:'Срок актуальности (необязательно)',kind:'date',value:item?.expires_at?.slice(0,10)||''},{name:'consent_sensitive',label:'Отдельно разрешаю сохранить чувствительные сведения этой записи',kind:'checkbox'}];
    const {form,read}=formFields(fields);const modal=this.openDialog(item?'Редактировать воспоминание':'Новое воспоминание',form);
    form.append(this.button('Сохранить',()=>{if(!form.reportValidity())return;const value=read();const entities=String(value.entities).split(',').map(s=>s.trim()).filter(Boolean);this.send({op:item?'update':'add',...value,id:item?.id,revision:item?.revision,project:value.project?Number(value.project):null,sensitive:item?.sensitive===1,...(entities.length?{entities}:{})});modal.close();},true));form.onsubmit=e=>e.preventDefault();
  }
  private relations():void {
    const data=this.state!;this.content.append(el('p','Имена и названия связывают упоминания. Причины и успешные решения добавляются только явно.'));
    for(const entity of data.entities??[]){const node=card(entity.name,`${entity.memories} воспоминаний`);node.append(this.button('Открыть связанные',()=>{this.entity=entity.id;this.page=0;this.tab='items';this.refresh();}));this.content.append(node);}
    const labels:Record<string,string>={related:'Связано',corrects:'Исправляет',depends_on:'Зависит от',solves:'Решает'};
    for(const relation of data.relations??[])this.content.append(card(`${labels[relation.kind]??'Связь'}: ${relation.content}`,relation.related_content));
    if(!data.entities?.length&&!data.relations?.length)this.content.append(empty('Связей пока нет','Укажи имена или названия в воспоминании, либо свяжи две записи через «Подробнее».'));
  }
  private link(item:MemoryItem):void {
    const candidates=this.state!.items.filter(i=>i.id!==item.id&&i.project_id===item.project_id);
    if(!candidates.length){this.feedback.textContent='Добавь ещё одно воспоминание в этом пространстве.';return;}
    const {form,read}=formFields([{name:'related_id',label:'Связанное воспоминание',kind:'select',options:candidates.map(i=>[String(i.id),i.content])},{name:'kind',label:'Связь',kind:'select',options:[['related','Связано'],['corrects','Исправляет'],['depends_on','Зависит от'],['solves','Решает']]}]);
    const modal=this.openDialog('Связь воспоминаний',form);form.append(this.button('Добавить связь',()=>{this.send({op:'link',id:item.id,related_id:Number(read().related_id),kind:read().kind});modal.close();},true));form.onsubmit=e=>e.preventDefault();
  }
  private history():void {
    const data=this.state!;
    for(const match of data.history_matches??[]){const node=card(date(match.created_at),match.content);node.append(this.button('Открыть разговор',()=>this.send({op:'conversation_open',id:match.conversation_id})));this.content.append(node);}
    if(data.selected_conversation){
      const cid=data.selected_conversation;const heading=card(`Разговор ${cid}`);heading.append(this.button('Не учиться на разговоре',()=>{if(confirm('Отключить сохранение новых реплик и обучение для этого разговора?'))this.send({op:'conversation_private',id:cid});}));this.content.append(heading);
      const summary=data.summaries?.find(s=>s.conversation_id===cid);if(summary)this.content.append(card('Краткое резюме · выдержки из твоих реплик',summary.content));
      for(const message of data.messages??[])this.content.append(card(`${message.role==='user'?'Ты':'Чушпан'} · ${date(message.created_at)}`,message.content));
      const offset=data.message_offset??0;const older=this.button('Более ранние сообщения',()=>this.send({op:'conversation_open',id:cid,offset:offset+200}));older.disabled=(data.messages?.length??0)<200;const newer=this.button('Более поздние сообщения',()=>this.send({op:'conversation_open',id:cid,offset:Math.max(0,offset-200)}));newer.disabled=offset===0;this.content.append(row(older,newer));
      const ids=new Set(data.evidence?.filter(e=>e.conversation_id===cid).map(e=>e.memory_id));const facts=data.items.filter(i=>ids.has(i.id));if(facts.length){this.content.append(el('h2','Связанные факты'));this.items(facts);}
      const {form,read}=formFields([{name:'delete_facts',label:'Также удалить извлечённые из разговора факты',kind:'checkbox'}]);form.append(this.button('Удалить разговор',()=>{if(confirm('Удалить этот разговор навсегда?'))this.send({op:'conversation_delete',id:cid,delete_facts:read().delete_facts,confirmed:true});},true));this.content.append(form);
    }
    for(const conversation of data.conversations){const node=card(conversation.title||conversation.preview||`Разговор ${conversation.id}`,conversation.memory_disabled?'Сохранение новых реплик отключено':'');node.append(this.button('Открыть',()=>this.send({op:'conversation_open',id:conversation.id})));this.content.append(node);}
    if(!data.conversations.length&&!data.selected_conversation)this.content.append(empty('История пока пуста','Здесь появятся сохранённые диалоги и связанные факты.'));
    this.content.append(el('h2','Действия и результаты','module-subtitle'));
    for(const action of data.actions??[])this.content.append(card(`${actionLabels[action.state]??'Результат не подтверждён'} · ${date(action.created_at)}`,`${action.intent}${action.error?' · '+action.error:''}${action.correction?' · Исправление: '+action.correction:''}`));
  }
  private manage():void {
    const data=this.state!,policy=data.policy;
    const {form,read}=formFields([{name:'automatic',label:'Автоматическое запоминание',kind:'select',value:policy.automatic??'auto',options:[['auto','Сохранять прямые утверждения'],['ask','Предлагать для подтверждения'],['off','Полностью отключить обучение']]},{name:'temporary_days',label:'Срок временных фактов, дней',kind:'number',min:1,max:3650,value:policy.temporary_days??30},{name:'action_learning',label:'Сохранять результаты действий',kind:'checkbox',value:policy.action_learning??true},{name:'action_retention_days',label:'Хранить действия, дней',kind:'number',min:1,max:3650,value:policy.action_retention_days??90},{name:'context_chars',label:'Объём выбранной памяти в ответе, символов',kind:'number',min:300,max:6000,value:policy.context_chars??1600},{name:'sensitive_context',label:'Отдельно разрешаю использовать вручную сохранённые чувствительные факты в ответах',kind:'checkbox',value:policy.sensitive_context??false}]);
    const excluded=new Set(policy.excluded??[]);for(const [key,label]of Object.entries(data.categories).filter(([key])=>key!=='personality')){const check=el('input');check.type='checkbox';check.checked=excluded.has(key);check.onchange=()=>check.checked?excluded.add(key):excluded.delete(key);const wrapper=el('label',`Не запоминать: ${label}`,'module-check');wrapper.append(check);form.append(wrapper);}
    form.append(this.button('Сохранить настройки',()=>{if(!form.reportValidity())return;const value=read();if(value.sensitive_context&&!policy.sensitive_context&&!confirm('Разрешить использовать чувствительные факты в контексте настроенной модели, включая облачную, если она выбрана?'))return;this.send({op:'policy',value:{...value,excluded:[...excluded],temporary_days:Number(value.temporary_days),action_retention_days:Number(value.action_retention_days),context_chars:Number(value.context_chars)}});},true));form.onsubmit=e=>e.preventDefault();
    this.content.append(card('Локальная память','Чувствительные сведения автоматически не извлекаются. Секреты не сохраняются. Отключение обучения оставляет ручное управление.'),form);
    const transfer=card('Твои данные','JSON содержит личные факты. Для защищённой локальной копии используй парольную фразу.');const file=el('input');file.type='file';file.accept='.json,application/json';file.hidden=true;file.onchange=async()=>{const selected=file.files?.[0];if(!selected)return;if(selected.size>1000000){this.feedback.textContent='Выбери файл не больше 1 МБ.';return;}this.send({op:'import_preview',content:await selected.text()});};
    transfer.append(row(this.button('Экспорт JSON',()=>this.send({op:'export'})),this.button('Импорт с просмотром',()=>file.click()),this.button('Создать защищённую копию',()=>this.backupDialog())),file);this.content.append(transfer);
    const preview=data.import_preview;if(preview){const node=card(`Проверь импорт · ${preview.count} записей`);for(const item of preview.items)node.append(el('p',`${data.categories[item.category]??item.category}: ${item.content}`));const consent=el('input');consent.type='checkbox';if(preview.sensitive){const label=el('label',`Отдельно разрешаю импорт ${preview.sensitive} чувствительных записей`);label.append(consent);node.append(label);}node.append(row(this.button('Подтвердить импорт',()=>this.send({op:'import_apply',token:preview.token,confirmed:true,consent_sensitive:consent.checked}),true),this.button('Отмена импорта',()=>this.send({op:'import_cancel'}))));this.content.append(node);}
    for(const backup of data.backups??[]){const node=card('Защищённая копия',backup.name);node.append(row(this.button('Восстановить с просмотром',()=>this.backupDialog(backup.name)),this.button('Удалить копию',()=>{if(confirm('Удалить эту резервную копию?'))this.send({op:'backup_delete',name:backup.name,confirmed:true});})));this.content.append(node);}
    if(data.storage)this.content.append(card('Объём памяти',`${data.storage.facts} фактов · ${data.storage.messages} сообщений · ${(data.storage.bytes/1048576).toFixed(1)} МБ в SQLite`));
    const deletion=card('Очистка памяти','Удаляются факты, редакции, связи и производные индексы. История и старые копии очищаются отдельно.');deletion.append(this.button('Очистить выбранную категорию',()=>{if(confirm(`Удалить ${this.category?data.categories[this.category]:'все воспоминания'}? Старые резервные копии останутся.`))this.send({op:'clear',category:this.category||null,confirmed:true});},true));this.content.append(deletion);
  }
  private backupDialog(name?:string):void {
    const {form,read}=formFields([{name:'password',label:name?'Парольная фраза копии':'Новая парольная фраза (от 12 символов)',kind:'text',required:true,max:512}]);const input=form.querySelector<HTMLInputElement>('[name=password]')!;input.type='password';input.minLength=12;input.autocomplete='off';
    const modal=this.openDialog(name?'Открыть защищённую копию':'Защищённая локальная копия',form);form.append(el('p','Парольная фраза не сохраняется. Без неё восстановление невозможно.'),this.button(name?'Предпросмотр восстановления':'Создать копию',()=>{if(form.reportValidity()){this.send({op:name?'backup_preview':'backup',name,password:read().password});input.value='';modal.close();}},true));form.onsubmit=e=>e.preventDefault();
  }
  private newProject():void {const {form,read}=formFields([{name:'name',label:'Название',required:true,max:80},{name:'description',label:'Описание',kind:'textarea',max:1000}]);const modal=this.openDialog('Новый проект',form);form.append(this.button('Создать',()=>{if(form.reportValidity()){this.send({op:'project_add',...read()});modal.close();}},true));form.onsubmit=e=>e.preventDefault();}
  private bindProject(project:number):void {const {form,read}=formFields([{name:'conversation',label:'Разговор',kind:'select',options:this.state!.conversations.map(c=>[String(c.id),c.title||c.preview||`Разговор ${c.id}`])}]);const modal=this.openDialog('Контекст проекта',form);form.append(el('p','В этом разговоре поиск памяти будет ограничен выбранным проектом.'),this.button('Привязать',()=>{this.send({op:'project_bind',project,conversation:Number(read().conversation)});modal.close();},true));form.onsubmit=e=>e.preventDefault();}
}

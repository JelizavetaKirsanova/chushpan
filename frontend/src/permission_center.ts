import {icon} from './dashboard';

export interface PermissionItem {
  id:string; name:string; status:string; note:string; url:string;
  group?:string; group_name?:string; icon?:string; purpose?:string; features?:string[];
  mechanism?:string; can_request?:boolean; supported?:boolean; last_error?:string|null;
  details?:Record<string,unknown>;
}
export interface PermissionReport {
  items:PermissionItem[]; scope:string; message?:string|null;
  history?:{at:string;id:string;action:string;status:string;error?:string|null;identity?:object;osstatus?:number|null}[];
}
export interface PermissionFilters {search:string; group:string; problems:boolean}
const labels:Record<string,string> = {granted:'Разрешено',denied:'Запрещено',not_determined:'Не запрашивалось',check_needed:'Требуется проверка',unavailable:'Недоступно',unsupported:'Недоступно',unknown:'Требуется проверка'};
const el = (tag:string,text='',cls='') => {const node=document.createElement(tag);node.textContent=text;node.className=cls;return node;};

export function permissionCenter(root:HTMLElement, report:PermissionReport|null,
  send:(value:object)=>void, filters:PermissionFilters, online=true):void {
  const button=(label:string,action:()=>void)=>{const b=el('button',label) as HTMLButtonElement;b.type='button';b.disabled=!online;b.onclick=action;return b;};
  const act=(id:string,action:string)=>send({op:'permission_action',id,action});
  root.replaceChildren();
  root.append(button('Проверить используемые разрешения',()=>send({op:'permissions'})),
    el('p','Проверка не записывает звук, не запускает музыку и не управляет устройствами. Доступ запрашивается только по кнопке или при использовании функции.','settings-help'));
  if(!report)return;
  root.append(el('p',report.scope,'settings-help'));
  if(report.message)root.append(el('p',report.message,'settings-help'));
  const toolbar=el('div','','permission-toolbar');
  const search=el('input') as HTMLInputElement;search.type='search';search.placeholder='Поиск разрешений и функций';search.value=filters.search;search.setAttribute('aria-label','Поиск разрешений');
  const group=el('select') as HTMLSelectElement;group.setAttribute('aria-label','Категория разрешений');
  const groups=new Map(report.items.map(i=>[i.group??'other',i.group_name??'Другие']));
  for(const [key,title] of [['', 'Все категории'],...groups]){const option=el('option',title) as HTMLOptionElement;option.value=key;group.append(option);}group.value=filters.group;
  const problems=el('input') as HTMLInputElement;problems.type='checkbox';problems.checked=filters.problems;
  const label=el('label','Только проблемы');label.prepend(problems);toolbar.append(search,group,label);root.append(toolbar);
  const list=el('div','','permission-list');root.append(list);
  const render=()=>{
    list.replaceChildren();let previous='';
    const found=report.items.filter(i=>(!filters.group||(i.group??'other')===filters.group)&&(!filters.problems||!['granted','unavailable','unsupported'].includes(i.status))&&[i.name,i.purpose??i.note,...i.features??[]].join(' ').toLocaleLowerCase().includes(filters.search.toLocaleLowerCase()));
    for(const item of found){
      if(previous!==(item.group??'other')){previous=item.group??'other';list.append(el('h3',item.group_name??'Другие'));}
      const card=el('article','','settings-permission permission-card');card.dataset.permission=item.id;
      const heading=el('div','','permission-heading');const glyph=el('span','','permission-icon');glyph.innerHTML=icon(item.icon??'settings');glyph.setAttribute('aria-hidden','true');
      const status=el('span',labels[item.status]??'Требуется проверка','permission-status');status.dataset.status=item.status;
      heading.append(glyph,el('strong',item.name),status);card.append(heading);
      if(item.purpose)card.append(el('p',item.purpose));else if(item.note){const info=el('details');info.append(el('summary','Подробнее'),el('p',item.note));card.append(info);}
      if(item.features?.length)card.append(el('small',`Функции: ${item.features.join(', ')}`));
      if(item.mechanism)card.append(el('small',`Доступ: ${item.mechanism}`));
      if(item.last_error)card.append(el('p',item.last_error,'settings-error'));
      const actions=el('div','','permission-actions');
      if(item.can_request&&item.status!=='granted')actions.append(button('Запросить доступ',()=>act(item.id,'request')));
      if(item.url&&item.supported!==false)actions.append(button('Открыть настройки',()=>act(item.id,'open')));
      actions.append(button('Проверить доступ',()=>act(item.id,'check')));card.append(actions);
      if(item.details&&Object.keys(item.details).length){const details=el('details');details.append(el('summary','Для разработчика: исполнитель и результат API'),el('pre',JSON.stringify(item.details,null,2)));card.append(details);}
      list.append(card);
    }
    if(!found.length)list.append(el('p','Разрешений по этому фильтру нет.','settings-help'));
  };
  search.oninput=()=>{filters.search=search.value;render();};group.onchange=()=>{filters.group=group.value;render();};problems.onchange=()=>{filters.problems=problems.checked;render();};render();
  const history=el('details','','permission-history');history.append(el('summary','Локальный журнал проверок'));
  for(const entry of report.history??[]){const line=el('details');line.append(el('summary',`${new Date(entry.at).toLocaleString('ru-RU')} · ${report.items.find(i=>i.id===entry.id)?.name??entry.id} · ${labels[entry.status]??entry.status}`),el('pre',JSON.stringify(entry,null,2)));history.append(line);}
  if(!report.history?.length)history.append(el('p','Проверок пока нет.'));root.append(history);
}

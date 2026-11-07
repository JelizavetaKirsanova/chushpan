export interface BrowserDiagnosticReport {
  id?:string;running:boolean;status:string;source?:string;title?:string;url?:string;region?:string;
  region_access?:string;identity_status?:string;episode_status?:string;content_verified?:boolean;
  video_type?:string;player_found?:boolean;playback_confirmed?:boolean;currentTime_before?:number;
  currentTime_after?:number;duration?:number;fullscreen?:boolean;message?:string;elapsed_ms?:number;
  report_path?:string;screenshot_path?:string;structure_changed?:boolean;
  stages?:{name:string;status:string;message:string;elapsed_ms:number}[];
  network_errors?:{url:string;error:string}[];javascript_errors?:string[];
  frames?:{url:string;videos:number;iframe_count:number;controls:{role:string;label:string}[]}[];
}
function el<K extends keyof HTMLElementTagNameMap>(tag:K,text=''):HTMLElementTagNameMap[K]{const n=document.createElement(tag);n.textContent=text;return n;}
export async function copyDiagnostic(value:unknown,target:HTMLElement):Promise<void>{
  const text=JSON.stringify(value,null,2);
  try{await navigator.clipboard.writeText(text);target.textContent='Отчёт скопирован';}
  catch{const area=el('textarea');area.value=text;area.readOnly=true;area.setAttribute('aria-label','Диагностический отчёт для копирования');target.replaceChildren(area);area.focus();area.select();}
}
export function browserDiagnosticsPanel(report:BrowserDiagnosticReport|undefined,sources:{id:string;name:string;enabled:boolean;adapter:string}[],items:{id:string;title:string}[],send:(value:object)=>void,online:boolean):HTMLElement{
  const root=el('section');root.className='cinema-preference-card';root.dataset.browserDiagnostics='';
  root.append(el('h3','Playwright · проверка внешнего сайта'),el('p','Отдельный изолированный браузер проверяет страницу, iframe, плеер и сеть. Проверка с Play запускает только подтверждённое полное видео без изменения прогресса медиатеки.'));
  const title=el('select');title.setAttribute('aria-label','Произведение для Playwright');for(const item of items){const o=el('option',item.title);o.value=item.id;title.append(o);}
  const source=el('select');source.setAttribute('aria-label','Источник для Playwright');for(const s of sources.filter(s=>s.enabled&&s.adapter!=='local')){const o=el('option',s.name);o.value=s.id;source.append(o);}
  const button=(label:string,action:()=>void,disabled=false)=>{const b=el('button',label);b.type='button';b.disabled=!online||disabled;b.onclick=action;return b;};
  root.append(title,source,button('Исследовать страницу',()=>send({op:'browser_diagnostics',title_id:title.value,source_id:source.value}),!!report?.running||!items.length||!source.options.length),button('Проверить Play и currentTime',()=>send({op:'browser_diagnostics',title_id:title.value,source_id:source.value,play:true}),!!report?.running||!items.length||!source.options.length));
  if(report?.running)root.append(button('Отменить проверку',()=>send({op:'browser_diagnostics_cancel'})));
  if(!report)return root;
  const status=el('p',report.running?'Проверка выполняется…':report.message??'Проверка ещё не выполнялась.');status.setAttribute('role','status');root.append(status);
  if(!report.id)return root;
  root.append(el('p',`${report.source} · ${report.title} · регион ${report.region}; географическое положение внешнего IP отдельно не подтверждалось.`));
  const labels:[string,string][]=[['identity_status','Совпадение произведения'],['episode_status','Сезон и серия'],['region_access','Регион'],['player_found','Плеер'],['content_verified','Полное видео'],['playback_confirmed','Движение currentTime']];
  const data=report as unknown as Record<string,unknown>;for(const [name,label]of labels){const v=data[name];root.append(el('p',`${label}: ${v===true||v==='confirmed'||v==='allowed'?'подтверждено':v==='probable'?'вероятное совпадение':v==='blocked'?'доступ ограничен':v===false?'не подтверждено':'не удалось проверить'}`));}
  if(report.currentTime_after!==undefined)root.append(el('p',`currentTime: ${report.currentTime_before?.toFixed(3)} → ${report.currentTime_after.toFixed(3)} с; длительность ${report.duration?.toFixed(1)} с`));
  if(report.structure_changed)root.append(el('p','Структура страницы изменилась. Элементы плеера найдены заново.'));
  const details=el('details');details.append(el('summary','Этапы, iframe и ошибки'));
  for(const s of report.stages??[])details.append(el('p',`${s.name} · ${s.elapsed_ms} мс · ${s.message}`));
  for(const f of report.frames??[])details.append(el('p',`${f.url} · video: ${f.videos} · iframe: ${f.iframe_count} · ${f.controls.map(c=>c.label).filter(Boolean).join(', ')}`));
  for(const e of report.network_errors??[])details.append(el('p',`${e.error} · ${e.url}`));
  for(const e of report.javascript_errors??[])details.append(el('p',`JavaScript: ${e}`));root.append(details);
  const copyStatus=el('span');root.append(button('Копировать отчёт Playwright',()=>{void copyDiagnostic(report,copyStatus);}),copyStatus);
  if(report.screenshot_path){const image=el('img');image.src=`/cinema/diagnostics/${report.id}/image`;image.alt='Снимок исследованной страницы';image.style.maxWidth='100%';image.loading='lazy';root.append(image);}
  root.append(el('small',`Отчёт сохранён локально: ${report.report_path}`));return root;
}

import type {CinemaEvent} from './cinema';

export interface DiagnosticStage {status:string;message:string;method?:string;checked_at?:string;code?:string}
export interface SourceDiagnosticReport {running:boolean;rows:{source_id:string;name:string;url:string;title:string;page_url?:string;video_type?:string;engine?:string;playback_confirmed_at?:string|null;verification_status?:string;last_successful_at?:string|null;error_history?:{stage:string;code:string;message:string;checked_at?:string}[];candidate?:{embedded?:boolean;content_verified?:boolean}|null;stages:Record<string,DiagnosticStage>;last_error?:{stage:string;message:string;code:string}|null}[]}
const phases:[string,string][]=[['site','Сайт'],['content','Название'],['player','Плеер'],['playback','Запуск'],['pause','Пауза'],['seek','Перемотка'],['fullscreen','Полный экран'],['resume','Продолжение']];
const stateLabels:Record<string,string>={confirmed:'Подтверждено',failed:'Ошибка',needs_user:'Нужно действие',not_checked:'Не проверено',unsupported:'Не поддерживается'};
function el<K extends keyof HTMLElementTagNameMap>(tag:K,text=''):HTMLElementTagNameMap[K]{const n=document.createElement(tag);n.textContent=text;return n;}

/** HTTP status and a real advancing player are independent pieces of evidence. */
export class CinemaSourceDiagnostics {
  private root=el('section');
  private data:CinemaEvent|null=null;
  private online=false;
  private selected='';
  constructor(private send:(value:object)=>void){this.root.className='cinema-source-diagnostics';this.root.setAttribute('aria-label','Проверка источников');}
  mount(target:HTMLElement):void{target.append(this.root);this.render();}
  connection(value:boolean):void{this.online=value;this.render();}
  receive(data:CinemaEvent):void{this.data=data;this.render();}
  private button(label:string,action:()=>void,disabled=false):HTMLButtonElement{const b=el('button',label);b.type='button';b.disabled=!this.online||disabled;b.onclick=action;return b;}
  private render():void{
    const report=this.data?.source_diagnostics;const busy=report?.running??false;
    const heading=el('h3','Проверить источники');const description=el('p','Сначала проверяется доступность и поиск. Проверка плеера отдельно откроет выбранное видео; позиция и история медиатеки не изменятся.');
    const controls=el('div');controls.className='cinema-source-diagnostic-actions';
    const select=el('select');select.setAttribute('aria-label','Название для проверки источников');select.append(el('option','Только доступность сайтов'));
    select.options[0].value='';for(const item of this.data?.items??[]){const option=el('option',`${item.title}${item.year?` (${item.year})`:''}`);option.value=item.id;select.append(option);}select.value=this.selected;select.disabled=!this.online||busy;select.onchange=()=>{this.selected=select.value;};
    controls.append(select,this.button(busy?'Проверка выполняется…':'Проверить все источники',()=>this.send({op:'diagnose_sources',title_id:select.value}),busy));
    const progress=el('p',busy?'Проверка выполняется. Запуск считается подтверждённым только при движении времени видео.':'Автоматическая проверка не запускает видео.');progress.setAttribute('role','status');
    this.root.replaceChildren(heading,description,controls,progress);
    if(!report?.rows.length)return;
    const wrap=el('div');wrap.className='cinema-diagnostic-table-wrap';const table=el('table');table.className='cinema-diagnostic-table';const head=el('tr');head.append(el('th','Источник'),...phases.map(([,label])=>el('th',label)),el('th','Последняя ошибка / действие'));const thead=el('thead');thead.append(head);table.append(thead);const body=el('tbody');
    for(const row of report.rows){
      const tr=el('tr');const name=el('th',row.name);name.scope='row';if(row.title)name.append(el('small',row.title));if(row.playback_confirmed_at)name.append(el('small',`Воспроизведение подтверждено ${new Date(row.playback_confirmed_at).toLocaleString('ru-RU')}`));tr.append(name);
      for(const [key,label]of phases){const stage=row.stages[key];const cell=el('td',stateLabels[stage?.status??'not_checked']??'Не проверено');cell.dataset.status=stage?.status??'not_checked';cell.title=stage?.message??'Проверка ещё не выполнена.';cell.setAttribute('aria-label',`${label}: ${cell.textContent}. ${cell.title}`);tr.append(cell);}
      const detail=el('td');if(row.last_error)detail.append(el('p',row.last_error.message));const notes=el('details');notes.append(el('summary','Результаты и ограничения'));for(const [key,label]of phases){const stage=row.stages[key];if(stage?.status!=='not_checked'&&stage)notes.append(el('p',`${label} · ${stage.method==='http'?'автоматически, HTTP':'проверка плеера'}: ${stage.message}`));}if(row.page_url){const link=el('a','Проверенная страница');link.href=row.page_url;link.target='_blank';link.rel='noopener noreferrer';notes.append(link);}detail.append(notes);
      if(row.candidate){detail.append(this.button('Проверить плеер в Safari',()=>this.send({op:'diagnose_player',source_id:row.source_id,engine:'safari'}),busy));if(row.candidate.embedded&&row.candidate.content_verified)detail.append(this.button('Проверить в AVKit',()=>this.send({op:'diagnose_player',source_id:row.source_id,engine:'native'}),busy));}
      tr.append(detail);body.append(tr);
    }
    table.append(body);wrap.append(table);const details=el('details');details.append(el('summary','Подробные проверки и запуск видео'),wrap);this.root.append(details);
  }
}

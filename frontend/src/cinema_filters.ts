import type {CatalogueConfig,CatalogueFilters} from './cinema_catalogue';

const node=<K extends keyof HTMLElementTagNameMap>(tag:K,text='',cls=''):HTMLElementTagNameMap[K]=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
export function activeFilterCount(f:CatalogueFilters):number {
  return f.genres.length+Number(!!(f.year||f.year_min||f.year_max))+Number(!!f.rating)+Number(!!f.country)+Number(!!f.original_language)+Number(f.kind!=='all')+[f.russian_titles,f.hide_animation,f.hide_anime,f.hide_documentary,f.hide_watched,f.hide_watchlist,f.hide_saved].filter(Boolean).length;
}

/** Draft controls do not issue requests until Apply. Native dialog traps focus. */
export class CinemaFilterDrawer {
  private dialog:HTMLDialogElement|null=null;
  open(parent:HTMLElement,initial:CatalogueFilters,config:CatalogueConfig|null,defaults:()=>CatalogueFilters,apply:(filters:CatalogueFilters)=>void):void {
    this.dialog?.close();const dialog=node('dialog','','cinema-filter-drawer');this.dialog=dialog;dialog.setAttribute('aria-label','Фильтры каталога');
    let draft={...initial,genres:[...initial.genres]};
    const button=(label:string,action:()=>void)=>{const b=node('button',label);b.type='button';b.onclick=action;return b;};
    const field=(label:string,input:HTMLElement)=>{const row=node('label','','cinema-field');input.setAttribute('aria-label',label);row.append(node('span',label),input);return row;};
    const checkbox=(label:string,checked:boolean,change:(v:boolean)=>void)=>{const input=node('input');input.type='checkbox';input.value=label;input.checked=checked;input.onchange=()=>{change(input.checked);count();};const row=field(label,input);row.className='cinema-switch';return row;};
    const select=(label:string,values:[string,string][],value:string,change:(v:string)=>void)=>{const s=node('select');for(const [id,name]of values){const option=node('option',name);option.value=id;s.append(option);}s.value=value;s.onchange=()=>{change(s.value);count();};return field(label,s);};
    let submit:HTMLButtonElement;let error:HTMLElement;
    const count=()=>{if(submit)submit.textContent=`Применить (${activeFilterCount(draft)})`;};
    const render=()=>{
      const header=node('header');const close=button('×',()=>dialog.close());close.setAttribute('aria-label','Закрыть фильтры');header.append(node('h2','Фильтры'),close);
      const form=node('form');const genres=node('fieldset');genres.append(node('legend','Жанры'));
      const search=node('input');search.type='search';search.placeholder='Поиск жанров…';search.setAttribute('aria-label','Поиск жанров');genres.append(search);
      const available=draft.kind==='movie'?config?.genres_by_kind?.movie:draft.kind==='series'?config?.genres_by_kind?.series:config?.genres;
      const options=node('div','','cinema-filter-genres');for(const genre of [...new Set([...(available??config?.genres??['триллер','детектив','драма','комедия','ужасы','фантастика','мультфильм']).map(g=>g.toLocaleLowerCase('ru')),...draft.genres])]){
        options.append(checkbox(genre,draft.genres.includes(genre),checked=>{draft.genres=checked?[...draft.genres,genre]:draft.genres.filter(v=>v!==genre);draft.genre='';}));
      }
      search.oninput=()=>{for(const option of [...options.children] as HTMLElement[])option.hidden=!option.textContent?.toLocaleLowerCase('ru').includes(search.value.toLocaleLowerCase('ru'));};
      genres.append(options,select('Режим жанров',[['or','Любой выбранный (OR)'],['and','Все выбранные (AND)']],draft.genre_mode,v=>draft.genre_mode=v as 'or'|'and'));form.append(genres);
      const years=node('fieldset');years.append(node('legend','Год выпуска'));const bounds=node('div','','cinema-range-values');const ranges=node('div','','cinema-year-ranges');
      const first=1870,last=new Date().getFullYear()+3;
      for(const [key,label,fallback]of [['year_min','Год от',first],['year_max','Год до',last]] as const){
        const value=node('input');value.type='number';value.min='0';value.max='2200';value.placeholder=String(fallback);value.value=String(draft[key]||draft.year||'');
        const range=node('input');range.type='range';range.min=String(first);range.max=String(last);range.value=String(draft[key]||draft.year||fallback);range.setAttribute('aria-label',`${label} · ползунок`);
        value.oninput=()=>{draft[key]=Number(value.value)||0;draft.year=0;range.value=String(draft[key]||fallback);count();};range.oninput=()=>{draft[key]=Number(range.value);draft.year=0;value.value=range.value;count();};bounds.append(field(label,value));ranges.append(range);
      }years.append(bounds,ranges);form.append(years);
      const rating=node('input');rating.type='range';rating.min='0';rating.max='10';rating.step='.5';rating.value=String(draft.rating);const ratingValue=node('output',`от ${draft.rating.toFixed(1)}`);rating.oninput=()=>{draft.rating=Number(rating.value);ratingValue.textContent=`от ${draft.rating.toFixed(1)}`;count();};form.append(field('Минимальный рейтинг TMDB',rating),ratingValue,
        select('Сортировка каталога',[['popularity','По популярности'],['rating','По рейтингу'],['date','По дате выхода'],['title','По названию']],draft.sort,v=>draft.sort=v),
        select('Тип контента',[['all','Всё'],['movie','Фильмы'],['series','Сериалы']],draft.kind,v=>{draft.kind=v;render();}),
        select('Страна производства',[['','Любая страна'],...(config?.countries??[{id:'EE',name:'Эстония'},{id:'US',name:'США'},{id:'RU',name:'Россия'},{id:'JP',name:'Япония'}]).map(c=>[c.id,c.name] as [string,string])],draft.country,v=>draft.country=v),
        select('Язык оригинала',[['','Любой язык'],...(config?.languages??[{id:'ru',name:'Русский'},{id:'en',name:'Английский'}]).map(c=>[c.id,c.name] as [string,string])],draft.original_language,v=>draft.original_language=v));
      const extra=node('fieldset');extra.append(node('legend','Дополнительно'),checkbox('Только названия с русскими буквами',draft.russian_titles!==false,v=>draft.russian_titles=v),checkbox('Скрывать мультфильмы',draft.hide_animation!==false,v=>draft.hide_animation=v),checkbox('Скрывать аниме',draft.hide_anime!==false,v=>draft.hide_anime=v),checkbox('Скрывать документальные фильмы',!!draft.hide_documentary,v=>draft.hide_documentary=v),checkbox('Скрывать просмотренное',!!draft.hide_watched,v=>draft.hide_watched=v),checkbox('Скрывать «Хочу посмотреть»',!!draft.hide_watchlist,v=>draft.hide_watchlist=v),checkbox('Скрывать другие сохранённые',!!draft.hide_saved,v=>draft.hide_saved=v));
      if(draft.query)extra.append(checkbox('Искать без ограничений · только этот поиск',draft.relax_restrictions,v=>draft.relax_restrictions=v));form.append(extra);
      error=node('p','','cinema-form-error');error.setAttribute('role','alert');form.append(error);
      const footer=node('footer');submit=button('Применить',()=>{});submit.type='submit';submit.className='primary';footer.append(button('Сбросить фильтры',()=>{if(window.confirm('Сбросить выбранные фильтры к твоим настройкам по умолчанию?')){draft={...defaults(),query:initial.query};render();}}),submit);count();form.append(footer);
      form.onsubmit=e=>{e.preventDefault();if(!form.reportValidity())return;if(draft.year_min&&draft.year_max&&draft.year_min>draft.year_max){error.textContent='Начало диапазона должно быть раньше конца.';return;}dialog.close();apply(draft);};dialog.replaceChildren(header,form);
    };
    dialog.onclose=()=>{dialog.remove();if(this.dialog===dialog)this.dialog=null;};render();parent.append(dialog);dialog.showModal();
  }
}

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Window} from 'happy-dom';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';

function fixture() {
  const window=new Window({url:'http://127.0.0.1:8879'});
  window.document.body.innerHTML='<div id="cinema-root"></div>';
  const code=stripTypeScriptTypes(readFileSync(new URL('../src/cinema.ts',import.meta.url),'utf8'),{mode:'transform'})
    .replace(/^import .*;\n/gm,'').replace(/^export /gm,'')+'\nexports.api={CinemaView,playbackTime,parsePlaybackTime,viewingProgress};';
  const sandbox={exports:{} as Record<string,any>,document:window.document,window,URL:window.URL,icon:()=>'<svg></svg>'};
  const filters=stripTypeScriptTypes(readFileSync(new URL('../src/cinema_filters.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
  vm.runInNewContext(`{${filters}globalThis.CinemaFilterDrawer=CinemaFilterDrawer;globalThis.activeFilterCount=activeFilterCount;}`,sandbox);
  const catalogue=stripTypeScriptTypes(readFileSync(new URL('../src/cinema_catalogue.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
  vm.runInNewContext(`{${catalogue}globalThis.CinemaCatalogueView=CinemaCatalogueView;}`,sandbox);
  for(const [file,cls]of [['cinema_personal','CinemaPersonalView'],['cinema_player','CinemaEmbeddedPlayer']]){
    const source=stripTypeScriptTypes(readFileSync(new URL(`../src/${file}.ts`,import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
    vm.runInNewContext(`{${source}globalThis.${cls}=${cls};}`,sandbox);
  }
  vm.runInNewContext(code,sandbox);
  return {window,...sandbox.exports.api};
}

test('time and bounded progress',()=>{
  const f=fixture();try {
    assert.equal(f.playbackTime(3723.4),'1:02:03');assert.equal(f.parsePlaybackTime('1:02:03'),3723);
    assert.equal(f.parsePlaybackTime('28:14'),1694);assert.throws(()=>f.parsePlaybackTime('1:99'));
    assert.equal(f.viewingProgress(null),0);
  }finally{f.window.happyDOM.abort();}
});

test('real library data, source setup and offline controls',()=>{
  const f=fixture();try {
    const sent:object[]=[];const view=new f.CinemaView((v:object)=>sent.push(v),()=>{},()=>{},()=>{});
    view.connection(true);
    const event={type:'cinema',items:[],sources:[],history:[],preferences:{watched_threshold:.95,auto_next:false,startup:'continue',poll_seconds:5,devices:{}},devices:[],location:'general',durable:true,session:null,message:''};
    view.receive(event);assert.match(f.window.document.body.textContent??'',/Вечер начинается/);assert.doesNotMatch(f.window.document.body.textContent??'',/Во все тяжкие/);
    const sourceButton=[...f.window.document.querySelectorAll('button')].find(n=>n.textContent==='Настройки');sourceButton!.click();button(f,'Источники просмотра').click();
    assert.match(f.window.document.body.textContent??'',/Kinogo/);assert.match(f.window.document.body.textContent??'',/Filmix/);
    view.connection(false);assert.ok([...f.window.document.querySelectorAll('button')].every(n=>n.disabled));
    assert.equal((sent[0] as {op:string}).op,'refresh');
  }finally{f.window.happyDOM.abort();}
});

test('player DOM adapter handles same-origin HTML5 and refuses ambiguous players',()=>{
  const window=new Window({url:'https://example.org/episode'});window.document.body.innerHTML='<video></video>';
  const video=window.document.querySelector('video')!;
  for(const [name,value]of Object.entries({readyState:4,duration:100,currentTime:25,paused:true,ended:false}))Object.defineProperty(video,name,{value,configurable:true,writable:true});
  const source=readFileSync(new URL('../../backend/cinema/player.js',import.meta.url),'utf8');
  const run=(request:object)=>JSON.parse(window.eval(source+`(${JSON.stringify({url:'https://example.org/episode',...request})})`));
  assert.equal(run({action:'status'}).position,25);assert.equal(run({action:'seek',position:40}).position,40);
  assert.equal(run({action:'detach',session:'unused'}).detached,true);
  assert.equal(window.__chushpanCinema,undefined);
  assert.equal(run({url:'https://different.example',action:'status'}).error,'page_changed');
  const second=video.cloneNode(true);window.document.body.append(second);Object.defineProperty(second,'readyState',{value:4});Object.defineProperty(second,'duration',{value:100});
  assert.equal(run({action:'status'}).error,'multiple_players');window.happyDOM.abort();
});

function snapshot(items:any[]=[]) {
  return {type:'cinema',items,sources:[],history:[],preferences:{watched_threshold:.95,auto_next:false,startup:'continue',poll_seconds:5,devices:{}},devices:[],location:'general',durable:true,session:null,message:'',tmdb:{configured:true}};
}
function movie(id:string,status:string) {
  const ep={id:`ep-${id}`,title_id:id,season:0,episode:0,position:30,duration:100,watched:0,last_viewed:'2026-11-04T12:00:00Z',last_link_id:null,links:[]};
  return {id,title:id,kind:'movie',year:2026,poster_url:'',description:'Описание',status,favorite:0,episodes:[ep],current:ep,metadata:{genres:['триллер'],runtime:120,rating:8,seasons:[],episode_count:0}};
}
function button(f:any,text:string) {const b=[...f.window.document.querySelectorAll('button')].find((n:any)=>n.textContent===text) as HTMLButtonElement;assert.ok(b,`Missing button ${text}`);return b;}

test('seven tabs keep lists separate and watch requests source choices before opening',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    view.receive(snapshot([movie('Начато','watching'),movie('Отложено','planned'),movie('Завершено','watched')]));
    assert.equal(f.window.document.querySelectorAll('.cinema-tabs button').length,7);
    assert.match(f.window.document.querySelector('.cinema-content').textContent,/Начато/);
    assert.doesNotMatch(f.window.document.querySelector('.cinema-content').textContent,/Отложено|Завершено/);
    button(f,'▶ Продолжить').click();assert.equal(sent.at(-1).op,'options');
    button(f,'Хочу посмотреть').click(); // the tab precedes the card quick action
    assert.match(f.window.document.querySelector('.cinema-content').textContent,/Отложено/);
    button(f,'Смотреть').click();assert.deepEqual(JSON.parse(JSON.stringify(sent.at(-1))),{op:'options',title_id:'Отложено',episode_id:'ep-Отложено'});
    button(f,'Просмотрено').click();button(f,'Пересмотреть').click();assert.equal(sent.at(-1).op,'replay');
  }finally{f.window.happyDOM.abort();}
});

test('catalogue displays metadata and applies filters in app',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    const match={...movie('TMDB Film','planned'),catalogue_key:'tmdb:movie:1'};
    view.receive({...snapshot(),matches:[match]});button(f,'Каталог').click();
    assert.match(f.window.document.body.textContent??'',/TMDB Film|триллер|120 мин|8.0/);
    const input=f.window.document.querySelector('.cinema-search input') as HTMLInputElement;input.value='Триллер';input.dispatchEvent(new f.window.Event('input'));
    f.window.document.querySelector('.cinema-filter-trigger').click();const genre=f.window.document.querySelector('input[value=триллер]') as HTMLInputElement;genre.checked=true;genre.dispatchEvent(new f.window.Event('change'));
    const form=f.window.document.querySelector('.cinema-filter-drawer form')!;form.dispatchEvent(new f.window.Event('submit',{cancelable:true}));
    assert.equal(sent.at(-1).op,'catalogue_page');assert.equal(sent.at(-1).filters.query,'Триллер');assert.deepEqual(Array.from(sent.at(-1).filters.genres),['триллер']);
    view.receive({...snapshot(),catalogue_page:{request_id:sent.at(-1).request_id,page:1,items:[match],next_page:null,total_pages:1}});button(f,'Хочу посмотреть').click(); // tab; catalogue quick action is found explicitly below
    button(f,'Каталог').click();const quick=[...f.window.document.querySelectorAll('.cinema-card button')].find((n:any)=>n.textContent==='Хочу посмотреть') as HTMLButtonElement;quick.click();assert.equal(sent.at(-1).op,'catalogue_add');
  }finally{f.window.happyDOM.abort();}
});

test('source-less manual title can be saved and failed progress stays reviewable',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);view.receive(snapshot());
    button(f,'+ Добавить').click();const name=f.window.document.querySelector('dialog input[aria-label="Название"]') as HTMLInputElement;name.value='Новый фильм';
    f.window.document.querySelector('dialog form')!.dispatchEvent(new f.window.Event('submit',{cancelable:true}));assert.equal(sent.at(-1).op,'add');assert.equal(sent.at(-1).entry.source_id,'');
    view.receive({...snapshot([movie('Новый фильм','planned')]),saved:'Новый фильм'});button(f,'Хочу посмотреть').click();button(f,'Новый фильм').click();
    const manual=f.window.document.querySelector('dialog details form')!;manual.dispatchEvent(new f.window.Event('submit',{cancelable:true}));assert.equal(sent.at(-1).op,'progress');assert.ok(f.window.document.querySelector('dialog'));
    view.receive({...snapshot([movie('Новый фильм','planned')]),error:'SQLite недоступна'});assert.match(f.window.document.querySelector('dialog')!.textContent,/SQLite недоступна/);
  }finally{f.window.happyDOM.abort();}
});

test('unavailable sources explain limits and do not issue open requests',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    view.receive({...snapshot([movie('Film','planned')]),source_options:{title_id:'Film',episode_id:'ep-Film',options:[{source:{id:'source',name:'Offline',enabled:false,adapter:'manual'},link:null,available:false,reason:'Источник отключён.'}]}});
    assert.match(f.window.document.querySelector('dialog')!.textContent,/Источник отключён/);
    assert.equal([...f.window.document.querySelectorAll('dialog button')].filter((b:any)=>b.textContent==='Открыть вручную').length,0);
    assert.ok(!sent.some(v=>v.op==='open'));
  }finally{f.window.happyDOM.abort();}
});

test('existing movie binds a local file with valid hidden episode fields',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    const source={id:'local',name:'Мои файлы',base_url:'',search_url:'',adapter:'local',category:'local',enabled:true};
    view.receive({...snapshot([movie('Film','watching')]),sources:[source],source_options:{title_id:'Film',episode_id:'ep-Film',options:[{source,link:null,available:false,reason:'Привяжи файл'}]}});
    button(f,'Привязать источник').click();const input=f.window.document.querySelector('dialog input[aria-label="Точная страница фильма или серии"]') as HTMLInputElement;
    assert.equal(input.type,'text');input.value='/tmp/movie.mp4';
    const form=f.window.document.querySelector('dialog form')!;assert.ok((form as HTMLFormElement).reportValidity());form.dispatchEvent(new f.window.Event('submit',{cancelable:true}));
    assert.equal(sent.at(-1).op,'add');assert.equal(sent.at(-1).entry.title_id,'Film');assert.equal(sent.at(-1).entry.url,'/tmp/movie.mp4');
  }finally{f.window.happyDOM.abort();}
});

test('favorites is independent of status, filters types and sorts by personal rating',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    const a={...movie('Favorite movie','planned'),favorite:1,user_rating:7.5};
    const b={...movie('Favorite series','watched'),kind:'series',favorite:1,user_rating:9.5};
    view.receive(snapshot([a,b,movie('Other','watched')]));button(f,'Избранное').click();
    assert.equal(f.window.document.querySelectorAll('.cinema-content .cinema-card').length,2);
    const sort=f.window.document.querySelector('[aria-label="Сортировка медиатеки"]');sort.value='rating';sort.dispatchEvent(new f.window.Event('change'));
    assert.equal(f.window.document.querySelector('.cinema-content .cinema-card').dataset.titleId,b.id);
    button(f,'Фильмы').click();assert.equal(f.window.document.querySelectorAll('.cinema-content .cinema-card').length,1);
    const heart=f.window.document.querySelector('.cinema-content .cinema-favorite');heart.click();assert.equal(sent.at(-1).values.favorite,false);assert.equal(sent.at(-1).values.status,undefined);
    view.receive(snapshot([{...a,favorite:0},b]));assert.equal(f.window.document.querySelectorAll('.cinema-content .cinema-card').length,0);
  }finally{f.window.happyDOM.abort();}
});

test('half rating can be edited, deleted and synchronizes open detail without losing manual input',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item={...movie('Rated','watched'),user_rating:9};view.receive(snapshot([item]));button(f,'Просмотрено').click();button(f,'Rated').click();
    const rating=f.window.document.querySelector('[aria-label="Моя оценка"]');assert.ok([...rating.options].some((v:any)=>v.value==='9.5'));rating.value='9.5';rating.dispatchEvent(new f.window.Event('change'));assert.equal(sent.at(-1).values.user_rating,9.5);
    const position=f.window.document.querySelector('[aria-label="Позиция · мин:сек"]');position.value='12:34';
    view.receive(snapshot([{...item,user_rating:9.5,favorite:1}]));assert.equal(rating.value,'9.5');assert.equal(position.value,'12:34');assert.match(f.window.document.querySelector('dialog .cinema-ratings').textContent,/TMDB: 8.0\/10.*Моя оценка: 9.5\/10/);
    rating.value='';rating.dispatchEvent(new f.window.Event('change'));assert.equal(sent.at(-1).values.user_rating,null);
    f.window.document.querySelector('[aria-label="Закрыть"]').click();
    const min=f.window.document.querySelector('[aria-label="Моя оценка от"]');min.value='9.5';min.dispatchEvent(new f.window.Event('change'));assert.equal(f.window.document.querySelectorAll('.cinema-content .cinema-card').length,1);
    view.receive(snapshot([{...item,user_rating:7.5}]));assert.equal(f.window.document.querySelectorAll('.cinema-content .cinema-card').length,0);
  }finally{f.window.happyDOM.abort();}
});

test('profile loads real analytics lazily and keeps its charts mounted across progress snapshots',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const initial=snapshot([movie('Любимое','watched')]);view.receive(initial);
    assert.ok(!sent.some(v=>v.op==='profile'));button(f,'Мой кинопрофиль').click();const request=sent.at(-1);assert.equal(request.op,'profile');
    const profile={watched_movies:1,watched_series:0,favorites:1,rated_count:1,average:9.5,distribution:Array.from({length:10},(_,i)=>({rating:i+1,count:i===8?1:0})),genres:[{name:'триллер',count:1}],directors:[],cast:[],top:[],least:[],insights:['Пока одна оценка — выводы предварительные.'],trend:[],metadata_coverage:0,positive_count:1};
    view.receive({...initial,personal_result:{request_id:request.request_id,profile}});
    assert.match(f.window.document.querySelector('.cinema-personal')!.textContent,/9.5|одна оценка|триллер/);
    const chart=f.window.document.querySelector('.cinema-chart-bar');view.receive(initial);assert.equal(f.window.document.querySelector('.cinema-chart-bar'),chart);
    button(f,'Обновить метаданные').click();assert.equal(sent.at(-1).op,'personal_enrich');
  }finally{f.window.happyDOM.abort();}
});

test('personal shelves explain recommendations and persist dismiss, watched and planned actions',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const initial=snapshot();view.receive(initial);button(f,'Для тебя').click();const request=sent.at(-1);
    const item={...movie('Рекомендация','planned'),catalogue_key:'tmdb:movie:777',reason:'Тебе нравится жанр триллер.'};
    view.receive({...initial,personal_result:{request_id:request.request_id,recommendations:{samples:1,mood:'any',message:'Мало оценок',errors:[],shelves:[{id:'liked',name:'Тебе понравится',items:[item]}]}}});
    assert.match(f.window.document.querySelector('.cinema-personal')!.textContent,/Тебе нравится жанр триллер/);
    const planned=[...f.window.document.querySelectorAll('.cinema-personal button')].find((b:any)=>b.textContent==='Хочу посмотреть');planned.click();assert.equal(sent.at(-1).op,'catalogue_add');assert.equal(sent.at(-1).values.status,'planned');
    button(f,'Уже смотрела').click();assert.equal(sent.at(-1).values.status,'watched');
    button(f,'Не интересно').click();assert.equal(sent.at(-1).op,'recommendation_dismiss');view.receive({...initial,recommendation_dismissed:item.catalogue_key});assert.equal(f.window.document.querySelectorAll('.cinema-personal-card').length,0);
  }finally{f.window.happyDOM.abort();}
});

test('source URL alone auto-detects official API and settings expose playback and personalization choices',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);view.receive(snapshot());button(f,'Настройки').click();
    assert.match(f.window.document.querySelector('.cinema-content')!.textContent,/Настройки медиатеки|Скрывать аниме/);
    button(f,'Источники просмотра').click();button(f,'+ Добавить источник').click();const address=f.window.document.querySelector('dialog [aria-label="Адрес сайта"]');address.value='https://archive.org';
    f.window.document.querySelector('dialog form')!.dispatchEvent(new f.window.Event('submit',{cancelable:true}));const source=sent.at(-1).source;assert.equal(source.name,'archive.org');assert.equal(source.search_mode,'archive');assert.equal(source.adapter,'manual');
  }finally{f.window.happyDOM.abort();}
});

test('source search discards stale results and distinguishes manual pages from verified matching content',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item=movie('Film','planned');const initial=snapshot([item]);view.receive(initial);button(f,'Хочу посмотреть').click();button(f,'Film').click();button(f,'Найти и смотреть').click();const request=sent.at(-1);assert.equal(request.op,'find_sources');
    const result={id:'result',source_id:'source',source_name:'Ручной сайт',kind:'search',url:'https://site.example',availability:'unknown',reason:'Выбери результат на сайте',embedded:false,language:'',access_type:'unknown',allowed:true,match:0,capabilities:{mode:'Ручной выбор'}};
    view.receive({...initial,source_results:{request_id:'stale',title_id:item.id,episode_id:item.current.id,results:[result],selected_id:null,message:'Старая выдача'}});assert.doesNotMatch(f.window.document.querySelector('dialog')!.textContent,/Старая выдача/);
    view.receive({...initial,source_results:{request_id:request.request_id,title_id:item.id,episode_id:item.current.id,results:[result],selected_id:null,message:'Ручной выбор'}});
    assert.match(f.window.document.querySelector('dialog')!.textContent,/Язык неизвестен|Условия доступа неизвестны/);button(f,'Открыть поиск ↗').click();assert.equal(sent.at(-1).op,'source_result_open');assert.ok(!sent.some(v=>v.op==='open'));
  }finally{f.window.happyDOM.abort();}
});

test('embedded video persists across snapshots and confirms only advancing playback',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item=movie('Film','watching');
    const initial={...snapshot([item]),session:{title_id:item.id,episode_id:item.current.id,mode:'embedded'}};
    view.receive({...initial,embedded_player:{token:'random',url:'https://site.example/film.mp4',title:'Film',position:30,title_id:item.id,episode_id:item.current.id}});
    const video=f.window.document.querySelector('video');assert.ok(video);for(const [key,value]of Object.entries({duration:100,currentTime:30,paused:false,seeking:false}))Object.defineProperty(video,key,{value,writable:true,configurable:true});
    video.dispatchEvent(new f.window.Event('timeupdate'));assert.ok(!sent.some(v=>v.event==='playing'));
    video.currentTime=30.3;video.dispatchEvent(new f.window.Event('timeupdate'));assert.equal(sent.at(-1).event,'playing');assert.equal(sent.at(-1).token,'random');
    view.receive(initial);assert.equal(f.window.document.querySelector('video'),video);
    button(f,'Сохранить и закрыть плеер').click();assert.equal(sent.at(-1).event,'close');assert.equal(f.window.document.querySelector('video'),null);
  }finally{f.window.happyDOM.abort();}
});

test('media settings separate sites from technical TMDB credentials and expose persistent catalogue policy',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);view.receive(snapshot());button(f,'Настройки').click();
    const russian=f.window.document.querySelector('[aria-label="Только названия на русском"]');assert.equal(russian.checked,true);
    russian.checked=false;const sort=f.window.document.querySelector('[aria-label="Сортировка по умолчанию"]');sort.value='title';
    f.window.document.querySelector('.cinema-preference-card form').dispatchEvent(new f.window.Event('submit',{cancelable:true}));assert.equal(sent.at(-1).values.russian_titles,false);assert.equal(sent.at(-1).values.default_sort,'title');
    button(f,'Источники просмотра').click();assert.equal(f.window.document.querySelector('input[type=password]'),null);assert.doesNotMatch(f.window.document.querySelector('.cinema-settings-pane').textContent,/Интеграция TMDB|API-ключ/);
    f.window.document.dispatchEvent(new f.window.Event('cinema:integration'));assert.equal(f.window.document.querySelector('input[type=password]').value,'');assert.match(f.window.document.querySelector('.cinema-content').textContent,/Интеграция TMDB/);
  }finally{f.window.happyDOM.abort();}
});

test('rating modal saves half stars and a note without changing favorite or watch status',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item={...movie('Фильм','watched'),favorite:1,user_rating:8,user_note:'Прежняя заметка'};view.receive(snapshot([item]));button(f,'Просмотрено').click();button(f,'Фильм').click();
    f.window.document.querySelector('[aria-label="Оценить фильм"]').click();const note=f.window.document.querySelector('dialog textarea');assert.equal(note.value,'Прежняя заметка');note.value='Сильный финал';
    const radio=f.window.document.querySelector('[aria-label="9.5 из 10"]');radio.checked=true;radio.dispatchEvent(new f.window.Event('change'));assert.equal(f.window.document.querySelector('dialog output').textContent,'9.5');
    f.window.document.querySelector('dialog form').dispatchEvent(new f.window.Event('submit',{cancelable:true}));assert.equal(sent.at(-1).values.user_rating,9.5);assert.equal(sent.at(-1).values.user_note,'Сильный финал');assert.equal(sent.at(-1).values.favorite,undefined);assert.equal(sent.at(-1).values.status,undefined);
    view.receive({...snapshot([{...item,user_rating:9.5,user_note:'Сильный финал'}]),saved:item.id});assert.ok(f.window.document.querySelector('.cinema-rich-dialog'));assert.match(f.window.document.querySelector('dialog .cinema-ratings').textContent,/TMDB: 8.0.*Моя оценка: 9.5/);
    button(f,'О фильме').click();assert.match(f.window.document.querySelector('.cinema-rich-info').textContent,/Сильный финал/);
  }finally{f.window.happyDOM.abort();}
});

test('outdated title details cannot override a newer choice or reopen a closed dialog',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);view.receive(snapshot());button(f,'Каталог').click();const request=sent.filter(v=>v.op==='catalogue_page').at(-1);
    const a={...movie('Первый','planned'),catalogue_key:'tmdb:movie:1'};const b={...movie('Второй','planned'),catalogue_key:'tmdb:movie:2'};
    view.receive({...snapshot(),catalogue_page:{request_id:request.request_id,page:1,items:[a,b],next_page:null,total_pages:1}});button(f,'Первый').click();button(f,'Второй').click();
    view.receive({...snapshot(),catalogue_detail:a});assert.equal(f.window.document.querySelector('dialog'),null);view.receive({...snapshot(),catalogue_detail:b});assert.match(f.window.document.querySelector('dialog').textContent,/Второй/);
    f.window.document.querySelector('[aria-label="Закрыть"]').click();view.receive({...snapshot(),catalogue_detail:b});assert.equal(f.window.document.querySelector('dialog'),null);
  }finally{f.window.happyDOM.abort();}
});

test('import is reviewed before confirmation and native export acknowledges actual save',async()=>{
  const f=fixture();try{
    const sent:any[]=[];let exported:any=null;Object.defineProperty(f.window,'pywebview',{value:{api:{save_cinema_library:async(document:any)=>{exported=document;return {ok:true};}}}});
    const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);view.receive(snapshot());view.receive({...snapshot(),library_import_preview:{token:'proposal',titles:10,sources:2,events:30,existing:1}});
    assert.ok(!sent.some(v=>v.op==='library_import'));assert.match(f.window.document.querySelector('dialog').textContent,/10 названий|Существующие оценки/);button(f,'Импортировать').click();assert.equal(sent.at(-1).token,'proposal');
    view.receive({...snapshot(),library_imported:{added:9,preserved:1}});assert.equal(f.window.document.querySelector('dialog'),null);
    const document={schema:'chushpan.cinema'};view.receive({...snapshot(),library_export:document});await new Promise(resolve=>setTimeout(resolve,0));assert.equal(exported,document);assert.match(f.window.document.querySelector('.cinema-feedback').textContent,/сохранена/);
  }finally{f.window.happyDOM.abort();}
});

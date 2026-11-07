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
  const actions=stripTypeScriptTypes(readFileSync(new URL('../src/cinema_actions.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
  vm.runInNewContext(`{${actions}globalThis.posterActions=posterActions;globalThis.syncPosterActions=syncPosterActions;}`,sandbox);
  const filters=stripTypeScriptTypes(readFileSync(new URL('../src/cinema_filters.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
  vm.runInNewContext(`{${filters}globalThis.CinemaFilterDrawer=CinemaFilterDrawer;globalThis.activeFilterCount=activeFilterCount;}`,sandbox);
  const catalogue=stripTypeScriptTypes(readFileSync(new URL('../src/cinema_catalogue.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
  vm.runInNewContext(`{${catalogue}globalThis.CinemaCatalogueView=CinemaCatalogueView;}`,sandbox);
  for(const [file,cls]of [['cinema_personal','CinemaPersonalView'],['cinema_player','CinemaEmbeddedPlayer'],['cinema_viewer','CinemaViewer'],['cinema_source_diagnostics','CinemaSourceDiagnostics']]){
    const source=stripTypeScriptTypes(readFileSync(new URL(`../src/${file}.ts`,import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
    vm.runInNewContext(`{${source}globalThis.${cls}=${cls};}`,sandbox);
  }
  const browser=stripTypeScriptTypes(readFileSync(new URL('../src/cinema_browser_diagnostics.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^export /gm,'');
  vm.runInNewContext(`{${browser}globalThis.browserDiagnosticsPanel=browserDiagnosticsPanel;globalThis.copyDiagnostic=copyDiagnostic;}`,sandbox);
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
  Object.defineProperty(video,'getBoundingClientRect',{value:()=>({width:640,height:360})});
  for(const [name,value]of Object.entries({readyState:4,duration:100,currentTime:25,paused:true,ended:false}))Object.defineProperty(video,name,{value,configurable:true,writable:true});
  const source=readFileSync(new URL('../../backend/cinema/player.js',import.meta.url),'utf8');
  const run=(request:object)=>JSON.parse(window.eval(source+`(${JSON.stringify({url:'https://example.org/episode',...request})})`));
  assert.equal(run({action:'status'}).position,25);assert.equal(run({action:'seek',position:40}).position,40);
  assert.equal(run({action:'detach',session:'unused'}).detached,true);
  assert.equal(window.__chushpanCinema,undefined);
  assert.equal(run({url:'https://different.example',action:'status'}).error,'page_changed');
  const second=video.cloneNode(true);window.document.body.append(second);Object.defineProperty(second,'readyState',{value:4});Object.defineProperty(second,'duration',{value:100});
  Object.defineProperty(second,'getBoundingClientRect',{value:()=>({width:640,height:360})});
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

test('seven tabs keep lists separate and watch starts automatic discovery',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    view.receive(snapshot([movie('Начато','watching'),movie('Отложено','planned'),movie('Завершено','watched')]));
    assert.equal(f.window.document.querySelectorAll('.cinema-tabs button').length,7);
    assert.match(f.window.document.querySelector('.cinema-content').textContent,/Начато/);
    assert.doesNotMatch(f.window.document.querySelector('.cinema-content').textContent,/Отложено|Завершено/);
    f.window.document.querySelector('.cinema-quick-play').click();assert.equal(sent.at(-1).op,'watch');f.window.document.querySelector('dialog button[aria-label=Закрыть]').click();
    button(f,'Хочу посмотреть').click(); // the tab precedes the card quick action
    assert.match(f.window.document.querySelector('.cinema-content').textContent,/Отложено/);
    f.window.document.querySelector('.cinema-quick-play').click();assert.equal(sent.at(-1).op,'watch');assert.equal(sent.at(-1).title_id,'Отложено');assert.equal(sent.at(-1).episode_id,'ep-Отложено');f.window.document.querySelector('dialog button[aria-label=Закрыть]').click();
    button(f,'Просмотрено').click();f.window.document.querySelector('.cinema-quick-play').click();assert.equal(sent.at(-1).op,'watch');assert.equal(sent.at(-1).title_id,'Завершено');
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
    button(f,'Каталог').click();const quick=f.window.document.querySelector('.cinema-quick-bookmark') as HTMLButtonElement;quick.click();assert.equal(sent.at(-1).op,'catalogue_add');
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
    const planned=f.window.document.querySelector('.cinema-personal .cinema-quick-bookmark');planned.click();assert.equal(sent.at(-1).op,'catalogue_add');assert.equal(sent.at(-1).values.watchlist,true);
    f.window.document.querySelector('.cinema-personal .cinema-quick-watched').click();assert.equal(sent.at(-1).values.watched,true);
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
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item=movie('Film','planned');const initial=snapshot([item]);view.receive(initial);button(f,'Хочу посмотреть').click();button(f,'Film').click();button(f,'Найти и смотреть').click();const request=sent.at(-1);assert.equal(request.op,'watch');
    const result={id:'result',source_id:'source',source_name:'Ручной сайт',kind:'search',url:'https://site.example',availability:'unknown',reason:'Выбери результат на сайте',embedded:false,language:'',access_type:'unknown',allowed:true,match:0,capabilities:{mode:'Ручной выбор'}};
    view.receive({...initial,source_results:{request_id:'stale',title_id:item.id,episode_id:item.current.id,results:[result],selected_id:null,message:'Старая выдача'}});assert.doesNotMatch(f.window.document.querySelector('dialog')!.textContent,/Старая выдача/);
    view.receive({...initial,source_results:{request_id:request.request_id,title_id:item.id,episode_id:item.current.id,results:[result],selected_id:null,message:'Ручной выбор'}});
    assert.match(f.window.document.querySelector('dialog')!.textContent,/Выбери результат на сайте/);assert.ok(![...f.window.document.querySelectorAll('dialog button')].some((b:any)=>/Открыть сайт|Открыть поиск/.test(b.textContent)));assert.ok(!sent.some(v=>v.op==='open'||v.op==='source_result_open'));
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

test('hero watch uses the episode selected in the detail card',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    const item=movie('Series','watching');item.kind='series';item.episodes[0].season=1;item.episodes[0].episode=1;
    const second={...item.episodes[0],id:'second',season:2,episode:3};item.episodes.push(second);view.receive(snapshot([item]));button(f,'Series').click();
    const select=f.window.document.querySelector('select[aria-label=Сезон]');select.value='2';select.dispatchEvent(new f.window.Event('change'));
    f.window.document.querySelector('.cinema-detail-main-actions button').click();
    assert.equal(sent.at(-1).op,'watch');assert.equal(sent.at(-1).episode_id,'second');
    assert.ok(f.window.document.querySelector('dialog.cinema-compact-search'));assert.match(f.window.document.querySelector('dialog').textContent,/Сезон 2 · Серия 3/);
  }finally{f.window.happyDOM.abort();}
});

test('unknown cost is a visible manual choice under free-only, not paid or automatic',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item=movie('Film','planned');
    view.receive({...snapshot([item]),source_results:{title_id:item.id,episode_id:item.current.id,selected_id:null,message:'Только подтверждённые бесплатные',results:[{id:'unknown',content_verified:true,video_type:'full_movie',source_name:'Unknown',source_id:'source',kind:'page',url:'https://site.example/film',availability:'available',reason:'Совпадение проверено',access_status:'UNKNOWN',access_type:'unknown',match:1,allowed:false,manual_allowed:true,embedded:false,capabilities:{mode:'HTML5'}}]}});
    assert.match(f.window.document.querySelector('dialog').textContent,/Стоимость просмотра не проверена/);const choose=button(f,'Смотреть');assert.equal(choose.disabled,false);assert.ok(!sent.some(v=>v.op==='source_result_open'));choose.click();assert.equal(sent.at(-1).op,'source_result_open');assert.equal(sent.at(-1).result_id,'unknown');
  }finally{f.window.happyDOM.abort();}
});

test('paid navigation requires an explicit confirmation with the server token',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    view.receive({...snapshot(),watch_confirmation:{token:'bound-token',source_name:'KION',url:'https://kion.ru/serial/example',message:'Сервис содержит платный контент.'}});
    assert.ok(!sent.some(v=>v.op==='watch_confirm'));button(f,'Открыть страницу сервиса').click();assert.equal(sent.at(-1).op,'watch_confirm');assert.equal(sent.at(-1).token,'bound-token');
  }finally{f.window.happyDOM.abort();}
});

test('Brave search setup masks and clears the key and keeps Safari permissions explicit',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);view.receive(snapshot());button(f,'Настройки').click();button(f,'Поиск и Safari').click();
    const input=f.window.document.querySelector('input[aria-label="Ключ Brave Search API"]');assert.equal(input.type,'password');input.value='BSA_fixture_search_key';input.closest('form').dispatchEvent(new f.window.Event('submit',{cancelable:true}));assert.equal(sent.at(-1).op,'search_credentials');assert.equal(input.value,'');
    assert.equal(f.window.document.querySelector('input[data-preference=open_in_safari]').checked,false);
    button(f,'Проверить интеграцию с Safari').click();assert.equal(sent.at(-1).op,'safari_check');assert.ok(!sent.some(v=>v.op==='safari_access'));
    view.receive({...snapshot(),safari_check:{method:'Apple Events',extension:'not_installed',automation:'granted',javascript:'not_granted'}});assert.match(f.window.document.querySelector('[data-safari-status]').textContent,/JavaScript — not_granted/);
  }finally{f.window.happyDOM.abort();}
});

test('next episode offer opens only the offered episode and diagnostics remain readable',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item=movie('Series','watching');item.kind='series';item.current.season=1;item.current.episode=1;const next={...item.current,id:'next',episode:2};item.episodes.push(next);
    view.receive({...snapshot([item]),next_episode:{title_id:item.id,episode_id:next.id,season:1,episode:2,title:item.title}});assert.ok(!sent.some(v=>v.op==='watch'));button(f,'Продолжить следующую').click();assert.equal(sent.at(-1).op,'watch');assert.equal(sent.at(-1).episode_id,'next');
    f.window.document.querySelector('dialog[open] button[aria-label=Закрыть]').click();button(f,'Настройки').click();button(f,'Поиск и Safari').click();button(f,'Показать последние этапы').click();assert.equal(sent.at(-1).op,'cinema_diagnostics');
    view.receive({...snapshot([item]),cinema_diagnostics:[{id:1,stage:'RESTORE_POSITION',status:'limited',message:'Safari не подтвердил восстановление',created_at:'2026-11-04T21:00:00Z'}]});assert.match(f.window.document.querySelector('[data-cinema-diagnostics]').textContent,/RESTORE_POSITION.*limited.*не подтвердил/);
  }finally{f.window.happyDOM.abort();}
});

test('remembered Safari playback remains manually resumable when anonymous checks are restricted',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item=movie('Private film','watching');
    view.receive({...snapshot([item]),source_results:{title_id:item.id,episode_id:item.current.id,selected_id:null,message:'Анонимная проверка ограничена',results:[{id:'last',source_name:'Last source',source_id:'source',kind:'page',scope:'movie',url:'https://site.example/film',availability:'restricted',reason:'Ранее воспроизводился в Safari',access_status:'UNKNOWN',access_type:'unknown',match:1,allowed:true,manual_allowed:true,previously_played:true,embedded:false,capabilities:{mode:'HTML5'}}]}});
    const resume=button(f,'Проверить плеер в Safari');assert.equal(resume.disabled,false);resume.click();assert.equal(sent.at(-1).op,'source_result_open');assert.equal(sent.at(-1).result_id,'last');
  }finally{f.window.happyDOM.abort();}
});

test('Tavily is the default search provider and its key is cleared before submission',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);view.receive(snapshot());button(f,'Настройки').click();button(f,'Поиск и Safari').click();
    const provider=f.window.document.querySelector('select[aria-label="Провайдер интернет-поиска"]');assert.equal(provider.value,'tavily');
    const key=f.window.document.querySelector('input[aria-label="Ключ Tavily API"]');assert.equal(key.type,'password');assert.equal(key.autocomplete,'new-password');key.value='tvly-fixture-ui-key';key.closest('form').dispatchEvent(new f.window.Event('submit',{cancelable:true}));
    assert.equal(sent.at(-1).op,'tavily_credentials');assert.equal(sent.at(-1).credential,'tvly-fixture-ui-key');assert.equal(key.value,'');
    assert.match(f.window.document.querySelector('.cinema-settings-pane').textContent,/macOS Keychain/);
    button(f,'Проверить подключение').click();assert.equal(sent.at(-1).op,'tavily_check');assert.equal(sent.at(-1).credential,undefined);
    view.receive({...snapshot(),search_connection:{provider:'tavily',ok:false,code:'credits_exhausted',message:'Лимит кредитов Tavily исчерпан.'}});
    assert.match(f.window.document.querySelector('[data-tavily-connection]').textContent,/Лимит кредитов/);assert.equal(f.window.document.querySelector('[data-tavily-connection]').dataset.error,'true');
    provider.value='brave';provider.closest('form').dispatchEvent(new f.window.Event('submit',{cancelable:true}));assert.equal(sent.at(-1).values.search_provider,'brave');
    button(f,'Удалить ключ Tavily').click();assert.equal(sent.at(-1).op,'tavily_credentials');assert.equal(sent.at(-1).credential,'');
  }finally{f.window.happyDOM.abort();}
});

test('connection success shows only usage and keeps the existing Brave setup',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    const initial={...snapshot(),internet_search:{provider:'tavily',configured:true,providers:{tavily:{configured:true,storage:'keychain',state:'saved'},brave:{configured:true,storage:'private_file'}}}};view.receive(initial);button(f,'Настройки').click();button(f,'Поиск и Safari').click();
    assert.match(f.window.document.querySelector('.cinema-settings-pane').textContent,/Ключ Tavily|Ключ сохранён в macOS Keychain/);assert.match(f.window.document.querySelector('.cinema-settings-pane').textContent,/Ключ Brave сохранён/);
    view.receive({...initial,search_connection:{provider:'tavily',ok:true,code:'connected',message:'Tavily подключён.',usage:4,limit:100}});
    assert.match(f.window.document.querySelector('[data-tavily-connection]').textContent,/4 \/ 100/);assert.equal(f.window.document.querySelector('input[aria-label="Ключ Tavily API"]').value,'');
  }finally{f.window.happyDOM.abort();}
});

test('four poster icons toggle durable fields without detail clicks and refresh watched and profile',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    const item={...movie('Моя история','planned'),watchlist:1,favorite:1,user_rating:9.5};view.receive(snapshot([item]));button(f,'Хочу посмотреть').click();
    const panel=f.window.document.querySelector('.cinema-poster-actions');assert.equal(panel.querySelectorAll('button').length,4);
    assert.deepEqual([...panel.querySelectorAll('button')].map((b:any)=>b.dataset.posterAction),['bookmark','play','watched','favorite']);
    assert.ok([...panel.querySelectorAll('button')].every((b:any)=>b.title&&b.getAttribute('aria-label')));
    let bubbled=0;panel.parentElement.addEventListener('click',()=>bubbled++);
    for(const [key,field,value]of [['bookmark','watchlist',false],['favorite','favorite',false],['watched','watched',true]]){
      panel.querySelector(`[data-poster-action=${key}]`).click();assert.equal(sent.at(-1).op,'update');assert.equal(sent.at(-1).values[field],value);assert.equal(f.window.document.querySelector('dialog'),null);
    }
    assert.equal(bubbled,0);assert.equal(f.window.document.querySelector('.cinema-context'),null);
    view.receive(snapshot([{...item,status:'watched',watchlist:0}]));assert.equal(f.window.document.querySelectorAll('.cinema-library .cinema-card').length,0);
    button(f,'Просмотрено').click();const active=f.window.document.querySelector('.cinema-quick-watched');assert.equal(active.getAttribute('aria-pressed'),'true');active.click();assert.equal(sent.at(-1).values.watched,false);assert.equal(f.window.document.querySelector('dialog'),null);
    button(f,'Мой кинопрофиль').click();const first=sent.at(-1);assert.equal(first.op,'profile');
    const profile={watched_movies:1,watched_series:0,favorites:1,rated_count:1,average:9.5,distribution:[],genres:[],directors:[],cast:[],top:[],least:[],insights:[],trend:[],metadata_coverage:0,positive_count:1};
    view.receive({...snapshot([{...item,status:'watched',watchlist:0}]),personal_result:{request_id:first.request_id,profile}});
    assert.equal(f.window.document.querySelector('.cinema-profile-stats strong').textContent,'1');
    view.receive(snapshot([{...item,status:'watching',watchlist:0}]));const refresh=sent.at(-1);assert.equal(refresh.op,'profile');assert.notEqual(refresh.request_id,first.request_id);
    view.receive({...snapshot([{...item,status:'watching',watchlist:0}]),personal_result:{request_id:refresh.request_id,profile:{...profile,watched_movies:0}}});
    assert.equal(f.window.document.querySelector('.cinema-profile-stats strong').textContent,'0');
    button(f,'Смотрю').click();f.window.document.querySelector('.cinema-quick-play').click();assert.equal(sent.at(-1).op,'watch');assert.ok(!sent.some(v=>v.op==='replay'));
  }finally{f.window.happyDOM.abort();}
});

test('profile discards counts computed before a newer manual watched toggle',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    const item=movie('История','watched');view.receive(snapshot([item]));button(f,'Мой кинопрофиль').click();const before=sent.at(-1);
    const changed=snapshot([{...item,status:'watching'}]);view.receive(changed);
    const profile={watched_movies:1,watched_series:0,favorites:0,rated_count:0,average:null,distribution:[],genres:[],directors:[],cast:[],top:[],least:[],insights:[],trend:[],metadata_coverage:0,positive_count:0};
    view.receive({...changed,personal_result:{request_id:before.request_id,profile}});
    const fresh=sent.at(-1);assert.equal(fresh.op,'profile');assert.notEqual(fresh.request_id,before.request_id);assert.equal(f.window.document.querySelector('.cinema-profile-stats'),null);
    view.receive({...changed,personal_result:{request_id:fresh.request_id,profile:{...profile,watched_movies:0}}});assert.equal(f.window.document.querySelector('.cinema-profile-stats strong').textContent,'0');
  }finally{f.window.happyDOM.abort();}
});

test('AI search stays out of the catalogue flow until opened and voice remains independent',()=>{
  const f=fixture();try{
    const messages:string[]=[];let voice=0;
    const view=new f.CinemaView(()=>{},(text:string)=>messages.push(text),()=>{},()=>voice++);
    view.connection(true);view.receive(snapshot());
    const panel=f.window.document.querySelector('.cinema-ai-panel');
    assert.equal(panel.hidden,true);assert.equal(f.window.document.querySelector('#cinema-root > .cinema-composer'),null);
    button(f,'✧ AI-поиск').click();assert.equal(panel.hidden,false);
    const input=panel.querySelector('input');input.value='Найди детектив на вечер';
    panel.dispatchEvent(new f.window.Event('submit',{bubbles:true,cancelable:true}));
    assert.deepEqual(messages,['Найди детектив на вечер']);assert.equal(panel.hidden,true);
    button(f,'✧ AI-поиск').click();panel.dispatchEvent(new f.window.Event('submit',{bubbles:true,cancelable:true}));assert.equal(panel.hidden,false);
    f.window.document.dispatchEvent(new f.window.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));assert.equal(panel.hidden,true);
    f.window.document.querySelector('[aria-label="Голосовая команда"]').click();assert.equal(voice,1);
    assert.ok(f.window.document.querySelector('[aria-label="Фильтры каталога"]'));
  }finally{f.window.happyDOM.abort();}
});

test('player reports gesture requirements and only offers an explicit user button',async()=>{
  const window=new Window({url:'https://example.org/episode'});try{
    window.document.body.innerHTML='<video></video>';const video=window.document.querySelector('video')!;
    for(const [name,value]of Object.entries({readyState:4,duration:100,currentTime:25,paused:true,ended:false}))Object.defineProperty(video,name,{value,configurable:true,writable:true});
    Object.defineProperty(video,'getBoundingClientRect',{value:()=>({width:640,height:360})});
    let calls=0;Object.defineProperty(video,'play',{value:()=>{calls++;return Promise.reject(new window.DOMException('Needs user gesture','NotAllowedError'));}});
    Object.defineProperty(video,'requestFullscreen',{value:()=>Promise.reject(new window.DOMException('Needs user gesture','NotAllowedError'))});
    const source=readFileSync(new URL('../../backend/cinema/player.js',import.meta.url),'utf8');
    const run=(action:string)=>JSON.parse(window.eval(source+`(${JSON.stringify({url:'https://example.org/episode',action,session:'test'})})`));
    run('play');await new Promise(resolve=>setTimeout(resolve,0));assert.equal(run('status').play_error,'gesture_required');
    run('fullscreen');await new Promise(resolve=>setTimeout(resolve,0));assert.equal(run('status').fullscreen_error,'gesture_required');
    run('offer_gesture');assert.equal(calls,1);const button=window.document.querySelector<HTMLButtonElement>('[data-chushpan-cinema="play-fullscreen"]')!;assert.ok(button);
    button.click();await new Promise(resolve=>setTimeout(resolve,0));assert.equal(calls,2);
    run('detach');assert.equal(window.document.querySelector('[data-chushpan-cinema]'),null);
  }finally{await window.happyDOM.abort();}
});

test('HTML5 voice restoration requires an exact exposed audio track',async()=>{
  const window=new Window({url:'https://example.org/episode'});try{
    window.document.body.innerHTML='<video></video>';const video=window.document.querySelector('video')!;
    const tracks=[{label:'Оригинал',language:'en',enabled:true},{label:'Русская',language:'ru',enabled:false}];
    for(const [name,value]of Object.entries({readyState:4,duration:100,currentTime:25,paused:true,audioTracks:tracks}))Object.defineProperty(video,name,{value,configurable:true,writable:true});
    Object.defineProperty(video,'getBoundingClientRect',{value:()=>({width:640,height:360})});
    const source=readFileSync(new URL('../../backend/cinema/player.js',import.meta.url),'utf8');
    const run=(voice:string)=>JSON.parse(window.eval(source+`(${JSON.stringify({url:'https://example.org/episode',action:'voice',session:'test',voice})})`));
    assert.equal(run('Русская').voice_confirmed,true);assert.equal(tracks[0].enabled,false);assert.equal(tracks[1].enabled,true);
    assert.equal(run('Несуществующая').voice_confirmed,false);assert.equal(tracks[1].enabled,true);
  }finally{await window.happyDOM.abort();}
});

test('official trailer is a separate action and does not request full watch',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    const item=movie('Фильм','planned');item.catalogue_key='tmdb:movie:42';view.receive(snapshot([item]));button(f,'Хочу посмотреть').click();button(f,'Фильм').click();
    sent.length=0;button(f,'Трейлер').click();
    assert.equal(sent.at(-1).op,'trailer');assert.equal(sent.at(-1).key,'tmdb:movie:42');
    assert.equal(sent.some(v=>['watch','open','source_result_open'].includes(v.op)),false);
  }finally{f.window.happyDOM.abort();}
});

test('trailer search result never offers full-watch or source-binding buttons',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item=movie('Фильм','planned');
    view.receive({...snapshot([item]),source_results:{title_id:item.id,episode_id:item.current.id,selected_id:null,message:'Источник для просмотра не найден',results:[{id:'promo',source_name:'Сервис',source_id:'source',kind:'page',url:'https://legal.example/watch',availability:'available',reason:'Только трейлер',classification_reason:'Только трейлер',video_type:'trailer',content_verified:false,configurable:false,access_status:'FREE',match:1,allowed:false,manual_allowed:false,embedded:false,capabilities:{mode:'HTML5'}}]}});
    const dialog=f.window.document.querySelector('dialog')!;assert.match(dialog.textContent,/Источник для просмотра не найден/);
    assert.equal(dialog.querySelector('[data-result-id="promo"] button'),null);
    assert.equal(sent.some(v=>v.op==='source_result_open'),false);
  }finally{f.window.happyDOM.abort();}
});

test('player media binding prevents an action after source replacement',async()=>{
  const window=new Window({url:'https://example.org/film'});try{
    window.document.body.innerHTML='<video src="https://example.org/full.mp4"></video>';const video=window.document.querySelector('video')!;
    for(const [name,value]of Object.entries({readyState:4,duration:120,currentTime:25,paused:true}))Object.defineProperty(video,name,{value,configurable:true,writable:true});
    Object.defineProperty(video,'getBoundingClientRect',{value:()=>({width:640,height:360})});
    let plays=0;Object.defineProperty(video,'play',{value:()=>{plays++;return Promise.resolve();}});
    const source=readFileSync(new URL('../../backend/cinema/player.js',import.meta.url),'utf8');
    const run=(request:object)=>JSON.parse(window.eval(source+`(${JSON.stringify({url:'https://example.org/film',...request})})`));
    const first=run({action:'status'});video.src='https://example.org/preview.mp4';
    assert.equal(run({action:'play',binding:first.binding}).error,'player_changed');assert.equal(plays,0);
  }finally{await window.happyDOM.abort();}
});

test('compact viewer binds controls to the session and only offers observed audio tracks',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    const item=movie('Film','watching');const ep=item.current;ep.links=[{id:'link',episode_id:ep.id,source_id:'source',source_name:'Fixture',url:'https://example.org/full.mp4',voice:''}] as any;
    const initial:any={...snapshot([item]),sources:[{id:'source',name:'Fixture',adapter:'direct'}],session:{title_id:item.id,episode_id:ep.id,mode:'html5'},playback:{state:'PLAYING',message:'Время продвигается.',code:'',token:'session-one',revision:1,last_successful_at:1,source:{title_id:item.id,episode_id:ep.id,link_id:'link',source_id:'source',url:ep.links[0].url,adapter_id:'safari',voice:'',capabilities:{play:true},validation:{content_verified:true}}}};
    view.receive(initial);const panel=f.window.document.querySelector('.cinema-viewer')!;
    assert.match(panel.textContent,/Видео воспроизводится/);assert.equal(panel.querySelector('[aria-label="Дорожка плеера"]'),null);
    [...panel.querySelectorAll('button')].find((b:any)=>b.textContent==='Пауза')!.click();assert.equal(sent.at(-1).token,'session-one');assert.equal(sent.at(-1).action,'pause');
    initial.playback={...initial.playback,token:'session-two',source:{...initial.playback.source,voice:'Русский',capabilities:{audio_tracks:['Русский','English']}}};view.receive(initial);
    const voice=panel.querySelector('[aria-label="Дорожка плеера"]') as HTMLSelectElement;voice.value='English';voice.dispatchEvent(new f.window.Event('change'));assert.equal(sent.at(-1).voice,'English');assert.equal(sent.at(-1).token,'session-two');
    view.connection(false);assert.ok([...panel.querySelectorAll('button,select')].every((n:any)=>n.disabled));
  }finally{f.window.happyDOM.abort();}
});

test('source diagnostics keeps HTTP confirmation separate from a real playback trial',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    const item=movie('Film','watching');const data:any={...snapshot([item]),source_diagnostics:{running:false,rows:[{source_id:'one',name:'External',url:'https://example.org',title:'Film',candidate:{},stages:{site:{status:'confirmed',message:'HTTP 200'},content:{status:'needs_user',message:'Тип видео не подтверждён'},playback:{status:'not_checked',message:'Время не проверялось'}},last_error:null}]}};
    view.receive(data);button(f,'Настройки').click();button(f,'Источники просмотра').click();
    const panel=f.window.document.querySelector('.cinema-source-diagnostics')!;
    assert.match(panel.textContent,/HTTP 200/);assert.match(panel.textContent,/Не проверено/);
    const select=panel.querySelector('select')!;select.value='Film';select.dispatchEvent(new f.window.Event('change'));
    [...panel.querySelectorAll('button')].find((b:any)=>b.textContent==='Проверить все источники')!.click();assert.equal(sent.at(-1).op,'diagnose_sources');assert.equal(sent.at(-1).title_id,'Film');
    [...panel.querySelectorAll('button')].find((b:any)=>b.textContent==='Проверить плеер в Safari')!.click();assert.equal(sent.at(-1).op,'diagnose_player');assert.equal(sent.at(-1).source_id,'one');
    view.connection(false);assert.ok([...panel.querySelectorAll('button,select')].every((b:any)=>b.disabled));
  }finally{f.window.happyDOM.abort();}
});

test('embedded preview duration is rejected before autoplay',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item=movie('Sintel','watching');
    view.receive({...snapshot([item]),session:{title_id:item.id,episode_id:item.current.id,mode:'embedded'},embedded_player:{token:'proof',url:'https://archive.org/full.mp4',title:'Sintel',position:0,title_id:item.id,episode_id:item.current.id,expected_duration:888.06}});
    const video=f.window.document.querySelector('video')!;let plays=0;
    Object.defineProperty(video,'duration',{value:30});Object.defineProperty(video,'play',{value:()=>{plays++;return Promise.resolve();}});
    video.dispatchEvent(new f.window.Event('loadedmetadata'));
    assert.equal(plays,0);assert.equal(sent.at(-1).event,'error');assert.equal(sent.at(-1).duration,30);assert.equal(video.getAttribute('src'),null);assert.match(f.window.document.body.textContent!,/превью/);
  }finally{f.window.happyDOM.abort();}
});

test('detail backdrop, Escape and inner clicks preserve focus and workspace scroll',()=>{
  const f=fixture();try{
    const page=f.window.document.createElement('main');page.className='workspace-page';
    const root=f.window.document.querySelector('#cinema-root')!;root.replaceWith(page);page.append(root);
    const view=new f.CinemaView(()=>{},()=>{},()=>{},()=>{});view.connection(true);view.receive(snapshot([movie('Карточка','watching')]));
    const opener=button(f,'Карточка');opener.focus();page.scrollTop=330;
    opener.click();let dialog=f.window.document.querySelector('dialog')!;
    Object.defineProperty(dialog,'getBoundingClientRect',{value:()=>({left:100,top:100,right:500,bottom:500})});
    let bubbled=0;page.addEventListener('click',()=>bubbled++);
    dialog.querySelector('h2')!.dispatchEvent(new f.window.MouseEvent('click',{bubbles:true,clientX:150,clientY:150}));
    assert.ok(dialog.open);assert.equal(bubbled,0);
    dialog.dispatchEvent(new f.window.PointerEvent('pointerdown',{bubbles:true,clientX:150,clientY:150}));
    dialog.dispatchEvent(new f.window.MouseEvent('click',{bubbles:true,clientX:10,clientY:10}));assert.ok(dialog.open);
    page.scrollTop=800;
    dialog.dispatchEvent(new f.window.PointerEvent('pointerdown',{bubbles:true,clientX:10,clientY:10}));
    dialog.dispatchEvent(new f.window.MouseEvent('click',{bubbles:true,clientX:10,clientY:10}));
    assert.equal(f.window.document.querySelector('dialog'),null);assert.equal(page.scrollTop,330);assert.equal(f.window.document.activeElement,opener);
    opener.click();dialog=f.window.document.querySelector('dialog')!;dialog.dispatchEvent(new f.window.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
    assert.equal(f.window.document.querySelector('dialog'),null);
  }finally{f.window.happyDOM.abort();}
});

test('automatic dialog close on prepared playback keeps the fallback request alive',()=>{
  const f=fixture();try{
    const sent:any[]=[];const item=movie('Фильм','watching');const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);view.receive(snapshot([item]));
    f.window.document.querySelector('.cinema-quick-play').click();assert.equal(sent.at(-1).op,'watch');
    const before=sent.length;
    view.receive({...snapshot([item]),session:{title_id:item.id,episode_id:item.current.id,mode:'embedded'},embedded_player:{token:'pending',title:item.title,title_id:item.id,episode_id:item.current.id,position:0,url:'https://video.example/full.mp4',autoplay:false}});
    assert.equal(f.window.document.querySelector('dialog'),null);
    assert.ok(!sent.slice(before).some(v=>v.op==='watch_cancel'));
  }finally{f.window.happyDOM.abort();}
});

test('Playwright is an explicit diagnostic action and can be cancelled without starting a watch',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item=movie('Film','planned');
    const state={...snapshot([item]),sources:[{id:'source',name:'External',base_url:'https://site.example',adapter:'html5',enabled:true}]};
    view.receive(state);button(f,'Настройки').click();button(f,'Поиск и Safari').click();
    assert.ok(!sent.some(v=>v.op==='browser_diagnostics'));button(f,'Проверить Play и currentTime').click();
    assert.equal(sent.at(-1).op,'browser_diagnostics');assert.equal(sent.at(-1).source_id,'source');assert.equal(sent.at(-1).title_id,item.id);assert.equal(sent.at(-1).play,true);
    view.receive({...state,browser_diagnostics:{id:'a'.repeat(32),running:true,status:'running'}});button(f,'Отменить проверку').click();assert.equal(sent.at(-1).op,'browser_diagnostics_cancel');
    view.receive({...state,browser_diagnostics:{id:'a'.repeat(32),running:false,status:'limited',message:'HTTP 451: доступ ограничен',region:'EE',region_access:'blocked',playback_confirmed:false,report_path:'/private/report.json'}});
    assert.match(f.window.document.querySelector('[data-browser-diagnostics]').textContent,/HTTP 451.*доступ ограничен/);
    assert.ok(!sent.some(v=>v.op==='watch'||v.op==='source_result_open'));
  }finally{f.window.happyDOM.abort();}
});

test('a probable page opens only through an explicit browser action and is never called playing',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);const item=movie('Film','planned');
    view.receive({...snapshot([item]),source_results:{title_id:item.id,episode_id:item.current.id,selected_id:null,message:'Вероятное совпадение',results:[{id:'probable',source_name:'External',source_id:'source',kind:'page',url:'https://site.example/film',availability:'available',viewing_status:'browser_only',identity_status:'probable',reason:'На странице видны название и год',access_status:'UNKNOWN',match:.85,allowed:true,manual_allowed:true,embedded:false,capabilities:{mode:'HTML5'}}]}});
    assert.ok(!sent.some(v=>v.op==='source_result_open'));button(f,'Открыть вероятное совпадение').click();assert.equal(sent.at(-1).player,'browser');
    assert.ok(![...f.window.document.querySelectorAll('button')].some((b:any)=>b.textContent==='Смотреть'));
  }finally{f.window.happyDOM.abort();}
});

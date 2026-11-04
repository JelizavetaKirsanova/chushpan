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
  const sandbox={exports:{} as Record<string,any>,document:window.document,window,icon:()=>'<svg></svg>'};
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
    const sourceButton=[...f.window.document.querySelectorAll('button')].find(n=>n.textContent==='Настройки');sourceButton!.click();
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

test('four tabs keep lists separate and watch requests source choices before opening',()=>{
  const f=fixture();try{
    const sent:any[]=[];const view=new f.CinemaView((v:any)=>sent.push(v),()=>{},()=>{},()=>{});view.connection(true);
    view.receive(snapshot([movie('Начато','watching'),movie('Отложено','planned'),movie('Завершено','watched')]));
    assert.equal(f.window.document.querySelectorAll('.cinema-tabs button').length,4);
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
    const genre=f.window.document.querySelector('[aria-label="Жанр"]') as HTMLSelectElement;genre.value='триллер';genre.dispatchEvent(new f.window.Event('change'));
    const form=f.window.document.querySelector('.cinema-filters')!;form.dispatchEvent(new f.window.Event('submit',{cancelable:true}));
    assert.equal(sent.at(-1).op,'search');assert.equal(sent.at(-1).query,'Триллер');assert.equal(sent.at(-1).genre,'триллер');
    button(f,'Хочу посмотреть').click(); // tab; catalogue quick action is found explicitly below
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

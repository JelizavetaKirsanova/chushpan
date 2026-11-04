import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Window} from 'happy-dom';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';

function fixture() {
  const window=new Window({url:'http://127.0.0.1:8879'});
  window.document.body.innerHTML='<section class="workspace-page"><div id="catalogue"></div></section>';
  const observers:any[]=[];
  class Observer {
    targets=new Set<any>();
    callback:(entries:any[])=>void;options:any;
    constructor(callback:(entries:any[])=>void,options:any){this.callback=callback;this.options=options;observers.push(this);}
    observe(target:any){this.targets.add(target);}
    disconnect(){this.targets.clear();}
    trigger(target:any){if(this.targets.has(target))this.callback([{target,isIntersecting:true}]);}
  }
  Object.defineProperty(window,'IntersectionObserver',{value:Observer,configurable:true});
  window.HTMLElement.prototype.scrollIntoView=()=>{};
  const code=stripTypeScriptTypes(readFileSync(new URL('../src/cinema_catalogue.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'')+'\nexports.api={CinemaCatalogueView,CatalogueWindow,catalogueFeeds};';
  const sandbox={exports:{} as Record<string,any>,document:window.document,window};vm.runInNewContext(code,sandbox);
  return {window,observers,...sandbox.exports.api};
}
function entry(id:number){return {title:`Film ${id}`,kind:'movie',year:2025,poster_url:'',description:'',catalogue_key:`tmdb:movie:${id}`,metadata:{rating:8,genres:[],runtime:100,seasons:[],episode_count:0,popularity:id}};}
function snapshot(extra:object={}){return {type:'cinema',items:[],sources:[],history:[],preferences:{devices:{},startup:'continue'},durable:true,tmdb:{configured:true},...extra};}
function mount(f:any){const sent:any[]=[],changes:any[]=[],watched:any[]=[];const view=new f.CinemaCatalogueView({send:(v:any)=>sent.push(v),detail:()=>{},watch:(v:any)=>watched.push(v),change:(item:any,values:any)=>changes.push({item,values}),remove:()=>{},poster:(item:any)=>{const n=f.window.document.createElement('div');n.textContent=item.title;return n;},settings:()=>{}});view.connection(true);view.receive(snapshot());view.mount(f.window.document.getElementById('catalogue'));return {view,sent,changes,watched};}
function click(f:any,label:string,root:any=f.window.document){const b=[...root.querySelectorAll('button')].find((n:any)=>n.textContent===label) as HTMLButtonElement;assert.ok(b,`Missing button: ${label}`);b.click();}

test('bounded pagination deduplicates cards, pending pages and stale generations',()=>{
  const f=fixture();try{
    const pages=new f.CatalogueWindow();const id=pages.request(1);assert.equal(pages.request(1),null);
    pages.accept({request_id:id,page:1,items:[entry(1),entry(1)],next_page:2});assert.equal(pages.items().length,1);
    for(let page=2;page<=10;page++){const id=pages.request(page);pages.accept({request_id:id,page,items:[entry(page),entry(page-1)],next_page:page+1});}
    assert.equal(pages.pages.size,6);assert.equal(new Set(pages.items().map((i:any)=>i.catalogue_key)).size,pages.items().length);
    const old=pages.request(11);pages.reset();assert.equal(pages.accept({request_id:old,page:11,items:[entry(11)]}),false);assert.equal(pages.pages.size,0);
    const failure=pages.request(1);pages.accept({request_id:failure,error:'wait',retry_after:30});assert.equal(pages.request(1),null);assert.ok(pages.retryAt>Date.now());assert.equal(pages.failedPage,1);
  }finally{f.window.happyDOM.abort();}
});

test('home has fourteen lazy shelves and Show all loads a paged list',()=>{
  const f=fixture();try{
    const {view,sent}=mount(f);assert.equal(f.window.document.querySelectorAll('.cinema-shelf').length,14);
    assert.equal(sent.filter((v:any)=>v.op==='catalogue_page').length,1);
    const request=sent.find((v:any)=>v.op==='catalogue_page');view.receive(snapshot({catalogue_page:{request_id:request.request_id,feed:'trending',page:1,items:[entry(1),entry(2)],next_page:2}}));
    const shelf=f.window.document.querySelector('.cinema-shelf')!;assert.equal(shelf.querySelectorAll('.cinema-card').length,2);click(f,'Смотреть всё',shelf);
    assert.equal(sent.at(-1).filters.feed,'trending');assert.equal(sent.at(-1).filters.page,1);
    const id=sent.at(-1).request_id;view.receive(snapshot({catalogue_page:{request_id:id,feed:'trending',page:1,items:Array.from({length:20},(_,i)=>entry(i)),next_page:2,total_pages:10}}));
    const sentinel=f.window.document.querySelector('.cinema-sentinel');f.observers.at(-1).trigger(sentinel);const next=sent.at(-1);assert.equal(next.filters.page,2);
    f.observers.at(-1).trigger(sentinel);assert.equal(sent.at(-1).request_id,next.request_id);
    view.receive(snapshot({catalogue_page:{request_id:next.request_id,feed:'trending',page:2,items:[entry(19),entry(20)],next_page:3,total_pages:10}}));
    assert.equal(f.window.document.querySelectorAll('.cinema-catalogue-grid .cinema-card').length,21);
  }finally{f.window.happyDOM.abort();}
});

test('debounced global search sends only the final text and combines filters',async()=>{
  const f=fixture();try{
    const {view,sent}=mount(f);view.search('Ин');view.search('Интер');view.search('Interstellar');
    const before=sent.filter((v:any)=>v.op==='catalogue_page').length;
    await new Promise(r=>setTimeout(r,400));
    const requests=sent.filter((v:any)=>v.op==='catalogue_page');assert.equal(requests.length,before+1);assert.equal(requests.at(-1).filters.query,'Interstellar');
    const change=(label:string,value:string)=>{const el=f.window.document.querySelector(`[aria-label="${label}"]`);el.value=value;el.dispatchEvent(new f.window.Event('change'));};
    change('Тип','movie');change('Жанр','триллер');change('Страна','US');change('Язык оригинала','en');change('Рейтинг TMDB','7');change('Сортировка','date');
    f.window.document.querySelector('form')!.dispatchEvent(new f.window.Event('submit',{cancelable:true}));
    const filters=sent.at(-1).filters;assert.equal(filters.kind,'movie');assert.equal(filters.genre,'триллер');assert.equal(filters.country,'US');assert.equal(filters.original_language,'en');assert.equal(filters.rating,7);assert.equal(filters.sort,'date');
    const old=requests.at(-1).request_id;view.receive(snapshot({catalogue_page:{request_id:old,page:1,items:[entry(999)],next_page:null}}));assert.doesNotMatch(f.window.document.querySelector('.cinema-catalogue-grid')?.textContent??'',/Film 999/);
  }finally{f.window.happyDOM.abort();}
});

test('errors preserve loaded cards and retry the failed page without duplicate requests',()=>{
  const f=fixture();try{
    const {view,sent}=mount(f);click(f,'Весь каталог');const first=sent.at(-1);
    view.receive(snapshot({catalogue_page:{request_id:first.request_id,page:1,items:[entry(1)],next_page:2,total_pages:3}}));
    click(f,'Загрузить ещё');const next=sent.at(-1);
    view.receive(snapshot({catalogue_page:{request_id:next.request_id,error:'TMDB недоступен'}}));
    assert.equal(f.window.document.querySelectorAll('.cinema-catalogue-grid .cinema-card').length,1);assert.match(f.window.document.querySelector('.cinema-catalogue-footer')!.textContent,/TMDB недоступен/);
    click(f,'Повторить загрузку');assert.equal(sent.at(-1).filters.page,2);
    const pending=sent.at(-1).request_id;f.observers.at(-1).trigger(f.window.document.querySelector('.cinema-sentinel'));assert.equal(sent.at(-1).request_id,pending);
  }finally{f.window.happyDOM.abort();}
});

test('catalogue actions are explicit and recommendations hide watched items immediately',()=>{
  const f=fixture();try{
    const {view,sent,changes,watched}=mount(f);click(f,'Для меня');const first=sent.at(-1);
    const library={id:'local',catalogue_key:'tmdb:movie:1',title:'Film 1',status:'watched',favorite:0};
    view.receive(snapshot({items:[library],catalogue_page:{request_id:first.request_id,page:1,items:[entry(1),entry(2)],next_page:null}}));
    assert.equal(f.window.document.querySelectorAll('.cinema-catalogue-grid .cinema-card').length,1);assert.doesNotMatch(f.window.document.querySelector('.cinema-catalogue-grid')!.textContent,/Film 1/);
    click(f,'Хочу посмотреть');assert.equal(changes.at(-1).values.status,'planned');click(f,'Смотрю');assert.equal(changes.at(-1).values.status,'watching');click(f,'В избранное');assert.equal(changes.at(-1).values.favorite,true);click(f,'Смотреть');assert.equal(watched.at(-1).catalogue_key,'tmdb:movie:2');
    assert.equal(sent.filter((v:any)=>v.op==='update').length,0); // The host owns SQLite actions; browsing itself cannot write statuses.
  }finally{f.window.happyDOM.abort();}
});

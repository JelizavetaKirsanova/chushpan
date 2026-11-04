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
    const sourceButton=[...f.window.document.querySelectorAll('button')].find(n=>n.textContent==='Источники');sourceButton!.click();
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

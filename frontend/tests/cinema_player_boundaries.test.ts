// DOM doubles exercise the shipped player. They do not validate real Safari/TCC.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {Window} from 'happy-dom';

const player=readFileSync(new URL('../../backend/cinema/player.js',import.meta.url),'utf8');
const url='https://example.org/film';
function run(window:Window,request:object){
  return JSON.parse(window.eval(player+`(${JSON.stringify({url,action:'status',...request})})`));
}

test('inaccessible iframe cannot produce a successful player or trigger playback',async()=>{
  const window=new Window({url});try{
    window.document.body.innerHTML='<iframe></iframe>';
    const frame=window.document.querySelector('iframe')!;
    Object.defineProperty(frame,'contentDocument',{get(){throw new window.DOMException('Blocked','SecurityError');}});
    assert.equal(run(window,{}).error,'cross_origin_player');
    assert.equal(run(window,{action:'play',session:'test'}).error,'cross_origin_player');
    assert.equal(window.__chushpanCinema,undefined);
    assert.equal(window.document.querySelector('[data-chushpan-cinema]'),null);
  }finally{await window.happyDOM.abort();}
});

test('same-origin iframe reports its actual media state and frame URL',async()=>{
  const window=new Window({url});const child=new Window({url:'https://example.org/player'});try{
    window.document.body.innerHTML='<iframe></iframe>';
    child.document.body.innerHTML='<video src="https://example.org/full.mp4"></video>';
    Object.defineProperty(window.document.querySelector('iframe')!,'contentDocument',{value:child.document});
    const video=child.document.querySelector('video')!;
    for(const [key,value]of Object.entries({readyState:4,duration:120,currentTime:35,paused:true,seeking:false}))Object.defineProperty(video,key,{value,configurable:true});
    Object.defineProperty(video,'getBoundingClientRect',{value:()=>({width:640,height:360})});
    const state=run(window,{});
    assert.equal(state.position,35);assert.equal(state.paused,true);
    assert.equal(state.media_evidence.frame_url,'https://example.org/player');
    assert.equal(state.media_evidence.media_url,'https://example.org/full.mp4');
  }finally{await window.happyDOM.abort();await child.happyDOM.abort();}
});

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

test('gesture button refuses a replaced media URL before play or fullscreen',async()=>{
  const window=new Window({url});try{
    window.document.body.innerHTML='<video src="https://example.org/full.mp4"></video>';
    const video=window.document.querySelector('video')!;let plays=0,fullscreens=0;
    for(const [key,value]of Object.entries({readyState:4,duration:120,currentTime:35,paused:true}))Object.defineProperty(video,key,{value,configurable:true});
    Object.defineProperty(video,'getBoundingClientRect',{value:()=>({width:640,height:360})});
    Object.defineProperty(video,'requestFullscreen',{value:()=>{fullscreens++;return Promise.resolve();}});
    Object.defineProperty(video,'play',{value:()=>{plays++;return Promise.resolve();}});
    run(window,{action:'offer_gesture',session:'test'});
    const button=window.document.querySelector<HTMLButtonElement>('[data-chushpan-cinema]')!;
    video.src='https://example.org/other.mp4';button.click();
    assert.equal(plays,0);assert.equal(fullscreens,0);assert.match(button.textContent!,/Плеер изменился/);
  }finally{await window.happyDOM.abort();}
});

test('fullscreen stays pending until the browser transition promise resolves',async()=>{
  const window=new Window({url});try{
    window.document.body.innerHTML='<video></video>';const video=window.document.querySelector('video')!;
    for(const [key,value]of Object.entries({readyState:4,duration:120,currentTime:35,paused:true}))Object.defineProperty(video,key,{value,configurable:true});
    Object.defineProperty(video,'getBoundingClientRect',{value:()=>({width:640,height:360})});
    let full=false;let finish!:()=>void;
    Object.defineProperty(window.document,'fullscreenElement',{get:()=>full?video:null});
    Object.defineProperty(video,'requestFullscreen',{value:()=>{full=true;return new Promise<void>(resolve=>{finish=resolve;});}});
    const pending=run(window,{action:'fullscreen',session:'test'});
    assert.equal(pending.fullscreen,true);assert.equal(pending.fullscreen_pending,true);
    assert.equal(run(window,{}).fullscreen_pending,true);
    finish();await new Promise(resolve=>setTimeout(resolve,0));
    assert.equal(run(window,{}).fullscreen_pending,false);
  }finally{await window.happyDOM.abort();}
});

test('public video inside an open shadow root is detected and host fullscreen is observed',async()=>{
  const window=new Window({url});try{
    const host=window.document.createElement('ia-media-player');window.document.body.append(host);
    const root=host.attachShadow({mode:'open'});const video=window.document.createElement('video');root.append(video);
    for(const [key,value]of Object.entries({readyState:4,duration:120,currentTime:35,paused:true}))Object.defineProperty(video,key,{value,configurable:true});
    Object.defineProperty(video,'getBoundingClientRect',{value:()=>({width:640,height:360})});
    let fullscreenElement:Element=host;
    let hostWidth=640;
    Object.defineProperty(host,'getBoundingClientRect',{value:()=>({width:hostWidth,height:360})});
    Object.defineProperty(window.document,'fullscreenElement',{get:()=>fullscreenElement});
    const state=run(window,{});assert.equal(state.position,35);assert.equal(state.fullscreen,true);assert.equal(state.error,undefined);
    hostWidth=1440;
    assert.equal(run(window,{}).fullscreen,false);
    fullscreenElement=window.document.body;
    assert.equal(run(window,{}).fullscreen,false);
  }finally{await window.happyDOM.abort();}
});

test('closed shadow root remains inaccessible and cannot claim a player',async()=>{
  const window=new Window({url});try{
    const host=window.document.createElement('private-player');window.document.body.append(host);
    const root=host.attachShadow({mode:'closed'});root.append(window.document.createElement('video'));
    assert.equal(run(window,{}).error,'no_player');
  }finally{await window.happyDOM.abort();}
});

test('metadata preparation loads one lazy video once and never starts it',async()=>{
  const window=new Window({url});try{
    const host=window.document.createElement('play-av');window.document.body.append(host);const root=host.attachShadow({mode:'open'});const video=window.document.createElement('video');root.append(video);
    Object.defineProperty(video,'getBoundingClientRect',{value:()=>({width:640,height:360})});Object.defineProperty(video,'readyState',{value:0});
    let loads=0,plays=0;video.load=()=>{loads++;};video.play=()=>{plays++;return Promise.resolve();};
    assert.equal(run(window,{action:'prepare'}).playback_confirmed,false);run(window,{action:'prepare'});
    assert.equal(loads,1);assert.equal(plays,0);assert.equal(video.preload,'metadata');
  }finally{await window.happyDOM.abort();}
});

test('a real video awaiting metadata is distinct from an absent player',async()=>{
  const window=new Window({url});try{
    const video=window.document.createElement('video');window.document.body.append(video);
    Object.defineProperty(video,'getBoundingClientRect',{value:()=>({width:640,height:360})});
    Object.defineProperty(video,'readyState',{value:0});assert.equal(run(window,{}).error,'player_loading');
    video.remove();assert.equal(run(window,{}).error,'no_player');
  }finally{await window.happyDOM.abort();}
});

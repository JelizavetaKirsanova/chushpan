import test from 'node:test';
import assert from 'node:assert/strict';
import {microphonePresentation,requestMicrophone} from '../src/voice_activation.ts';

test('stopped microphone cannot be presented as waiting for the wake phrase',()=>{
  const state=microphonePresentation('sleeping','stopped');
  assert.equal(state?.label,'Микрофон выключен');
  assert.equal(state?.button,'Включить микрофон');
  assert.equal(microphonePresentation('listening','stopped')?.label,'Микрофон выключен');
  assert.equal(microphonePresentation('sleeping','starting')?.button,'Подготовка микрофона…');
  assert.equal(microphonePresentation('thinking','stopped'),null);
  assert.equal(microphonePresentation('speaking','stopped'),null);
  assert.equal(microphonePresentation('sleeping','listening'),null);
});
test('microphone request is explicit and does not announce input before backend confirmation',async()=>{
  const calls:string[]=[];
  const api={desktop_action:async(action:string)=>{calls.push(action);return {ok:true,message:'ok'};}};
  assert.equal(calls.length,0);
  await requestMicrophone(api,()=>calls.push('starting'),m=>calls.push(m));
  assert.deepEqual(calls,['starting','listen']);
});
test('denied or unavailable native microphone reports an actionable error',async()=>{
  const errors:string[]=[];
  await requestMicrophone(undefined,()=>{throw Error('must not start');},m=>errors.push(m));
  await requestMicrophone({desktop_action:async()=>({ok:false,message:'Нет разрешения macOS'})},()=>{},m=>errors.push(m));
  assert.match(errors[0],/Чушпан.app/);
  assert.equal(errors[1],'Нет разрешения macOS');
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';
import {Window} from 'happy-dom';
import {timeline} from '../src/performance_state.ts';
import {seconds} from '../src/workspace_state.ts';

test('statistics separates voice, chat, playback and controlled PCM benchmarks', async () => {
  const window = new Window({settings:{disableJavaScriptEvaluation:true, disableCSSFileLoading:true, disableJavaScriptFileLoading:true}});
  const html = readFileSync(new URL('../index.html',import.meta.url),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,'');
  window.document.write(html);
  const source = readFileSync(new URL('../src/performance.ts',import.meta.url),'utf8');
  const code = stripTypeScriptTypes(source,{mode:'transform'}).replace(/^import .*;\n/gm,'').replace('export class PerformancePanel','class PerformancePanel')+'\nexports.PerformancePanel=PerformancePanel;';
  const sandbox = {exports:{} as Record<string,unknown>, document:window.document, window, seconds, timeline, setInterval:()=>0, setTimeout:()=>0, performance};
  vm.runInNewContext(code,sandbox);
  const Panel = sandbox.exports.PerformancePanel as new (...args:unknown[])=>{connection:(online:boolean)=>void;receive:(event:unknown)=>void};
  const panel = new Panel(()=>{},()=>{});
  const summary = {count:4,measured:1,failed:1,cancelled:1,incomplete:1,mean_s:2,median_s:2,p95_s:2,max_s:2,over:{'5':0,'10':0,'15':0}};
  const event = {type:'performance_stats',days:1,summary,providers:{},requests:[],active:null,
    kinds:{voice:summary,text:{...summary,median_s:.1}},completion:{voice:{...summary,median_s:60}},
    comparison:{voice:{before:{...summary,median_s:12},after:summary}},
    benchmarks:{before:{summary,categories:{},configuration:{model:'test',num_ctx:16384}},after:{summary,categories:{},configuration:{model:'test',num_ctx:16384}}}};
  try {
    panel.connection(true);panel.receive(event);
    assert.match(window.document.getElementById('performance-summary')!.textContent,/1 \/ 1 \/ 1/);
    assert.match(window.document.getElementById('performance-samples')!.textContent,/60.*включая озвучку/);
    assert.match(window.document.getElementById('performance-comparison')!.textContent,/До: p50 12/);
    assert.match(window.document.getElementById('performance-benchmarks')!.textContent,/готовность PCM/);
    const select = window.document.getElementById('performance-kind') as unknown as HTMLSelectElement;
    select.value = 'text';select.dispatchEvent(new window.Event('change'));
    panel.receive(event);
    assert.match(window.document.getElementById('performance-summary')!.textContent,/0[,.]10/);
  } finally {await window.happyDOM.abort();}
});

test('history filters reach backend and model comparison does not invent p95 or availability', async () => {
  const window = new Window({settings:{disableJavaScriptEvaluation:true, disableCSSFileLoading:true, disableJavaScriptFileLoading:true}});
  window.document.write(readFileSync(new URL('../index.html',import.meta.url),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,''));
  const source=stripTypeScriptTypes(readFileSync(new URL('../src/performance.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace('export class PerformancePanel','class PerformancePanel')+'\nexports.PerformancePanel=PerformancePanel;';
  const sandbox={exports:{} as Record<string,unknown>,document:window.document,window,seconds,timeline,setInterval:()=>0,setTimeout:()=>0,performance};
  vm.runInNewContext(source,sandbox);
  const calls:unknown[][]=[];
  const Panel=sandbox.exports.PerformancePanel as new (...args:unknown[])=>{connection:(v:boolean)=>void;receive:(v:unknown)=>void};
  const panel=new Panel((...args:unknown[])=>calls.push(args),()=>{});
  const summary={count:2,measured:2,failed:0,cancelled:0,mean_s:1,median_s:1,p95_s:2,max_s:2,over:{'5':0,'10':0,'15':0}};
  try {
    panel.connection(true);
    const event={type:'performance_stats',days:1,summary,providers:{},requests:[],active:null,
      filter_options:{model:['qwen3:8b'],request_class:['routine'],input_mode:['microphone']},
      models:[{provider:'ollama',model:'qwen3:8b',requests:2,attempts:2,fallbacks:1,first_token:summary,completion:summary}],
      series:[{created_at:new Date().toISOString(),kind:'voice',status:'completed',first_content_s:1,total_s:2}]};
    panel.receive(event);
    const text=window.document.getElementById('performance-models')!.textContent;
    assert.match(text,/нужно 20 измерений/);assert.match(text,/доступность ещё не проверена/);
    assert.equal(window.document.querySelectorAll('.perf-latency-bar').length,1);
    const model=window.document.getElementById('performance-model') as unknown as HTMLSelectElement;
    model.value='qwen3:8b';model.dispatchEvent(new window.Event('change'));
    assert.equal((calls.at(-1)![1] as Record<string,string>).model,'qwen3:8b');
    const period=window.document.getElementById('performance-period') as unknown as HTMLSelectElement;
    period.value='0';period.dispatchEvent(new window.Event('change'));
    assert.equal(calls.at(-1)![0],0);
  } finally {await window.happyDOM.abort();}
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import vm from 'node:vm';
import {Window} from 'happy-dom';

function fixture(){
  const window=new Window({settings:{disableJavaScriptEvaluation:true,disableCSSFileLoading:true,disableJavaScriptFileLoading:true}});
  window.document.write('<section id="module-memory"></section>');
  const code=['module_ui','memory_library'].map(name=>stripTypeScriptTypes(readFileSync(new URL('../src/'+name+'.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^import .*;\n/gm,'').replace(/^export /gm,'')).join('\n')+'\nexports.View=MemoryLibraryView;';
  const sandbox={exports:{} as any,document:window.document,window,HTMLInputElement:window.HTMLInputElement,HTMLTextAreaElement:window.HTMLTextAreaElement,CustomEvent:window.CustomEvent,confirm:()=>true,setTimeout:()=>0,clearTimeout:()=>{},Blob:window.Blob,URL:window.URL,atob};
  vm.runInNewContext(code,sandbox);const sent:any[]=[];const view=new sandbox.exports.View((channel:string,value:any)=>sent.push({channel,...value}),()=>{});
  view.connection(true);
  const data={items:[{id:1,content:'Проект Орион',category:'work',source:'desktop_library',confidence:1,confirmed:1,sensitive:0,created_at:'2026-11-06',updated_at:'2026-11-06',confirmed_at:'2026-11-06',revision:1,project_id:null,expires_at:null,status:'active'}],projects:[],total:1,used_today:0,categories:{work:'Работа',study:'Учёба'},pending:[],access:[],fts:true,policy:{automatic:'auto'},conversations:[{id:7,title:'Орион',preview:''}],bindings:[],entities:[{id:2,name:'Орион',memories:1}],entity_links:[{memory_id:1,entity_id:2}],evidence:[{memory_id:1,conversation_id:7,message_id:3}],relations:[],versions:[],actions:[],backups:[]};
  view.receive({type:'memory_library',busy:false,data});
  const root=window.document.getElementById('module-memory')!;
  function click(label:string,scope:ParentNode=root){const b=[...scope.querySelectorAll('button')].find(b=>b.textContent===label);assert.ok(b,label);b.click();}
  return {window,view,sent,data,root,click};
}

test('relations navigate to an entity filter and history opens evidence without guessing',()=>{
  const f=fixture();try{
    f.click('Связи');f.click('Открыть связанные');assert.equal(f.sent.at(-1).entity,2);
    f.view.receive({type:'memory_library',busy:false,data:f.data});f.click('Подробнее');
    f.click('Связанный разговор 7',f.window.document.querySelector('dialog')!);
    assert.equal(f.sent.at(-1).op,'conversation_open');assert.equal(f.sent.at(-1).id,7);
  }finally{f.window.happyDOM.abort();}
});

test('history deletion offers independent facts deletion and shows sent status honestly',()=>{
  const f=fixture();try{
    f.view.receive({type:'memory_library',busy:false,data:{...f.data,selected_conversation:7,messages:[{id:3,role:'user',content:'Сделай свет',created_at:'2026-11-06'}],actions:[{id:8,conversation_id:7,intent:'Сделай свет',state:'sent',created_at:'2026-11-06'}]}});
    f.click('История');assert.match(f.root.textContent!,/Команда отправлена/);assert.doesNotMatch(f.root.textContent!,/Результат подтверждён/);
    f.root.querySelector<HTMLInputElement>('[name=delete_facts]')!.checked=true;f.click('Удалить разговор');
    assert.equal(f.sent.at(-1).delete_facts,true);assert.equal(f.sent.at(-1).confirmed,true);
  }finally{f.window.happyDOM.abort();}
});

test('memory policy saves off, retention and category exclusions through actual form',()=>{
  const f=fixture();try{
    f.click('Настройки памяти');f.root.querySelector<HTMLSelectElement>('[name=automatic]')!.value='off';
    f.root.querySelector<HTMLInputElement>('[name=action_retention_days]')!.value='14';
    const excluded=[...f.root.querySelectorAll('label')].find(l=>l.textContent?.includes('Не запоминать: Учёба'))!;
    const check=excluded.querySelector('input')!;check.checked=true;check.dispatchEvent(new f.window.Event('change'));
    f.click('Сохранить настройки');const value=f.sent.at(-1).value;
    assert.equal(value.automatic,'off');assert.equal(value.action_retention_days,14);assert.deepEqual(Array.from(value.excluded),['study']);
  }finally{f.window.happyDOM.abort();}
});

test('import preview is reviewable and sensitive consent is separate',()=>{
  const f=fixture();try{
    f.view.receive({type:'memory_library',busy:false,data:{...f.data,import_preview:{token:'preview',count:1,sensitive:1,items:[{content:'Синтетический чувствительный факт',category:'work'}]}}});
    f.click('Настройки памяти');assert.match(f.root.textContent!,/Синтетический чувствительный факт/);
    f.click('Подтвердить импорт');assert.equal(f.sent.at(-1).op,'import_apply');assert.equal(f.sent.at(-1).token,'preview');assert.equal(f.sent.at(-1).consent_sensitive,false);
  }finally{f.window.happyDOM.abort();}
});

test('encrypted backup uses masked password, explicit submit and clears the field',()=>{
  const f=fixture();try{
    f.click('Настройки памяти');f.click('Создать защищённую копию');const modal=f.window.document.querySelector('dialog')!;
    const input=modal.querySelector<HTMLInputElement>('[name=password]')!;assert.equal(input.type,'password');input.value='test-only-long-passphrase';
    f.click('Создать копию',modal);assert.equal(f.sent.at(-1).op,'backup');assert.equal(input.value,'');
    assert.equal(modal.isConnected,false);
  }finally{f.window.happyDOM.abort();}
});

/** Exercises the shipped theme initializer with isolated DOM, storage and OS events. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {stripTypeScriptTypes} from 'node:module';
import vm from 'node:vm';
import {Window} from 'happy-dom';

const code=stripTypeScriptTypes(readFileSync(new URL('../src/theme.ts',import.meta.url),'utf8'),{mode:'transform'});
type ThemeMode='system'|'light'|'dark';
type StoragePort={getItem:(key:string)=>string|null;setItem:(key:string,value:string)=>void};

function storage(initial?:string):StoragePort {
  const values=new Map<string,string>(initial===undefined?[]:[['chushpan:theme',initial]]);
  return {getItem:key=>values.get(key)??null,setItem:(key,value)=>{values.set(key,value);}};
}

function fixture(localStorage:StoragePort=storage(),light=false) {
  const window=new Window({url:'http://127.0.0.1:8879',settings:{disableJavaScriptEvaluation:true,disableCSSFileLoading:true,disableJavaScriptFileLoading:true}});
  window.document.body.innerHTML='<select id="theme-mode"><option value="system">Системная</option><option value="light">Светлая</option><option value="dark">Тёмная</option></select>';
  const changes=new Set<()=>void>();
  const media={matches:light,addEventListener:(event:string,listener:()=>void)=>{assert.equal(event,'change');changes.add(listener);}};
  Object.defineProperty(window,'matchMedia',{value:(query:string)=>{assert.equal(query,'(prefers-color-scheme: light)');return media;}});
  vm.runInNewContext(code,{window,document:window.document,localStorage});
  const root=window.document.documentElement;
  const control=window.document.getElementById('theme-mode')! as unknown as HTMLSelectElement;
  return {
    window,root,control,
    select:(mode:ThemeMode)=>{control.value=mode;control.dispatchEvent(new window.Event('change') as unknown as Event);},
    system:(isLight:boolean)=>{media.matches=isLight;changes.forEach(listener=>listener());},
  };
}

for (const mode of ['light','dark','system'] as const) {
  test(`theme ${mode} persists through a fresh initialization`,async()=>{
    const shared=storage();
    const first=fixture(shared,true);
    try {
      first.select(mode);
      assert.equal(shared.getItem('chushpan:theme'),mode);
      assert.equal(first.root.dataset.themeMode,mode);
      assert.equal(first.root.dataset.theme,mode==='system'?'light':mode);
    }finally{await first.window.happyDOM.close();}
    const reopened=fixture(shared,false);
    try {
      assert.equal(reopened.control.value,mode);
      assert.equal(reopened.root.dataset.themeMode,mode);
      assert.equal(reopened.root.dataset.theme,mode==='system'?'dark':mode);
      assert.equal(reopened.root.style.colorScheme,reopened.root.dataset.theme);
    }finally{await reopened.window.happyDOM.close();}
  });
}

test('OS appearance events affect system mode and leave explicit light/dark choices intact',async()=>{
  const f=fixture();try {
    assert.equal(f.root.dataset.theme,'dark');
    f.system(true);assert.equal(f.root.dataset.theme,'light');
    f.select('dark');f.system(false);f.system(true);
    assert.equal(f.root.dataset.theme,'dark');assert.equal(f.root.style.colorScheme,'dark');
    f.select('light');f.system(false);
    assert.equal(f.root.dataset.theme,'light');assert.equal(f.root.style.colorScheme,'light');
    f.select('system');assert.equal(f.root.dataset.theme,'dark');
    f.system(true);assert.equal(f.root.dataset.theme,'light');
    assert.equal(f.root.dataset.themeMode,'system');
  }finally{await f.window.happyDOM.close();}
});

for (const saved of [undefined,'invalid-theme']) {
  test(`theme defaults to current system appearance with ${saved===undefined?'missing':'invalid'} storage`,async()=>{
    const f=fixture(storage(saved),true);try {
      assert.equal(f.control.value,'system');assert.equal(f.root.dataset.themeMode,'system');
      assert.equal(f.root.dataset.theme,'light');assert.equal(f.root.style.colorScheme,'light');
      f.system(false);assert.equal(f.root.dataset.theme,'dark');
    }finally{await f.window.happyDOM.close();}
  });
}

test('unavailable storage still permits theme changes for this session and falls back on restart',async()=>{
  const unavailable:StoragePort={getItem:()=>{throw new Error('Storage unavailable');},setItem:()=>{throw new Error('Storage unavailable');}};
  const f=fixture(unavailable,true);try {
    assert.equal(f.root.dataset.themeMode,'system');assert.equal(f.root.dataset.theme,'light');
    assert.doesNotThrow(()=>f.select('dark'));
    assert.equal(f.root.dataset.themeMode,'dark');assert.equal(f.root.dataset.theme,'dark');
  }finally{await f.window.happyDOM.close();}
  const reopened=fixture(unavailable,true);try {
    assert.equal(reopened.root.dataset.themeMode,'system');assert.equal(reopened.root.dataset.theme,'light');
  }finally{await reopened.window.happyDOM.close();}
});

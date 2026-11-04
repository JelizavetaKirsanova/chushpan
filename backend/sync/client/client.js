'use strict';
const byId = id => document.getElementById(id);
const encoded = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'');
let db, keys, identity, trusted = false, online = false, capabilities = [], pendingCommands = new Map(), queued = [], invitation = new URLSearchParams(location.hash.slice(1)).get('pair'), lastSync=0;
history.replaceState(null,'',location.pathname);
function note(message) { byId('notice').textContent = message; }
function storage(operation, key, value) { return new Promise((resolve,reject) => { const transaction=db.transaction('local',operation); const store=transaction.objectStore('local'); const request=operation==='readonly'?store.get(key):value===undefined?store.delete(key):store.put(value,key); let result;request.onsuccess=()=>{result=request.result;};transaction.oncomplete=()=>resolve(result);transaction.onerror=()=>reject(new Error('Локальное хранилище недоступно')); }); }
async function request(operation, value={}) {
  const body=JSON.stringify(value), timestamp=String(Math.floor(Date.now()/1000)), nonce=crypto.randomUUID().replaceAll('-','');
  const payload=`/api/${operation}\n${timestamp}\n${nonce}\n${body}`;
  const signature=await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},keys.privateKey,new TextEncoder().encode(payload));
  const response=await fetch(`/api/${operation}`,{method:'POST',headers:{'Content-Type':'application/json','X-Device':identity,'X-Time':timestamp,'X-Nonce':nonce,'X-Signature':encoded(signature)},body,signal:AbortSignal.timeout(12000)});
  const result=await response.json();
  if(!response.ok) {const error=new Error(result.error||'Сервис недоступен');error.status=response.status;throw error;}
  return result;
}
function card(title, detail='') { const node=document.createElement('article');node.className='card';const heading=document.createElement('strong');heading.textContent=title;node.append(heading);if(detail){const p=document.createElement('p');p.textContent=detail;node.append(p);}return node; }
function button(label, action) { const node=document.createElement('button');node.type='button';node.textContent=label;node.onclick=()=>Promise.resolve(action()).catch(error=>note(error.message));return node; }
function message(text, role='assistant') { const node=document.createElement('div');node.className='message '+role;node.textContent=text;byId('messages').append(node); }
async function command(operation, args, options={}) {
  if (!online) {
    if(operation==='chat'&&byId('queue-opt-in').checked&&queued.length<10) {
      queued.push({operation,arguments:args,id:crypto.randomUUID().replaceAll('-',''),expires:Date.now()/1000+60});
      await storage('readwrite','queue',queued);note('Текст в очереди на этом устройстве. Через 60 секунд запрос истечёт.');return;
    }
    throw new Error('Mac недоступен. Команда не отправлена.');
  }
  const item={operation,arguments:args,id:options.id||crypto.randomUUID().replaceAll('-',''),expires:options.expires||Date.now()/1000+90};
  const result=await request('command',item);
  if(result.status==='running'){pendingCommands.set(item.id,operation);note('Команда принята. Ожидаю подтверждённый результат…');}
  else note('Состояние команды: '+result.status);
}
function statusView(data) {
  online=true;trusted=true;capabilities=data.capabilities;byId('connection').textContent='Mac на связи';byId('workspace').hidden=false;byId('pairing').hidden=true;
  byId('chat-form').querySelector('button').disabled=!capabilities.includes('chat');
  byId('app-form').hidden=!capabilities.includes('open_app');
  byId('scenarios').replaceChildren(...data.scenarios.map(s=>{const node=card(s.name);node.append(button('Запустить',()=>command('scenario',{id:s.id,revision:s.revision})));return node;}));
  if(!data.scenarios.length)byId('scenarios').append(card('Нет разрешённых сценариев','Выбери разрешение «Сценарии» в карточке устройства на Mac.'));
  byId('devices').replaceChildren(...data.devices.map(d=>{const node=card(d.name,d.kind);if(['led_strip','wled'].includes(d.kind)){node.append(button('Включить',()=>command('device',{target:d.id,action:'power',power:true})),button('Выключить',()=>command('device',{target:d.id,action:'power',power:false})));}return node;}));
  if(!data.devices.length)byId('devices').append(card('Устройства не разрешены','Разрешения выдаются на Mac.'));
  byId('command-history').replaceChildren(...data.history.map(r=>{const node=card(r.operation.split(':')[0],new Date(r.created_at*1000).toLocaleString()+' · '+r.status);if(r.status==='running')node.append(button('Остановить',()=>command('cancel',{id:r.command_id})));return node;}));
}
async function poll() {
  if (!keys) return;
  try {
    if (!trusted) {
      const state=await request('pair_status');
      if(state.status==='pending'){byId('pair-code').textContent=state.code;byId('pair-status').textContent='Подтверди этот же код на Mac.';return;}
      trusted=true;await storage('readwrite','trusted',true);
    }
    const data=await request('status');statusView(data);
    if(data.categories.length&&Date.now()-lastSync>30000)await synchronize();
    for(const [id,operation] of pendingCommands) {
      const result=await request('result',{id});
      if(result.status==='running')continue;
      pendingCommands.delete(id);
      const detail=result.result||{};note(detail.error||detail.message||result.status);
      if(operation==='chat'&&detail.message)message(detail.message);
      if(result.status==='confirmation_required'){byId('confirm-form').hidden=false;byId('confirm-message').textContent=detail.message;}
    }
    const ready=queued;queued=[];await storage('readwrite','queue',queued);
    for(const item of ready){if(item.expires>Date.now()/1000) await command('chat',item.arguments,item);else note('Устаревший запрос удалён из очереди.');}
  } catch(error) {
    online=false;byId('connection').textContent='Нет подтверждённой связи';
    if(error.status===403){await storage('readwrite','cache');byId('sync-data').replaceChildren();byId('sync-time').textContent='Доступ не подтверждён. Локальная копия удалена.';}
    if(trusted)note(error.message+' Выполненные команды автоматически не повторяются.');
  }
}
async function synchronize() {
  const result=await request('sync');await storage('readwrite','cache',result);
  lastSync=Date.now();
  renderCache(result);note('Синхронизированы только выбранные на Mac категории.');
}
function renderCache(cache) {
  if(!cache)return;
  byId('sync-time').textContent='Последняя копия: '+new Date(cache.server_time*1000).toLocaleString();
  const output=[];
  for(const [kind,items] of Object.entries(cache.data)) {
    output.push(card(kind,Array.isArray(items)?items.length+' записей':'Сохранено'));
    for(const item of (Array.isArray(items)?items:[]).slice(0,100)) {
      const node=card(item.content||item.name||item.title||item.role||item.id, item.created_at||item.last_viewed||'');
      if(kind==='memory')node.append(button('Предложить исправление',async()=>{const content=prompt('Новый текст (потребуется подтверждение на Mac)',item.content);if(content){const result=await request('memory_update',{id:item.id,revision:item.revision,content});note(result.status==='conflict'?'На Mac уже другая версия. Конфликт ожидает решения.':'Исправление ожидает подтверждения на Mac.');}}));
      output.push(node);
    }
  }
  byId('sync-data').replaceChildren(...output);
}
byId('pair-form').onsubmit=async event=>{event.preventDefault();try{const key=await crypto.subtle.exportKey('jwk',keys.publicKey);const result=await request('pair',{invite:invitation,key,name:byId('peer-name').value,kind:byId('peer-kind').value});invitation=null;byId('pair-code').textContent=result.code;byId('pair-status').textContent='Теперь сравни код и подтверди на Mac.';byId('pair-form').hidden=true;}catch(error){note(error.message);}};
byId('chat-form').onsubmit=async event=>{event.preventDefault();try{const text=byId('message').value.trim();if(!text)return;await command('chat',{text});message(text,'user');byId('message').value='';}catch(error){note(error.message);}};
byId('app-form').onsubmit=async event=>{event.preventDefault();try{await command('open_app',{name:byId('app-name').value});}catch(error){note(error.message);}};
byId('confirm-form').onsubmit=async event=>{event.preventDefault();try{await command('confirm',{code:byId('confirm-code').value});byId('confirm-form').hidden=true;byId('confirm-code').value='';}catch(error){note(error.message);}};
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{document.querySelectorAll('.tab').forEach(tab=>tab.hidden=tab.id!==button.dataset.tab);document.querySelectorAll('[data-tab]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));});
byId('sync-now').onclick=()=>synchronize().catch(error=>note(error.message));
byId('refresh').onclick=()=>poll();
byId('reconnect').onclick=()=>request('reconnect').then(statusView).catch(error=>note(error.message));
byId('clear-cache').onclick=async()=>{await storage('readwrite','cache');byId('sync-data').replaceChildren();byId('sync-time').textContent='Локальная копия очищена.';};
byId('forget').onclick=async()=>{if(confirm('Удалить ключ и локальные данные этого браузера? На Mac также отзови доверие устройству.')){db.close();indexedDB.deleteDatabase('chushpan-companion');location.reload();}};
(async()=>{
  if(!crypto.subtle){note('Нужен доверенный HTTPS-сертификат и современный Safari. HTTP не поддерживается.');return;}
  db=await new Promise((resolve,reject)=>{const request=indexedDB.open('chushpan-companion',1);request.onupgradeneeded=()=>request.result.createObjectStore('local');request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(new Error('Нужен доступ к IndexedDB.'));});
  keys=await storage('readonly','keys');
  if(!keys){keys=await crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},false,['sign','verify']);await storage('readwrite','keys',keys);}
  identity=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await crypto.subtle.exportKey('spki',keys.publicKey)))).map(n=>n.toString(16).padStart(2,'0')).join('');
  trusted=!!await storage('readonly','trusted');queued=(await storage('readonly','queue')||[]).filter(item=>item.operation==='chat'&&item.expires>Date.now()/1000).slice(0,10);
  renderCache(await storage('readonly','cache'));
  if(invitation){trusted=false;byId('pairing').hidden=false;byId('connection').textContent='Сопряжение';}
  else if(!trusted){byId('pairing').hidden=false;byId('pair-form').hidden=true;byId('pair-status').textContent='Создай новый QR-код в разделе «Мои устройства» на Mac.';}
  else await poll();
  setInterval(poll,8000);
})().catch(error=>note(error.message));

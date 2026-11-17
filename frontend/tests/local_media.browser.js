// Isolated real-browser checks, invoked by scripts/check_local_media.py.
window.mediaChecks = {
  legacy: async () => {
  const db=await new Promise((resolve,reject)=>{
    const r=indexedDB.open('chushpan-local-media',1);
    r.onupgradeneeded=()=>r.result.createObjectStore('files',{keyPath:'id'});
    r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);
  });
  const tx=db.transaction('files','readwrite');
  tx.objectStore('files').add({id:'legacy',name:'synthetic.txt',kind:'document',
    mime:'text/plain',size:100,addedAt:'2026-01-01',favorite:true,
    playlistIds:['legacy-list'],position:2,duration:100,
    blob:document.querySelector('input').files[0]});
  await new Promise((resolve,reject)=>{tx.oncomplete=resolve;tx.onabort=()=>reject(tx.error)});
  db.close();
},
  update: async () => {
  // Reproduce native File reserialization failure; metadata must not write the payload.
  const put=IDBObjectStore.prototype.put;
  IDBObjectStore.prototype.put=function(value,...args){
    if(value?.blob instanceof Blob)throw new DOMException('File rewrite denied','UnknownError');
    return put.call(this,value,...args);
  };
  const s=new LocalMediaStore();
  let row=(await s.list())[0];
  if(!row.favorite||row.position!==2||row.playlistIds[0]!=='legacy-list')throw Error('Legacy metadata lost');
  for(let i=0;i<30;i++){
    await Promise.all([s.update('legacy',{position:i,duration:100}),
      s.update('legacy',{favorite:false}),s.update('legacy',{playlistIds:['keep','remove']})]);
    await Promise.all([s.removePlaylist('remove'),s.update('legacy',{position:i+1})]);
    row=(await s.list())[0];
    if(row.position!==i+1||row.favorite||row.playlistIds.join()!=='keep')throw Error('Concurrent metadata lost');
  }
  if(await (await s.file('legacy')).text()!=='Synthetic media payload — проверка UTF-8')throw Error('Payload changed');
},
  reopen: async () => {
  const s=new LocalMediaStore();const row=(await s.list())[0];
  if(row.position!==30||row.duration!==100||row.favorite||row.playlistIds.join()!=='keep')throw Error('Restart lost state');
  if(await (await s.file(row.id)).text()!=='Synthetic media payload — проверка UTF-8')throw Error('Restart lost payload');
  await s.remove(row.id);let missing=false;
  try{await s.update(row.id,{favorite:true})}catch{missing=true}
  if(!missing||(await s.list()).length)throw Error('Deleted item resurrected');
  return {upgrade:true,concurrentChecks:60,restart:true,payloadPreserved:true,deletion:true};
},
};

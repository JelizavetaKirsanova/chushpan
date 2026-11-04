/** One in-flight write per device; retain only the latest pending slider values. */
export class DeviceWriter {
  private queued = new Map<string,{action:string;parameters:Record<string,unknown>}>();
  private active = new Map<string,string>();
  private timers = new Map<string,ReturnType<typeof setTimeout>>();
  private send:(value:object)=>void;
  private changed:()=>void;
  private delay:number;
  constructor(send:(value:object)=>void,changed:()=>void,delay=350) {this.send=send;this.changed=changed;this.delay=delay;}
  busy(id:string):boolean {return this.active.has(id);}
  offer(id:string,action:string,parameters:Record<string,unknown>,immediate=false):void {
    const previous=this.queued.get(id);
    const merged=action==='led'&&previous?.action==='led'&&parameters.power!==false&&previous.parameters.power!==false?{...previous.parameters,...parameters}:parameters;
    this.queued.set(id,{action,parameters:merged});
    clearTimeout(this.timers.get(id));
    if(immediate)this.flush(id);else this.timers.set(id,setTimeout(()=>this.flush(id),this.delay));
  }
  private flush(id:string):void {
    if(this.active.has(id))return;
    const item=this.queued.get(id);if(!item)return;this.queued.delete(id);
    const request_id=crypto.randomUUID();this.active.set(id,request_id);
    this.send({op:'act',id,...item,request_id});this.changed();
  }
  complete(request:string,ok:boolean):void {
    const id=[...this.active].find(([,key])=>key===request)?.[0];if(!id)return;
    this.active.delete(id);if(!ok)this.queued.delete(id);this.changed();this.flush(id);
  }
  disconnect():void {for(const t of this.timers.values())clearTimeout(t);this.timers.clear();this.queued.clear();this.active.clear();}
}

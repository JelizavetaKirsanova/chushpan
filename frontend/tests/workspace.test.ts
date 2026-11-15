import assert from "node:assert/strict";
import test from "node:test";
import {readFileSync} from "node:fs";
import {stripTypeScriptTypes} from "node:module";
import vm from "node:vm";
import {Window} from "happy-dom";
import { phaseLabels, healthLabels, seconds, RequestEvents, sessionCaption, recipientLabels } from "../src/workspace_state.ts";

test("every backend activity has a readable label", () => {
  for (const phase of ["idle", "recognizing", "processing", "switching", "generating", "error"]) {
    assert.ok(phaseLabels[phase]);
  }
});

test("late speech and waiting events cannot alter a newer request", () => {
  const events = new RequestEvents();
  assert.equal(events.accept({request_id: 2}), true);
  assert.equal(events.accept({request_id: 1}), false);
  assert.equal(events.accept({request_id: 3}), true);
  events.reset();
  assert.equal(events.accept({request_id: 1}), true);
  for (const phase of ["listening", "waiting", "speaking", "interrupted"]) assert.ok(phaseLabels[phase]);
});
test("model eligibility is not presented as verified availability", () => {
  assert.notEqual(healthLabels.unknown, healthLabels.ready);
  for (const status of ["open", "probing", "cancelled", "failed"]) assert.ok(healthLabels[status]);
});
test("zero latency and absent measurements stay distinct", () => {
  assert.equal(seconds(0), "0.00 с");
  assert.equal(seconds(null), "—");
  assert.equal(seconds(1.235), "1.24 с");
});

test("session captions distinguish follow-up listening from a mandatory wake word", () => {
  assert.equal(sessionCaption({active: true, listening: true, requires_wake: false,phase:'follow_up_listening'}),'Ждёт продолжения');
  assert.equal(sessionCaption({active: true, listening: false, requires_wake: false,phase:'processing'}),'Думает');
  assert.equal(sessionCaption({active: false, listening: false, requires_wake: true,phase:'idle'}),'Ожидает активации');
});

test("recipient states have plain labels and stale task updates are rejected", () => {
  for (const state of ["direct", "continuation", "waiting", "task", "task_running", "ambiguous"] as const) assert.ok(recipientLabels[state]);
  const events = new RequestEvents();
  assert.ok(events.accept({request_id: 4}));
  assert.equal(events.accept({request_id: 3}), false);
});

test("workspace routes back, persists sidebar state and compacts at the narrow breakpoint", () => {
  const window = new Window();
  window.document.write(readFileSync(new URL("../index.html", import.meta.url), "utf8")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<link\b[^>]*>/gi, ""));
  const code = stripTypeScriptTypes(readFileSync(new URL("../src/workspace.ts", import.meta.url), "utf8"), {mode:"transform"})
    .replace(/^import .*;\n/gm, "").replace("export class Workspace", "class Workspace") + "\nexports.Workspace=Workspace;";
  const listeners:((event:{matches:boolean})=>void)[] = [];
  Object.assign(window, {matchMedia:()=>({matches:false,addEventListener:(_type:string,callback:(event:{matches:boolean})=>void)=>listeners.push(callback)})});
  const sandbox = {exports:{} as Record<string,any>, document:window.document, window, location:window.location,
    history:window.history, sessionStorage:window.sessionStorage, localStorage:window.localStorage,
    CustomEvent:window.CustomEvent, Event:window.Event};
  vm.runInNewContext(code, sandbox);
  try {
    const workspace = new sandbox.exports.Workspace(()=>{},()=>{},()=>{},()=>{});
    workspace.navigate("settings");workspace.navigate("devices");
    const back = window.document.querySelector<HTMLButtonElement>("#route-back")!;
    assert.equal(back.hidden,false);
    back.click();assert.equal(window.document.querySelector<HTMLElement>(".hud")!.dataset.view,"settings");
    window.document.querySelector<HTMLButtonElement>("#sidebar-toggle")!.click();
    assert.equal(window.document.querySelector<HTMLElement>(".hud")!.dataset.sidebar,"collapsed");
    assert.equal(window.localStorage.getItem("chushpan:sidebar"),"collapsed");
    listeners[0]({matches:true});
    assert.equal(window.document.querySelector<HTMLButtonElement>("#sidebar-toggle")!.disabled,true);
    assert.equal(window.document.querySelector<HTMLElement>(".hud")!.dataset.sidebar,"collapsed");
    assert.equal(window.document.querySelector("#news"),null);
  } finally { window.happyDOM.abort(); }
});

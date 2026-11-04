import {microphonePresentation, requestMicrophone, dialogueCaption, type MicrophoneApi} from './voice_activation';
import {VoiceSettingsPanel} from './voice_settings';
import {AutomationsView} from './automations';
import {DevicesView} from './devices';
import './devices.css';
import "./style.css";
import "./workspace.css";
import './dashboard.css';
import './simple_workspace.css';
import './scenario_editor.css';
import './settings_center.css';
import {SettingsCenterUI} from './settings_center';
import { Workspace } from "./workspace";
import { PerformancePanel } from './performance';

import type { Mode } from "./core";
import { Character } from './character';
import { DashboardView } from './dashboard';
import { fill, stringsFor } from "./i18n";
import { Panels } from "./panels";
import { Link } from "./socket";
import type { AutostartEvent, BootEvent, ExportedEvent, Hello, HudEvent } from "./types";
import { RequestEvents } from "./workspace_state";

const requestEvents = new RequestEvents();

const hud = document.querySelector<HTMLElement>(".hud")!;
const byId = (id: string) => document.getElementById(id)!;

let t = stringsFor(document.documentElement.lang);
let hello: Hello | null = null;
let mode: Mode = "offline";
let microphone = "unknown";
let dialogue:Extract<HudEvent,{type:"session"}>|null=null;
let toolTimer = 0;
const performancePanel = new PerformancePanel((days, filters) => link.performance(days, filters), id => link.trace(id));
const workspace = new Workspace(id => link.history(id), value => link.dialogue(value), () => link.send("sleep"), value => link.music(value), id => id ? performancePanel.open(id) : performancePanel.refresh());
let latestTrace: string | null = null;
byId('open-performance').addEventListener('click', () => { if (latestTrace) workspace.openTrace(latestTrace); });

const panels = new Panels(t);
panels.setStrings(t);

function tap(): void {
  if (microphone === 'starting') return;
  if (microphone === 'stopped' && !['offline', 'booting', 'thinking', 'speaking'].includes(mode)) {
    const api=(window as unknown as {pywebview?:{api?:MicrophoneApi}}).pywebview?.api;
    void requestMicrophone(api,()=>{microphone='starting';setMode(mode);},message=>{microphone='stopped';setMode(mode);note(message,true);});
    return;
  }
  if (mode === "sleeping") link.send("wake");
  else if (mode === "listening" || mode === "thinking" || mode === "speaking") link.send("sleep");
}

const voiceSettings = new VoiceSettingsPanel(value => link.voice(value));
const core = new Character(byId('character'), tap);
const dashboard = new DashboardView(value => link.dashboard(value), value => link.chat(value), view => workspace.navigate(view), value => link.historySearch(value), core, value => panels.weather(value));
const automations = new AutomationsView(value => link.automations(value), view => workspace.navigate(view), () => link.send('wake'), text => link.chat({text}));
const devices = new DevicesView(value=>link.devices(value));
const settingsCenter = new SettingsCenterUI(value => link.settings(value), value => link.automations(value), view => workspace.navigate(view));

function setMode(next: Mode): void {
  mode = next;
  (byId("stop-response") as HTMLButtonElement).disabled = !["thinking", "speaking"].includes(next);
  hud.dataset.state = next;
  hud.dataset.activity = next;
  core.setMode(next);
  const mic = microphonePresentation(next, microphone);
  const label = mic?.label ?? dialogueCaption(next,dialogue) ?? t.states[next];
  byId("state-label").textContent = label;
  byId("caption-state").textContent = label;
  const name = hello?.name ?? "Чушпан";
  let hint = "";
  if (next === "sleeping") {
    const phrase = hello?.wake_phrase ?? (hello ? `Hey ${name}` : "эй Чушпан");
    hint = hello?.wake_word === false ? "" : fill(t.hints.sleeping, { phrase });
  }
  else if (next === "listening") hint = t.hints.listening;
  else if (next === "offline") hint = t.hints.offline;
  byId("caption-hint").textContent = mic?.hint ?? hint;
  if (next !== "thinking") showTool(null);
  const micLabel = mic?.button ?? (next === "sleeping" ? "Начать разговор" : next === "offline" ? "Нет соединения" : "Завершить разговор");
  byId('listen-control-label').textContent = micLabel;
  byId('listen-control').setAttribute('aria-label', micLabel);
  (byId("listen-control") as HTMLButtonElement).disabled = next === "offline" || next === "booting" || microphone === "starting";
}

function showTool(name: string | null): void {
  const box = byId("tool");
  clearTimeout(toolTimer);
  if (!name) {
    box.classList.remove("on");
    return;
  }
  box.textContent = `⟳ ${t.tools[name] ?? name}`;
  box.classList.add("on");
}

// The loading screen: shown while the backend reports a startup below 100%.
const GLYPH = { pending: "·", running: "⟳", done: "✔", skipped: "–", offline: "⚠", failed: "✖" };
let boot: BootEvent | null = null;
let bootHide = 0;

function showBoot(e: BootEvent | null = boot): void {
  boot = e;
  const box = byId("boot");
  if (!e) return;
  byId("boot-title").textContent = t.boot.title;
  byId("boot-percent").textContent = String(e.progress);
  byId("boot-fill").style.width = `${e.progress}%`;
  box.querySelector(".boot-bar")!.setAttribute("aria-valuenow", String(e.progress));
  byId("boot-steps").replaceChildren(
    ...e.steps.map(({ id, status }) => {
      const row = document.createElement("li");
      row.className = status;
      const note = { offline: t.boot.offline, failed: t.boot.failed, skipped: t.boot.skipped }[
        status as "offline" | "failed" | "skipped"
      ];
      const name = t.boot.steps[id] ?? id;
      row.textContent = `${GLYPH[status]}  ${note ? `${name} · ${note}` : name}`;
      return row;
    }),
  );
  clearTimeout(bootHide);
  if (e.progress >= 100) {
    box.classList.add("leaving"); // fades out, then hides
    bootHide = window.setTimeout(() => (box.hidden = true), 700);
  } else {
    box.hidden = false;
    box.classList.remove("leaving");
  }
}

const exportButton = byId("export") as HTMLButtonElement;
let noteTimer = 0;

function note(text: string, error = false): void {
  const box = byId("talk-note");
  box.textContent = text;
  box.classList.toggle("error", error);
  clearTimeout(noteTimer);
  noteTimer = window.setTimeout(() => (box.textContent = ""), 6000);
}

function exported(e: ExportedEvent): void {
  exportButton.disabled = false;
  if (e.error) note(fill(t.exportFailed, { error: e.error }), true);
  else note(fill(t.exported, { name: e.name ?? "" }));
}

exportButton.addEventListener("click", () => {
  exportButton.disabled = true; // until the backend says where it saved it
  link.send("export");
  window.setTimeout(() => (exportButton.disabled = false), 5000);
});

const autostartButton = byId("autostart") as HTMLButtonElement;

function showAutostart(e: AutostartEvent): void {
  autostartButton.hidden = false;
  autostartButton.disabled = false;
  autostartButton.setAttribute("aria-checked", String(e.enabled));
  autostartButton.title = e.error ?? "";
  byId("autostart-label").textContent = t.autostart;
}

autostartButton.addEventListener("click", () => {
  const on = autostartButton.getAttribute("aria-checked") !== "true";
  autostartButton.disabled = true; // until the backend confirms the new state
  link.set("autostart", on);
});

function greet(h: Hello): void {
  hello = h;
  t = { ...stringsFor(h.language), jarvisMemory: h.name };
  document.documentElement.lang = h.language;
  panels.setStrings(t);
  byId("name").textContent = h.name.toUpperCase();
  byId("model").textContent = h.model;
  document.title = fill(t.windowTitle, { name: h.name });
  byId("core").setAttribute("aria-label", t.coreLabel);
  byId("autostart-label").textContent = t.autostart;
  exportButton.textContent = `⇩ ${t.exportChat}`;
  showBoot();
  byId("owner").textContent = [h.owner, h.place].filter(Boolean).join(" · ");
  setMode(mode);
  tickClock();
}

function onEvent(e: HudEvent): void {
  if (e.type === "hello") requestEvents.reset();
  if (!requestEvents.accept(e)) return;
  if(e.type === "voice_settings") microphone=e.microphone;
  if(e.type === "session") dialogue=e;
  workspace.receive(e);
  performancePanel.receive(e);
  dashboard.receive(e);
  voiceSettings.receive(e);
  automations.receive(e);
  devices.receive(e);
  settingsCenter.receive(e);
  switch (e.type) {
    case "voice_recognition": return;
    case "voice_settings": case "session": return setMode(mode);
    case "notice":
      hud.dataset.activity = "error";
      return note(e.message, e.error);
    case "ai_status": case "settings_info": case "history": case "activity": case "dialogue_settings_result": case "addressee": case "music_settings": return;
    case "hello":
      return greet(e);
    case "state":
      return setMode(e.state);
    case "level":
      if (e.mic !== undefined && (mode === "listening" || mode === "sleeping")) core.feed(e.mic);
      if (e.out !== undefined && mode === "speaking") core.feed(e.out);
      return;
    case "weather":
      if (!dashboard.hasLocation) panels.weather(e);
      return;
    case "news":
      return panels.news(e);
    case "system":
      return panels.system(e);
    case "timers":
      return panels.setTimers(e.items);
    case "stats":
      latestTrace = e.trace_id ?? null;
      (byId('open-performance') as HTMLButtonElement).disabled = latestTrace === null;
      return panels.stats(e);
    case "autostart":
      return showAutostart(e);
    case "footprint":
      return panels.footprint(e);
    case "boot":
      return showBoot(e);
    case "exported":
      return exported(e);
    case "transcript":
      return panels.setTranscript(e.lines);
    case "user":
      return panels.user(e.text);
    case "reply":
      return panels.reply(e.delta, e.duration_s);
    case "reply_end":
      return panels.replyEnd(e.cut);
    case "tool":
      return showTool(e.name);
  }
}

const link = new Link(onEvent, (online) => {
  workspace.connection(online);
  performancePanel.connection(online);
  dashboard.connection(online);
  voiceSettings.connection(online);
  automations.connection(online);
  devices.connection(online);
  settingsCenter.connection(online);
  if (online) link.voiceStatus();
  exportButton.disabled = !online;
  if (!online) {
    microphone="unknown";
    setMode("offline");
    autostartButton.hidden = true;
  }
});
byId("stop-response").addEventListener("click", () => link.send("stop"));
byId("listen-control").addEventListener("click", tap);

function tickClock(): void {
  const now = new Date();
  const lang = hello?.language ?? document.documentElement.lang;
  byId("time").textContent = now.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" });
  byId("date").textContent = now.toLocaleDateString(lang, {
    weekday: "long",
    day: "numeric",
    month: "long",
  });
  const hour = now.getHours();
  byId('greeting').textContent = `${hour < 5 ? 'Тихой ночи' : hour < 12 ? 'Доброе утро' : hour < 18 ? 'Добрый день' : 'Добрый вечер'} ♡`;
}

// In the native window (pywebview) WebKit's Fullscreen API is off: the window does it.
type PyWebView = { api?: { toggle_fullscreen?: () => Promise<void> } };

function toggleFullscreen(): void {
  const native = (window as unknown as { pywebview?: PyWebView }).pywebview?.api;
  if (native?.toggle_fullscreen) void native.toggle_fullscreen();
  else if (document.fullscreenElement) void document.exitFullscreen();
  else void document.documentElement.requestFullscreen();
}

document.addEventListener("keydown", (e) => {
  if (e.target instanceof HTMLElement && (e.target.closest('input, textarea, select, button, [contenteditable="true"]') || document.querySelector('dialog[open]'))) return;
  if (e.code === "Space") {
    e.preventDefault();
    tap();
  } else if (e.key === "f" || e.key === "F") {
    toggleFullscreen();
  }
});

exportButton.textContent = `⇩ ${t.exportChat}`;
setMode("offline");
tickClock();
setInterval(tickClock, 1000);
core.start();
link.connect();

(window as unknown as {chushpanNavigate:(view:string)=>void}).chushpanNavigate=(view)=>{workspace.navigate(view==='application'?'settings':view);if(view==='application')settingsCenter.open('application');};

(window as unknown as {chushpanNativeNotice:(message:string)=>void}).chushpanNativeNotice=(message)=>note(message,true);

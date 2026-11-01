import "./style.css";
import "./workspace.css";
import { Workspace } from "./workspace";

import { Core, type Mode } from "./core";
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
let toolTimer = 0;
const workspace = new Workspace(id => link.history(id), value => link.dialogue(value), () => link.send("sleep"), value => link.music(value));

const panels = new Panels(t);
panels.setStrings(t);

function tap(): void {
  if (mode === "sleeping") link.send("wake");
  else if (mode === "listening" || mode === "thinking" || mode === "speaking") link.send("sleep");
}

const core = new Core(byId("core") as HTMLCanvasElement, tap);

function setMode(next: Mode): void {
  mode = next;
  hud.dataset.state = next;
  hud.dataset.activity = next;
  core.setMode(next);
  const label = t.states[next];
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
  byId("caption-hint").textContent = hint;
  if (next !== "thinking") showTool(null);
  byId("listen-control").textContent = next === "sleeping" ? "Начать разговор" : next === "offline" ? "Нет соединения" : "Завершить разговор";
  (byId("listen-control") as HTMLButtonElement).disabled = next === "offline" || next === "booting";
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
  toolTimer = window.setTimeout(() => box.classList.remove("on"), 6000);
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
  workspace.receive(e);
  switch (e.type) {
    case "notice":
      hud.dataset.activity = "error";
      return note(e.message, e.error);
    case "ai_status": case "settings_info": case "history": case "activity": case "session": case "dialogue_settings_result": case "addressee": case "music_settings": return;
    case "hello":
      return greet(e);
    case "state":
      return setMode(e.state);
    case "level":
      if (e.mic !== undefined && (mode === "listening" || mode === "sleeping")) core.feed(e.mic);
      if (e.out !== undefined && mode === "speaking") core.feed(e.out);
      return;
    case "weather":
      return panels.weather(e);
    case "news":
      return panels.news(e);
    case "system":
      return panels.system(e);
    case "timers":
      return panels.setTimers(e.items);
    case "stats":
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
  exportButton.disabled = !online;
  if (!online) {
    setMode("offline");
    autostartButton.hidden = true;
  }
});
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
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLButtonElement) return;
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

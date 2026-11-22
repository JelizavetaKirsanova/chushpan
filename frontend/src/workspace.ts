import type { HudEvent } from "./types";
import { phaseLabels, healthLabels, seconds, sessionCaption, recipientLabels } from "./workspace_state";

const node = (id: string) => document.getElementById(id)!;
function text(tag: string, value: string, className = ""): HTMLElement {
  const element = document.createElement(tag); element.textContent = value; element.className = className;
  return element;
}
function selectDuration(value:number):void {
  const select=node("dialogue-duration") as HTMLSelectElement;
  const selected=String(value);
  if(![...select.options].some(option=>option.value===selected)){
    const option=document.createElement("option");option.value=selected;option.textContent=`Сохранённое значение: ${value} с`;select.append(option);
  }
  select.value=selected;
}

export class Workspace {
  private selected = 0;
  private online = false;
  private hasSession = false;
  private sessionActive = false;
  private saving = false;
  private hasMusic = false;
  private currentView = "home";
  private routeHistory: string[] = [];
  private sidebarCompact = false;
  constructor(private readonly requestHistory: (id: number) => void,
    private readonly saveDialogue: (value: {enabled: boolean; duration_s: number; nameless_interruptions: boolean; addressee_strictness: "contextual" | "addressed"}) => void,
    private readonly closeSession: () => void,
    private readonly saveMusic: (value: string) => void,
    private readonly performance?: (id?: string) => void) {
    node("music-settings").addEventListener("submit", event => {
      event.preventDefault();
      if (!this.online || !this.hasMusic) return;
      node("music-feedback").textContent = "Сохраняю…";
      this.saveMusic((node("music-provider") as HTMLSelectElement).value);
    });
    node("dialogue-settings").addEventListener("submit", event => {
      event.preventDefault();
      if (!this.online || !this.hasSession) return;
      const duration = node("dialogue-duration") as HTMLSelectElement;
      if (!duration.reportValidity()) return;
      this.saving = true;
      node("dialogue-feedback").textContent = "Применяю…";
      this.saveDialogue({
        enabled: (node("dialogue-enabled") as HTMLInputElement).checked,
        duration_s: Number(duration.value),
        nameless_interruptions: (node("dialogue-nameless") as HTMLInputElement).checked,
        addressee_strictness: (node("dialogue-addressee") as HTMLSelectElement).value as "contextual" | "addressed",
      });
    });
    node("dialogue-close").addEventListener("click", () => { if (this.online) this.closeSession(); });
    const routeNames = new Set(["home", "history", "calendar", "automations", "devices", "lighting", "cinema", "memory", "notes", "mac-control", "my-devices", "analytics", "performance", "settings", "privacy", "diagnostics"]);
    const savedRoute = window.location.hash.slice(1);
    const initialView = routeNames.has(savedRoute) ? savedRoute : "home";
    const hud = document.querySelector<HTMLElement>(".hud")!;
    const moreSummary = document.querySelector<HTMLElement>('.nav-more > summary');
    if (moreSummary) moreSummary.setAttribute('aria-label', 'Ещё разделы');
    hud.dataset.view = initialView;
    this.currentView = initialView;
    try {
      const saved = JSON.parse(sessionStorage.getItem("chushpan:navigation") ?? "null") as {current?:string;history?:string[]}|null;
      if (saved?.current === initialView && Array.isArray(saved.history)) this.routeHistory = saved.history.filter(view => routeNames.has(view)).slice(-30);
    } catch { /* A private or unavailable browser store only affects back history. */ }
    const compact = window.matchMedia?.("(max-width: 1180px)") ?? {matches:false,addEventListener:()=>{}};
    this.sidebarCompact = compact.matches;
    let savedSidebar = false;
    try { savedSidebar = localStorage.getItem("chushpan:sidebar") === "collapsed"; } catch { /* Optional preference. */ }
    this.applySidebar(this.sidebarCompact || savedSidebar);
    compact.addEventListener("change", event => {
      this.sidebarCompact = event.matches;
      let userCollapsed = false;
      try { userCollapsed = localStorage.getItem("chushpan:sidebar") === "collapsed"; } catch { /* Optional preference. */ }
      this.applySidebar(this.sidebarCompact || userCollapsed);
    });
    node("sidebar-toggle").addEventListener("click", () => {
      if (this.sidebarCompact) return;
      const collapsed = hud.dataset.sidebar !== "collapsed";
      try { localStorage.setItem("chushpan:sidebar", collapsed ? "collapsed" : "expanded"); } catch { /* Current window still updates. */ }
      this.applySidebar(collapsed);
    });
    node("route-back").addEventListener("click", () => this.goBack());
    this.navigate(initialView);
    document.querySelectorAll<HTMLButtonElement>(".workspace-nav button[data-view]").forEach(button => {
      button.addEventListener("click", () => this.navigate(button.dataset.view!));
    });
    document.addEventListener("keydown", event => {
      if (document.querySelector('dialog[open]')) return;
      if (event.metaKey || event.ctrlKey) {
        const view = {"1": "home", "2": "history", ",": "settings"}[event.key];
        if (view) { event.preventDefault(); this.navigate(view); }
      } else if (event.key === "Escape") this.goBack();
    });
  }
  navigate(view: string, fromHistory = false): boolean {
    view = ({'smart-home': 'devices', permissions: 'settings', commands: 'automations', training: 'automations', day: 'home'} as Record<string, string>)[view] ?? view;
    if (!document.dispatchEvent(new CustomEvent("workspace:navigate", {detail: view, cancelable: true}))) return false;
    if (view !== this.currentView) {
      if (fromHistory) this.routeHistory.pop();
      else this.routeHistory.push(this.currentView);
      this.routeHistory = this.routeHistory.slice(-30);
      this.currentView = view;
      document.querySelector<HTMLElement>(".hud")!.dataset.view = view;
      try {
        history.replaceState({chushpanView:view}, "", `#${encodeURIComponent(view)}`);
        sessionStorage.setItem("chushpan:navigation", JSON.stringify({current:view,history:this.routeHistory}));
      } catch { /* Routing still works when browser storage is unavailable. */ }
    }
    const parent = ({day: 'home', notes: 'memory', privacy: 'settings', diagnostics: 'performance'} as Record<string, string>)[view] ?? view;
    document.querySelectorAll<HTMLButtonElement>(".workspace-nav button[data-view], .chat-navigation button[data-go]").forEach(button => {
      const selected = button.closest(".workspace-nav")
        ? button.dataset.view === parent
        : button.dataset.go === view;
      if (selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    if (view === "history") {
      if (this.online) this.requestHistory(this.selected);
      else node("history-messages").replaceChildren(text("p", "Для просмотра истории запусти Чушпана. Соединение с backend отсутствует.", "empty-state"));
    }
    const more = document.querySelector<HTMLDetailsElement>('.nav-more');
    if (more && ['memory', 'mac-control', 'my-devices', 'analytics', 'performance', 'settings', 'privacy', 'diagnostics'].includes(view)) more.open = true;
    if (view === 'diagnostics') {
      const diagnostics = document.querySelector<HTMLDetailsElement>('.diagnostics-inline');
      if (diagnostics) diagnostics.open = true;
    }
    if (view === "performance") this.performance?.();
    this.updateBackButton();
    return true;
  }
  private goBack(): void {
    const previous = this.routeHistory.at(-1);
    if (previous) this.navigate(previous, true);
  }
  private applySidebar(collapsed: boolean): void {
    const hud = document.querySelector<HTMLElement>(".hud")!;
    hud.dataset.sidebar = collapsed ? "collapsed" : "expanded";
    const button = node("sidebar-toggle") as HTMLButtonElement;
    const expanded = !collapsed;
    button.setAttribute("aria-expanded", String(expanded));
    button.setAttribute("aria-label", expanded ? "Свернуть боковую панель" : "Развернуть боковую панель");
    button.title = this.sidebarCompact
      ? "Боковая панель сворачивается автоматически в узком окне"
      : expanded ? "Свернуть боковую панель" : "Развернуть боковую панель";
    button.disabled = this.sidebarCompact;
    const label = button.querySelector<HTMLSpanElement>("span:not([data-icon])");
    if (label) label.textContent = expanded ? "Свернуть меню" : "Развернуть меню";
  }
  private updateBackButton(): void {
    const button = node("route-back") as HTMLButtonElement;
    button.hidden = this.currentView === "home" || this.routeHistory.length === 0;
  }
  openTrace(id: string): void { this.navigate('performance'); this.performance?.(id); }
  connection(online: boolean): void {
    this.online = online;
    document.querySelectorAll<HTMLSelectElement | HTMLButtonElement>("#music-settings select, #music-settings button")
      .forEach(control => control.disabled = !online || !this.hasMusic);
    document.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement>("#dialogue-settings input, #dialogue-settings button, #dialogue-settings select")
      .forEach(control => control.disabled = !online || !this.hasSession);
    (node("dialogue-close") as HTMLButtonElement).disabled = !online || !this.sessionActive;
    if (!online) {
      node("session-status").hidden = true;
      node("recipient-status").textContent = "Нет соединения";
      node("dialogue-feedback").textContent = this.saving ? "Соединение прервано; применение не подтверждено." : "";
      this.saving = false;
      node("ai-provider").textContent = "НЕТ СОЕДИНЕНИЯ";
      node("ai-reason").textContent = "Backend недоступен. Последние показатели не подтверждают текущее подключение.";
    } else if (document.querySelector<HTMLElement>(".hud")!.dataset.view === "history") this.requestHistory(this.selected);
  }
  receive(event: HudEvent): void {
    if (event.type === "music_settings") {
      this.hasMusic = true;
      (node("music-provider") as HTMLSelectElement).value = event.saved ?? "unselected";
      node("music-feedback").textContent = event.error ?? (event.restart_required
        ? "Выбор сохранён. Перезапусти Чушпана, чтобы применить его."
        : `Текущий сервис: ${event.current === "spotify" ? "Spotify" : event.current === "youtube_music" ? "YouTube Music" : "не выбран"}.`);
      document.querySelectorAll<HTMLSelectElement | HTMLButtonElement>("#music-settings select, #music-settings button")
        .forEach(control => control.disabled = !this.online);
    } else if (event.type === "session") {
      this.hasSession = true;
      this.sessionActive = event.active;
      node("session-status").hidden = !event.active;
      node("session-status").textContent = sessionCaption(event);
      if (!event.active) node("recipient-status").textContent = recipientLabels.waiting;
      node("session-interrupted").hidden = !event.interrupted;
      node("dialogue-reason").textContent = event.reason;
      if (this.saving || document.activeElement?.closest("#dialogue-settings") == null) {
        (node("dialogue-enabled") as HTMLInputElement).checked = event.enabled;
        selectDuration(event.duration_s);
        (node("dialogue-nameless") as HTMLInputElement).checked = event.nameless_interruptions;
        (node("dialogue-addressee") as HTMLSelectElement).value = event.addressee_strictness;
      }
      document.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement>("#dialogue-settings input, #dialogue-settings button, #dialogue-settings select")
        .forEach(control => control.disabled = !this.online);
      (node("dialogue-close") as HTMLButtonElement).disabled = !this.online || !event.active;
    } else if (event.type === "addressee") {
      node("recipient-status").textContent = recipientLabels[event.state];
    } else if (event.type === "dialogue_settings_result") {
      this.saving = false;
      node("dialogue-feedback").textContent = event.ok
        ? "Параметры применены для текущего запуска." : "Настройки не применены. Проверь допустимые значения.";
    } else if (event.type === "settings_info") {
      node("settings-items").replaceChildren(...Object.entries(event.items).map(([key, value]) => {
        const row = text("div", ""); row.append(text("dt", key), text("dd", value)); return row;
      }));
    } else if (event.type === "ai_status") {
      node("ai-provider").textContent = event.provider?.toUpperCase() ?? "Подключение";
      node("ai-model").textContent = event.model ?? "Выбор по приоритету";
      node("ai-reason").textContent = [phaseLabels[event.phase] ?? event.phase, event.reason].filter(Boolean).join(" · ");
      if (event.phase === "switching" || event.phase === "generating") this.activity(event.phase);
      node("ai-models").replaceChildren(...event.models.map(model => {
        const row = text("div", "", "model-row");
        row.append(text("span", model.model), text("small", `${healthLabels[model.status] ?? model.status}${model.retry_in_s ? ` · пауза ${Math.ceil(model.retry_in_s)} с при последней проверке` : ""}`));
        row.dataset.health = model.status;
        row.title = `${model.successes} успешных · ${model.failures} ошибок · ${seconds(model.latency_s)}`;
        return row;
      }));
    } else if (event.type === "openai_budget") {
      if (event.warning && event.message) {
        node("ai-reason").textContent = event.message;
        node("ai-reason").dataset.activity = "warning";
      } else if (node("ai-reason").dataset.activity === "warning") {
        delete node("ai-reason").dataset.activity;
        node("ai-reason").textContent = "Месячный лимит расходов OpenAI не достигнут.";
      }
    } else if (event.type === "stats") {
      node("summary-response").textContent = seconds(event.response_s ?? event.voice_s);
      node("summary-model").textContent = event.model_used === false ? "Локально" : seconds(event.stages?.model_total ?? event.first_token_s);
      if (event.model_used === false) {
        node("ai-model").textContent = "Локальная команда";
        node("ai-provider").textContent = "НА MAC";
        node("ai-reason").textContent = "Этот запрос выполнен без обращения к AI-модели.";
      }
    } else if (event.type === "activity") this.activity(event.phase);
    else if (event.type === "tool") this.activity("command");
    else if (event.type === "hello") {
      node("ai-model").textContent = event.model;
      node("ai-provider").textContent = event.model.startsWith("Ollama") ? "OLLAMA" : "GEMINI";
      node("ai-reason").textContent = "Настроенный провайдер. Доступность проверяется при запросе.";
    }
    else if (event.type === "history") {
      this.selected = event.selected;
      node("conversation-list").replaceChildren(...event.conversations.map(conversation => {
        const row = text('div', '', 'conversation-row');
        const button = text("button", `${conversation.pinned ? '⌁ ' : ''}${conversation.title || conversation.preview || `Разговор ${conversation.id}`}`, "conversation") as HTMLButtonElement;
        button.append(text("small", `${new Date(conversation.started_at).toLocaleString()} · ${conversation.messages} сообщений`));
        button.setAttribute("aria-pressed", String(conversation.id === event.selected));
        button.addEventListener("click", () => this.requestHistory(conversation.id));
        row.append(button);
        const actions = text('div', '', 'conversation-actions');
        for (const [action, label] of conversation.deleted_at ? [['restore', 'Восстановить']] : [['rename', 'Название'], ['pin', conversation.pinned ? 'Открепить' : 'Закрепить'], ['delete', 'В корзину']]) {
          const control = text('button', label) as HTMLButtonElement;
          control.dataset.conversationAction = action; control.dataset.id = String(conversation.id); control.dataset.title = conversation.title || conversation.preview || '';
          actions.append(control);
        }
        row.append(actions); return row;
      }));
      if (!event.conversations.length) node("conversation-list").append(text("p", "Здесь появятся сохранённые разговоры.", "empty-state"));
      node("history-messages").replaceChildren(...event.messages.map(message => {
        const row = text("article", "", `history-message ${message.role}`);
        row.append(text("small", `${message.role === "user" ? "Ты" : "Чушпан"} · ${new Date(message.created_at).toLocaleTimeString()}`), text("p", message.content));
        if (message.role === 'assistant' && message.request_id && message.has_trace) {
          const button = text('button', 'Диагностика ответа', 'trace-link');
          button.onclick = () => this.openTrace(message.request_id!); row.append(button);
        }
        return row;
      }));
      if (!event.messages.length) node("history-messages").append(text("p", event.error ?? "Выбери разговор слева. Показаны последние 50 диалогов; до 200 сообщений в каждом.", "empty-state"));
    }
  }
  private activity(phase: string): void {
    node("caption-state").textContent = phaseLabels[phase] ?? phase;
    document.querySelector<HTMLElement>(".hud")!.dataset.activity = phase;
  }
}

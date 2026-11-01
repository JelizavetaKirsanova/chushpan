import type { HudEvent } from "./types";
import { phaseLabels, healthLabels, seconds, sessionCaption, recipientLabels } from "./workspace_state";

const node = (id: string) => document.getElementById(id)!;
function text(tag: string, value: string, className = ""): HTMLElement {
  const element = document.createElement(tag); element.textContent = value; element.className = className;
  return element;
}

export class Workspace {
  private selected = 0;
  private online = false;
  private hasSession = false;
  private sessionActive = false;
  private saving = false;
  private hasMusic = false;
  constructor(private readonly requestHistory: (id: number) => void,
    private readonly saveDialogue: (value: {enabled: boolean; duration_s: number; nameless_interruptions: boolean; addressee_strictness: "contextual" | "addressed"}) => void,
    private readonly closeSession: () => void,
    private readonly saveMusic: (value: string) => void) {
    node("music-settings").addEventListener("submit", event => {
      event.preventDefault();
      if (!this.online || !this.hasMusic) return;
      node("music-feedback").textContent = "Сохраняю…";
      this.saveMusic((node("music-provider") as HTMLSelectElement).value);
    });
    node("dialogue-settings").addEventListener("submit", event => {
      event.preventDefault();
      if (!this.online || !this.hasSession) return;
      const duration = node("dialogue-duration") as HTMLInputElement;
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
    document.querySelectorAll<HTMLButtonElement>(".workspace-nav button").forEach(button => {
      button.addEventListener("click", () => this.navigate(button.dataset.view!));
    });
    document.addEventListener("keydown", event => {
      if (event.metaKey || event.ctrlKey) {
        const view = {"1": "home", "2": "history", ",": "settings"}[event.key];
        if (view) { event.preventDefault(); this.navigate(view); }
      } else if (event.key === "Escape") this.navigate("home");
    });
  }
  navigate(view: string): void {
    document.querySelector<HTMLElement>(".hud")!.dataset.view = view;
    document.querySelectorAll<HTMLButtonElement>(".workspace-nav button").forEach(button => {
      if (button.dataset.view === view) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    if (view === "history") {
      if (this.online) this.requestHistory(this.selected);
      else node("history-messages").replaceChildren(text("p", "Для просмотра истории запусти Чушпана. Соединение с backend отсутствует.", "empty-state"));
    }
  }
  connection(online: boolean): void {
    this.online = online;
    document.querySelectorAll<HTMLSelectElement | HTMLButtonElement>("#music-settings select, #music-settings button")
      .forEach(control => control.disabled = !online || !this.hasMusic);
    document.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement>("#dialogue-settings input, #dialogue-settings button, #dialogue-settings select")
      .forEach(control => control.disabled = !online || !this.hasSession);
    (node("dialogue-close") as HTMLButtonElement).disabled = !online || !this.sessionActive;
    if (!online) {
      node("session-status").textContent = "Нет соединения с голосовым ассистентом";
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
      node("session-status").textContent = sessionCaption(event);
      if (!event.active) node("recipient-status").textContent = recipientLabels.waiting;
      node("session-interrupted").hidden = !event.interrupted;
      node("dialogue-reason").textContent = event.reason;
      if (this.saving || document.activeElement?.closest("#dialogue-settings") == null) {
        (node("dialogue-enabled") as HTMLInputElement).checked = event.enabled;
        (node("dialogue-duration") as HTMLInputElement).value = String(event.duration_s);
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
        const button = text("button", `Разговор ${conversation.id}`, "conversation") as HTMLButtonElement;
        button.append(text("small", `${new Date(conversation.started_at).toLocaleString()} · ${conversation.messages} сообщений`));
        button.setAttribute("aria-pressed", String(conversation.id === event.selected));
        button.addEventListener("click", () => this.requestHistory(conversation.id)); return button;
      }));
      if (!event.conversations.length) node("conversation-list").append(text("p", "Здесь появятся сохранённые разговоры.", "empty-state"));
      node("history-messages").replaceChildren(...event.messages.map(message => {
        const row = text("article", "", `history-message ${message.role}`);
        row.append(text("small", `${message.role === "user" ? "Ты" : "Чушпан"} · ${new Date(message.created_at).toLocaleTimeString()}`), text("p", message.content)); return row;
      }));
      if (!event.messages.length) node("history-messages").append(text("p", event.error ?? "Выбери разговор слева. Показаны последние 50 диалогов; до 200 сообщений в каждом.", "empty-state"));
    }
  }
  private activity(phase: string): void {
    node("caption-state").textContent = phaseLabels[phase] ?? phase;
    document.querySelector<HTMLElement>(".hud")!.dataset.activity = phase;
  }
}

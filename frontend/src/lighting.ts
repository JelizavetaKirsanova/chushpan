import type { HudEvent } from "./types";
import type { DeviceRecord, DevicesEvent } from "./devices";
import { ColorEditor, colorDraft } from "./color_editor";
import type { ColorDraft, UserColor } from "./color_editor";

type Action = {
  kind?: string;
  color?: string | null;
  brightness?: number | null;
  power?: boolean | null;
  preset_id?: string | null;
  duration_s?: number;
  effect?: string;
  period_s?: number;
  colors?: string[];
};
type Preset = { id: string; revision: number; name: string; action: Action };
type Rule = {
  kind: string;
  at?: string;
  date?: string | null;
  weekdays?: number[];
  timezone?: string;
  interval_s?: number;
  latitude?: number | null;
  longitude?: number | null;
  offset_minutes?: number;
};
type Job = {
  id: string;
  revision: number;
  name: string;
  targets: string[];
  action: Action;
  rule: Rule;
  next_at: string;
  enabled: boolean;
  place_id: string;
  conditions: Record<string, boolean>;
  catch_up_s: number;
  status: string;
  progress?: object;
};
type LightData = {
  presets: Preset[];
  jobs: Job[];
  flags: Record<string, boolean>;
  journal: { at: string; status: string; message: string }[];
  notice: string;
};
type LightEvent = DevicesEvent & {
  lighting?: LightData;
  result?: DevicesEvent["result"] & { palette?: object };
};
const el = <T extends keyof HTMLElementTagNameMap>(
  tag: T,
  text = "",
  className = "",
): HTMLElementTagNameMap[T] => {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
};
const outcomes: Record<string, string> = {
  pending: "Ожидает",
  active: "Выполняется",
  running: "Отправка",
  paused: "Пауза",
  success: "Подтверждено",
  unverified: "Состояние не подтверждено",
  failed: "Ошибка",
  missed: "Пропущено",
  conflict: "Конфликт",
  cancelled: "Отменено",
  interrupted: "Прервано",
  skipped: "Условия не выполнены",
};
const builtins: [string, string][] = [
  ["Розовый", "#FF0020"],
  ["Тёплый · RGB", "#FF3020"],
  ["Фиолетовый", "#FF00C8"],
  ["Синий", "#0000FF"],
  ["Белый · RGB", "#FFFFFF"],
];

export class LightingView {
  private root = document.getElementById("lighting-root")!;
  private data: LightEvent | null = null;
  private online = false;
  private selected = new Set<string>();
  private drafts = new Map<string, ColorDraft>();
  private feedback = el("p", "", "lighting-feedback");
  private pending = new Set<string>();
  private dialog: HTMLDialogElement | null = null;
  constructor(
    private send: (value: object) => void,
    private navigate: (view: string) => void,
  ) {
    this.feedback.setAttribute("role", "status");
    document.addEventListener("workspace:navigate", (event) => {
      if ((event as CustomEvent).detail === "lighting") this.render();
    });
    this.render();
  }
  connection(online: boolean): void {
    this.online = online;
    if (!online) {
      this.pending.clear();
      this.feedback.textContent =
        "Нет соединения с Чушпаном. Команды не отправляются.";
      this.dialog?.close();
    }
    this.render();
  }
  receive(event: HudEvent): void {
    if (event.type !== "devices") return;
    this.data = event as LightEvent;
    if (event.operation_id && this.pending.delete(event.operation_id)) {
      const result = this.data.result;
      this.feedback.textContent =
        event.error || result?.error || result?.message || event.message || "";
      this.feedback.classList.toggle(
        "error",
        !!event.error ||
          result?.result_status === "failed" ||
          result?.result_status === "unverified",
      );
      if (result?.palette) this.download(result.palette);
    }
    const ids = new Set(this.lights().map((d) => d.id));
    this.selected.forEach((id) => {
      if (!ids.has(id)) this.selected.delete(id);
    });
    if (
      !this.root.contains(document.activeElement) ||
      !["INPUT", "SELECT", "TEXTAREA"].includes(
        document.activeElement?.tagName ?? "",
      )
    )
      this.render();
  }
  private lights(): DeviceRecord[] {
    return (
      this.data?.items.filter(
        (d) => d.kind === "led_strip" || d.kind === "wled",
      ) ?? []
    );
  }
  private command(operation: string, values: object = {}): void {
    if (!this.online) return;
    const request_id = crypto.randomUUID();
    this.pending.add(request_id);
    this.feedback.textContent = "Выполняю…";
    this.send({ op: "lighting", operation, ...values, request_id });
  }
  private raw(value: object): void {
    if (!this.online) return;
    const request_id = crypto.randomUUID();
    this.pending.add(request_id);
    this.send({ ...value, request_id });
  }
  private button(
    label: string,
    run: () => void,
    disabled = false,
  ): HTMLButtonElement {
    const b = el("button", label);
    b.type = "button";
    b.disabled = !this.online || disabled;
    b.onclick = run;
    return b;
  }
  private apply(action: Action, targets = [...this.selected]): void {
    if (!targets.length) {
      this.feedback.textContent = "Выбери хотя бы одну ленту.";
      return;
    }
    this.command("apply", { targets, action });
  }
  private render(): void {
    this.root.replaceChildren();
    const heading = el("header", "", "page-heading");
    heading.append(
      el("small", "ЧУШПАН · СВЕТ"),
      el("h1", "Освещение"),
      el(
        "p",
        "Твои ленты, цвета и ритм дня. Выбери цвет, затем нажми «Применить».",
      ),
    );
    this.root.append(heading, this.feedback);
    if (!this.data) {
      this.root.append(el("p", "Ожидаю реестр устройств…"));
      return;
    }
    const lights = this.lights();
    const toolbar = el("div", "", "lighting-toolbar");
    toolbar.append(
      this.button("Добавить / настроить ленты", () => this.navigate("devices")),
      this.button("Выбрать все здесь", () => {
        this.selected = new Set(
          lights.filter((d) => d.usable).map((d) => d.id),
        );
        this.render();
      }),
      this.button("Снять выбор", () => {
        this.selected.clear();
        this.render();
      }),
      this.button("Импорт палитры", () => this.importPalette()),
      this.button("Экспорт палитры", () => this.command("export")),
    );
    this.root.append(toolbar);
    const grid = el("div", "", "lighting-grid");
    for (const device of lights) grid.append(this.card(device));
    if (!lights.length)
      grid.append(
        el(
          "p",
          "Добавь ELK-BLEDOB и ELK-BLEDDM 39 в разделе «Устройства». После добавления проверь каждую функцию отдельно.",
          "lighting-empty",
        ),
      );
    this.root.append(grid);
    if (this.selected.size) {
      const selected = lights.filter((device) => this.selected.has(device.id));
      const group = el("section", "", "lighting-panel");
      group.append(el("h2", `Выбрано лент: ${this.selected.size}`));
      group.append(
        this.button(
          "Включить выбранные",
          () => this.apply({ power: true }),
          selected.some((device) => !!this.powerBlock(device, "power_on")),
        ),
        this.button(
          "Выключить выбранные",
          () => this.apply({ power: false }),
          selected.some((device) => !!this.powerBlock(device, "power_off")),
        ),
        this.button("Таймер", () => this.jobDialog()),
        this.button("Расписание", () => this.jobDialog(undefined, true)),
        this.button("Переход / эффект", () => this.effectDialog()),
        this.button("Остановить эффекты", () => this.apply({ kind: "stop" })),
        this.button("Готовые сценарии", () =>
          this.command("templates", { targets: [...this.selected] }),
        ),
      );
      const target = lights.find((d) => this.selected.has(d.id));
      if (target) group.append(this.editor(target));
      this.root.append(group);
    }
    this.renderPresets();
    this.renderJobs();
    this.renderConditions();
    this.renderJournal();
    if (this.data.lighting)
      this.root.append(el("p", this.data.lighting.notice, "lighting-notice"));
  }
  private powerBlock(
    device: DeviceRecord,
    key: "power_on" | "power_off",
  ): string {
    if (!device.usable)
      return device.reason || "Лента недоступна в текущем месте.";
    if (device.control_reason) return device.control_reason;
    if (device.busy) return "Команда выполняется.";
    if (device.connection === "error") {
      return device.connection_error || "Проверь связь с лентой.";
    }
    return device.features[key] === true
      ? ""
      : String(device.features[`${key}_reason`] || "Команда недоступна.");
  }
  private card(device: DeviceRecord): HTMLElement {
    const card = el("article", "", "lighting-card");
    const label = el("label", "", "lighting-card-title");
    const check = el("input");
    check.type = "checkbox";
    check.checked = this.selected.has(device.id);
    check.setAttribute("aria-label", "Выбрать " + device.name);
    check.onchange = () => {
      if (check.checked) this.selected.add(device.id);
      else this.selected.delete(device.id);
      this.render();
    };
    label.append(check, el("h2", device.name));
    const status =
      (
        {
          unknown: "Подключение не проверено",
          observed: "Последний контакт подтверждён",
          disconnected: "BLE-соединение закрыто · подключение по команде",
          error: "Ошибка подключения",
        } as Record<string, string>
      )[device.connection] ?? device.connection;
    const place =
      this.data?.places.find((p) => p.id === device.place_id)?.name ??
      device.place_id;
    card.append(
      label,
      el(
        "p",
        place + (device.room ? " · " + device.room : ""),
        "lighting-muted",
      ),
      el("p", status, "lighting-status"),
    );
    const onBlock = this.powerBlock(device, "power_on");
    const offBlock = this.powerBlock(device, "power_off");
    if (device.busy) {
      card.setAttribute("aria-busy", "true");
      card.append(el("p", "Команда выполняется…", "lighting-status"));
    }
    if (!device.usable || device.control_reason || device.connection_error) {
      const message = !device.usable
        ? device.reason
        : device.control_reason || device.connection_error;
      if (message) card.append(el("p", message, "error"));
    } else if (!device.busy) {
      const availability =
        onBlock === offBlock
          ? onBlock || "Управление питанием доступно"
          : [
              onBlock && `Включение. ${onBlock}`,
              offBlock && `Выключение. ${offBlock}`,
            ]
              .filter(Boolean)
              .join(" ");
      card.append(el("p", availability, "lighting-status"));
    }
    const observed = device.ble?.last_observed_power;
    const power = device.state.power;
    card.append(
      el(
        "p",
        typeof power === "boolean"
          ? `Питание: ${power ? "включена" : "выключена"}`
          : observed
            ? `Последнее наблюдение: ${observed.power ? "включена" : "выключена"} · ${new Date(observed.at).toLocaleString("ru-RU")}. Текущее питание неизвестно.`
            : "Состояние питания неизвестно",
        "lighting-muted",
      ),
    );
    const state = el("div", "", "lighting-state");
    const swatch = el("span", "", "lighting-swatch");
    swatch.style.background = String(
      device.state.color || device.sent.color || "transparent",
    );
    state.append(
      swatch,
      el(
        "span",
        device.state.color
          ? `Подтверждённый цвет: ${device.state.color}`
          : device.sent.color
            ? `Отправленный цвет: ${device.sent.color}`
            : "Цвет неизвестен",
      ),
    );
    card.append(state);
    card.append(
      el(
        "p",
        device.state.brightness !== undefined
          ? `Яркость: ${device.state.brightness}%`
          : device.sent.brightness !== undefined
            ? `Отправлено: ${device.sent.brightness}% · не считано`
            : "Яркость неизвестна",
        "lighting-muted",
      ),
    );
    const actions = el("div", "", "lighting-toolbar");
    actions.append(
      this.button(
        "Вкл",
        () => this.apply({ power: true }, [device.id]),
        !!onBlock,
      ),
      this.button(
        "Выкл",
        () => this.apply({ power: false }, [device.id]),
        !!offBlock,
      ),
    );
    if (device.kind === "led_strip") {
      actions.append(
        this.button(
          "Переподключить",
          () => this.raw({ op: "inspect", id: device.id }),
          !device.usable || !!device.control_reason || device.busy,
        ),
      );
    }
    const brightness = el("input");
    brightness.type = "range";
    brightness.min = "0";
    brightness.max = "100";
    brightness.value = String(device.sent.brightness ?? 50);
    brightness.setAttribute("aria-label", "Выбрать яркость " + device.name);
    brightness.disabled =
      !this.online || !device.features.brightness || !device.usable;
    const value = el("output", brightness.value + "%");
    brightness.oninput = () => (value.textContent = brightness.value + "%");
    if (device.features.brightness) {
      actions.append(
        brightness,
        value,
        this.button(
          "Применить яркость",
          () =>
            this.apply({ brightness: Number(brightness.value) }, [device.id]),
          brightness.disabled,
        ),
      );
    }
    card.append(actions);
    const quick = el("div", "", "lighting-quick");
    for (const [name, color] of builtins) {
      const button = this.button(
        name,
        () => this.apply({ color }, [device.id]),
        !device.features.color || !device.usable,
      );
      button.style.setProperty("--swatch", color);
      quick.append(button);
    }
    card.append(quick);
    card.append(
      this.button("Таймер", () =>
        this.jobDialog(undefined, false, [device.id]),
      ),
      this.button("Расписание", () =>
        this.jobDialog(undefined, true, [device.id]),
      ),
      this.button("Цвет / настройки", () => {
        this.selected = new Set([device.id]);
        this.render();
      }),
    );
    const details = el("details");
    details.append(
      el("summary", "Возможности и проверка протокола"),
      el("p", device.ble?.driver ?? "WLED JSON API"),
    );
    const capabilities = el("dl", "", "lighting-capabilities");
    for (const [name, key] of [
      ["RGB", "color"],
      ["Яркость", "brightness"],
      ["Включение", "power_on"],
      ["Выключение", "power_off"],
      ["Аппаратные эффекты", "effects"],
      ["Аппаратная скорость", "speed"],
      ["Белый канал", "white_channel"],
      ["Чтение состояния", "read_state"],
      ["Автономные таймеры", "autonomous_timers"],
      ["Музыкальная синхронизация", "music_sync"],
    ]) {
      capabilities.append(
        el("dt", name),
        el(
          "dd",
          device.features[key] === true
            ? key.startsWith("power_") && device.kind === "led_strip"
              ? "Отправка доступна"
              : "Подтверждено"
            : "Не подтверждено / недоступно",
        ),
      );
    }
    details.append(
      capabilities,
      el(
        "p",
        "Белый RGB — смешение цветов. Аппаратный белый канал и музыкальный режим не проверены.",
        "lighting-muted",
      ),
    );
    if (device.kind === "led_strip") {
      details.append(
        this.button("Диагностика BLE без команд", () =>
          this.raw({ op: "diagnose_ble", id: device.id }),
        ),
        this.button("Проверить включение", () =>
          this.probe(device, { power: true }),
        ),
        this.button("Проверить выключение", () =>
          this.probe(device, { power: false }),
        ),
        this.button("Проверить яркость 50%", () =>
          this.probe(device, { brightness: 50 }),
        ),
        this.button("Полные настройки / профиль", () =>
          this.navigate("devices"),
        ),
      );
      const probe = device.ble?.pending_probe;
      if (probe) {
        details.open = true;
        details.append(
          el("p", "Проверь ленту глазами. Выполнена именно тестовая команда?"),
          this.button("Да, сработало", () =>
            this.raw({
              op: "confirm_probe",
              id: device.id,
              probe_id: probe.id,
              observed: true,
            }),
          ),
          this.button("Нет", () =>
            this.raw({
              op: "confirm_probe",
              id: device.id,
              probe_id: probe.id,
              observed: false,
            }),
          ),
        );
      }
    }
    card.append(details);
    return card;
  }
  private probe(device: DeviceRecord, parameters: object): void {
    if (
      window.confirm(
        `Отправить одну тестовую команду ленте «${device.name}»? Наблюдай за результатом.`,
      )
    )
      this.raw({ op: "probe_led", id: device.id, parameters, consent: true });
  }
  private editor(device: DeviceRecord): HTMLElement {
    if (!this.drafts.has(device.id))
      this.drafts.set(
        device.id,
        colorDraft(
          device.preview_color ?? String(device.sent.color ?? "#FF00C8"),
        ),
      );
    return new ColorEditor({
      device,
      online: this.online,
      draft: this.drafts.get(device.id)!,
      colors: this.data?.user_colors ?? [],
      presets: builtins,
      preview: (color) =>
        this.raw({ op: "color_preview", id: device.id, color }),
      apply: (color) => this.apply({ color }),
      save: (value, existing?: UserColor) =>
        this.raw({
          op: "color_save",
          value,
          color_id: existing?.id,
          revision: existing?.revision ?? 0,
        }),
      remove: (color) =>
        this.raw({
          op: "color_delete",
          color_id: color.id,
          revision: color.revision,
          confirmed: true,
        }),
      favorite: (color) =>
        this.raw({
          op: "favorites",
          id: device.id,
          revision: device.revision,
          colors: [...new Set([...device.favorites, color])].slice(-16),
        }),
      verify: (color) => this.probe(device, { color }),
      confirm: (probe, observed) =>
        this.raw({
          op: "confirm_probe",
          id: device.id,
          probe_id: probe,
          observed,
        }),
    }).root;
  }
  private renderPresets(): void {
    const section = el("section", "", "lighting-panel");
    section.append(
      el("h2", "Пресеты"),
      this.button("Создать пресет", () => this.presetDialog()),
    );
    for (const preset of this.data?.lighting?.presets ?? []) {
      const row = el("div", "", "lighting-row");
      row.append(
        el("strong", preset.name),
        el(
          "span",
          [
            preset.action.color,
            preset.action.brightness != null
              ? preset.action.brightness + "%"
              : "",
            preset.action.power != null
              ? preset.action.power
                ? "вкл"
                : "выкл"
              : "",
          ]
            .filter(Boolean)
            .join(" · "),
        ),
        this.button(
          "Применить",
          () => this.apply({ kind: "preset", preset_id: preset.id }),
          !this.selected.size,
        ),
        this.button("Изменить / переименовать", () =>
          this.presetDialog(preset),
        ),
        this.button("Удалить", () => {
          if (window.confirm(`Удалить пресет «${preset.name}»?`))
            this.command("preset_delete", {
              id: preset.id,
              revision: preset.revision,
            });
        }),
      );
      section.append(row);
    }
    this.root.append(section);
  }
  private modal(title: string): {
    dialog: HTMLDialogElement;
    form: HTMLFormElement;
  } {
    this.dialog?.remove();
    const dialog = el("dialog", "", "lighting-dialog");
    const form = el("form");
    form.append(el("h2", title));
    dialog.append(form);
    const cancel = el("button", "Закрыть");
    cancel.type = "button";
    cancel.onclick = () => dialog.close();
    form.append(cancel);
    dialog.onclose = () => {
      dialog.remove();
      if (this.dialog === dialog) this.dialog = null;
      this.render();
    };
    document.body.append(dialog);
    this.dialog = dialog;
    dialog.showModal();
    return { dialog, form };
  }
  private input(
    form: HTMLElement,
    label: string,
    type: string,
    value: string,
  ): HTMLInputElement {
    const wrap = el("label", label);
    const input = el("input");
    input.type = type;
    input.value = value;
    wrap.append(input);
    form.append(wrap);
    return input;
  }
  private choose(
    form: HTMLElement,
    label: string,
    options: [string, string][],
    value: string,
  ): HTMLSelectElement {
    const wrap = el("label", label);
    const input = el("select");
    for (const [key, name] of options) {
      const option = el("option", name);
      option.value = key;
      input.append(option);
    }
    input.value = value;
    wrap.append(input);
    form.append(wrap);
    return input;
  }
  private submit(form: HTMLFormElement, label: string, run: () => void): void {
    const error = el("p", "", "error");
    error.setAttribute("role", "alert");
    const save = el("button", label);
    save.type = "submit";
    save.disabled = !this.online;
    form.append(error, save);
    form.onsubmit = (e) => {
      e.preventDefault();
      if (!form.reportValidity()) return;
      try {
        run();
        this.dialog?.close();
      } catch (exc) {
        error.textContent = String(exc instanceof Error ? exc.message : exc);
      }
    };
  }
  private actionFields(
    form: HTMLElement,
    initial: Action = {},
    allowPrograms = true,
    presetRgb = false,
  ): () => Action {
    const options: [string, string][] = [["set", "Цвет / яркость / питание"]];
    if (allowPrograms)
      options.push(
        ["fade", "Плавный переход"],
        ["effect", "Программный эффект"],
        ["preset", "Сохранённый пресет"],
      );
    const kind = this.choose(form, "Действие", options, initial.kind ?? "set");
    const fields = el("div");
    form.append(fields);
    const colorEnabled = this.input(fields, "Изменить цвет", "checkbox", "");
    colorEnabled.setAttribute("aria-label", "Изменить цвет");
    colorEnabled.checked = initial.color != null;
    const rgb = (initial.color ?? "#FF00C8").match(/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
    const rgbValues = rgb ? rgb.slice(1).map((part) => parseInt(part, 16)) : [255, 0, 200];
    const color = presetRgb ? null : this.input(fields, "Цвет HEX", "color", initial.color ?? "#FF00C8");
    let rgbInputs: HTMLInputElement[] = [];
    let palette: HTMLInputElement | null = null;
    let preview: HTMLElement | null = null;
    if (presetRgb) {
      const picker = el("div", "", "preset-color-picker");
      const channels = el("div", "", "preset-rgb");
      rgbInputs = ["R", "G", "B"].map((channel, i) => {
        const input = this.input(channels, channel, "number", String(rgbValues[i]));
        input.setAttribute("aria-label", channel);
        input.min = "0"; input.max = "255"; input.step = "1"; input.required = true;
        input.dataset.rgb = channel.toLowerCase();
        return input;
      });
      const swatches = el("div", "", "preset-swatches");
      const quick = [
        ["Тёплый белый", "#FFD6A1"], ["Холодный белый", "#E8F2FF"],
        ["Красный", "#FF0000"], ["Зелёный", "#00FF00"], ["Синий", "#0000FF"],
        ["Розовый", "#FF69B4"], ["Фиолетовый", "#A020F0"],
      ];
      for (const [label, hex] of quick) {
        const button = el("button", label); button.type = "button";
        button.style.setProperty("--swatch", hex);
        button.onclick = () => setHex(hex);
        swatches.append(button);
      }
      const pickerLabel = el("label", "Палитра");
      palette = document.createElement("input"); palette.type = "color";
      palette.setAttribute("aria-label", "Палитра цвета");
      pickerLabel.append(palette);
      preview = el("div", "", "preset-color-preview"); preview.setAttribute("aria-label", "Предпросмотр цвета");
      const random = el("button", "Случайный цвет"); random.type = "button";
      random.onclick = () => setHex(`#${Array.from({length:3}, () => Math.floor(Math.random()*256).toString(16).padStart(2,"0")).join("")}`);
      picker.append(channels, pickerLabel, preview, swatches, random);
      fields.append(picker);
      function currentHex() { return `#${rgbInputs.map((input) => Number(input.value).toString(16).padStart(2,"0")).join("")}`.toUpperCase(); }
      function setHex(hex: string) {
        const values = hex.replace("#", "").match(/.{2}/g)!.map((part) => parseInt(part,16));
        rgbInputs.forEach((input, i) => { input.value = String(values[i]); });
        updatePreview();
      }
      function updatePreview() {
        const hex = currentHex();
        if (preview) preview.style.backgroundColor = hex;
        if (palette) palette.value = hex;
      }
      rgbInputs.forEach((input) => input.addEventListener("input", updatePreview));
      palette!.addEventListener("input", () => setHex(palette!.value));
      updatePreview();
    }
    const brightnessEnabled = this.input(
      fields,
      "Изменить яркость",
      "checkbox",
      "",
    );
    brightnessEnabled.checked = initial.brightness != null;
    const brightness = this.input(
      fields,
      "Яркость 0–100",
      "number",
      String(initial.brightness ?? 50),
    );
    brightness.min = "0";
    brightness.max = "100";
    brightness.step = "1";
    const power = this.choose(
      fields,
      "Питание",
      [
        ["keep", "Сохранить"],
        ["on", "Включить"],
        ["off", "Выключить"],
      ],
      initial.power == null ? "keep" : initial.power ? "on" : "off",
    );
    const programs = el("div");
    form.append(programs);
    const duration = this.input(
      programs,
      "Длительность, минут",
      "number",
      String((initial.duration_s ?? 1200) / 60),
    );
    duration.min = "0.034";
    duration.max = "1440";
    duration.step = "any";
    const effect = this.choose(
      programs,
      "Эффект · выполняется на Mac",
      [
        ["cycle", "Смена цветов"],
        ["pulse", "Пульсация"],
        ["breathing", "Дыхание"],
        ["rainbow", "Радуга"],
        ["twinkle", "Медленное мерцание"],
        ["transition", "Переход по палитре"],
      ],
      initial.effect ?? "cycle",
    );
    const period = this.input(
      programs,
      "Период, секунд (не менее 8)",
      "number",
      String(initial.period_s ?? 20),
    );
    period.min = "8";
    period.max = "3600";
    const colors = this.input(
      programs,
      "Цвета через пробел (HEX)",
      "text",
      (initial.colors ?? ["#FF00C8", "#0000FF"]).join(" "),
    );
    const preset = this.choose(
      form,
      "Пресет",
      (this.data?.lighting?.presets ?? []).map((p) => [p.id, p.name]),
      initial.preset_id ?? "",
    );
    const visibility = () => {
      fields.hidden = !["set", "fade"].includes(kind.value);
      if (presetRgb && colorEnabled.parentElement) colorEnabled.parentElement.hidden = fields.hidden;
      if (presetRgb && colorEnabled.checked && !fields.hidden) {
        const picker = fields.querySelector<HTMLElement>(".preset-color-picker");
        if (picker) picker.hidden = false;
      }
      programs.hidden = !["fade", "effect"].includes(kind.value);
      preset.parentElement!.hidden = kind.value !== "preset";
      effect.parentElement!.hidden =
        period.parentElement!.hidden =
        colors.parentElement!.hidden =
          kind.value !== "effect";
    };
    kind.onchange = visibility;
    if (presetRgb) colorEnabled.onchange = () => {
      const picker = fields.querySelector<HTMLElement>(".preset-color-picker");
      if (picker) picker.hidden = !colorEnabled.checked;
    };
    if (presetRgb) {
      const picker = fields.querySelector<HTMLElement>(".preset-color-picker");
      if (picker) picker.hidden = !colorEnabled.checked;
    }
    visibility();
    return () => {
      if (kind.value === "preset") {
        if (!preset.value) throw new Error("Сначала создай пресет.");
        return { kind: "preset", preset_id: preset.value };
      }
      if (kind.value === "effect")
        return {
          kind: "effect",
          duration_s: Math.round(Number(duration.value) * 60),
          period_s: Number(period.value),
          effect: effect.value,
          colors: colors.value.trim().split(/\s+/),
        };
      const action: Action = { kind: kind.value };
      if (colorEnabled.checked) action.color = presetRgb
        ? `#${rgbInputs.map((input) => Number(input.value).toString(16).padStart(2,"0")).join("")}`.toUpperCase()
        : color!.value.toUpperCase();
      if (brightnessEnabled.checked)
        action.brightness = Number(brightness.value);
      if (power.value !== "keep") action.power = power.value === "on";
      if (kind.value === "fade")
        action.duration_s = Math.round(Number(duration.value) * 60);
      if (
        action.color === undefined &&
        action.brightness === undefined &&
        action.power === undefined
      )
        throw new Error("Выбери хотя бы один параметр.");
      return action;
    };
  }
  private presetDialog(preset?: Preset): void {
    const { form } = this.modal(preset ? "Изменить пресет" : "Новый пресет");
    const name = this.input(form, "Название", "text", preset?.name ?? "");
    name.setAttribute("aria-label", "Название");
    name.required = true;
    name.maxLength = 80;
    const action = this.actionFields(form, preset?.action, false, true);
    this.submit(form, "Сохранить", () =>
      this.command("preset_save", {
        value: {
          id: preset?.id ?? "",
          revision: preset?.revision ?? 0,
          name: name.value,
          action: action(),
        },
      }),
    );
  }
  private effectDialog(): void {
    const { form } = this.modal("Переход или эффект");
    form.append(
      el(
        "p",
        "Программный режим: Mac должен работать. Обновление не чаще одного раза в 2 секунды.",
      ),
    );
    const action = this.actionFields(form, {
      kind: "fade",
      brightness: 0,
      duration_s: 1200,
    });
    this.submit(form, "Запустить", () => this.apply(action()));
  }
  private jobDialog(
    job?: Job,
    schedule = false,
    targets = [...this.selected],
  ): void {
    const { form } = this.modal(
      job ? "Изменить задачу" : schedule ? "Новое расписание" : "Новый таймер",
    );
    const name = this.input(
      form,
      "Название",
      "text",
      job?.name ?? (schedule ? "Расписание света" : "Таймер сна"),
    );
    name.required = true;
    name.maxLength = 100;
    const selected = job?.targets ?? targets;
    form.append(
      el(
        "p",
        "Ленты: " +
          selected
            .map((id) => this.lights().find((d) => d.id === id)?.name ?? id)
            .join(", "),
      ),
    );
    const action = this.actionFields(form, job?.action ?? { power: false });
    const kind = this.choose(
      form,
      "Повтор",
      [
        ["once", "Один раз / задержка"],
        ["interval", "Через равные интервалы"],
        ["weekly", "Время и дни недели"],
        ["date", "Конкретная дата"],
        ["sunrise", "Восход · вручную заданное место"],
        ["sunset", "Закат · вручную заданное место"],
      ],
      job?.rule.kind ?? (schedule ? "weekly" : "once"),
    );
    const delay = this.input(
      form,
      "Через сколько минут / интервал",
      "number",
      String((job?.rule.interval_s ?? 2700) / 60),
    );
    delay.min = "0.167";
    delay.max = "525600";
    delay.step = "any";
    const start = this.input(
      form,
      "Следующий запуск (для одноразового/интервального; необязательно)",
      "datetime-local",
      job && job.rule.kind === "once" ? this.localInput(job.next_at) : "",
    );
    const at = this.input(form, "Время суток", "time", job?.rule.at ?? "23:30");
    const date = this.input(form, "Дата", "date", job?.rule.date ?? "");
    const days = el("fieldset");
    days.append(el("legend", "Дни недели"));
    const checks: HTMLInputElement[] = [];
    ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].forEach((name, index) => {
      const input = this.input(days, name, "checkbox", "");
      input.checked = (job?.rule.weekdays ?? [0, 1, 2, 3, 4, 5, 6]).includes(
        index,
      );
      checks.push(input);
    });
    form.append(days);
    const timezone = this.input(
      form,
      "Часовой пояс IANA",
      "text",
      job?.rule.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone,
    );
    const coordinates = el("fieldset");
    coordinates.append(
      el("legend", "Координаты для солнца · без доступа к геолокации"),
    );
    const latitude = this.input(
      coordinates,
      "Широта",
      "number",
      String(job?.rule.latitude ?? ""),
    );
    latitude.min = "-90";
    latitude.max = "90";
    latitude.step = "any";
    const longitude = this.input(
      coordinates,
      "Долгота",
      "number",
      String(job?.rule.longitude ?? ""),
    );
    longitude.min = "-180";
    longitude.max = "180";
    longitude.step = "any";
    const offset = this.input(
      coordinates,
      "Смещение от восхода/заката, минут",
      "number",
      String(job?.rule.offset_minutes ?? 0),
    );
    offset.min = "-720";
    offset.max = "720";
    form.append(coordinates);
    const place = this.choose(
      form,
      "Выполнять в месте",
      this.data?.places.map((p) => [p.id, p.name]) ?? [],
      job?.place_id ?? "general",
    );
    const conditionNames = new Set([
      ...Object.keys(this.data?.lighting?.flags ?? {}),
      ...Object.keys(job?.conditions ?? {}),
    ]);
    const conditions = [...conditionNames].map((key) => {
      const required = job?.conditions[key];
      const select = this.choose(
        form,
        `Условие «${key}»`,
        [
          ["any", "Не учитывать"],
          ["on", "Включено"],
          ["off", "Выключено"],
        ],
        required === undefined ? "any" : required ? "on" : "off",
      );
      return { key, select };
    });
    const grace = this.input(
      form,
      "Догнать пропуск в пределах, минут (0 = пропустить)",
      "number",
      String((job?.catch_up_s ?? 900) / 60),
    );
    grace.min = "0";
    grace.max = "1440";
    grace.step = "any";
    const enabled = this.input(form, "Задача включена", "checkbox", "");
    enabled.checked = job?.enabled ?? true;
    const visibility = () => {
      const solar = ["sunrise", "sunset"].includes(kind.value);
      coordinates.hidden = !solar;
      latitude.required = longitude.required = solar;
      date.parentElement!.hidden = kind.value !== "date";
      date.required = kind.value === "date";
      at.parentElement!.hidden = !["weekly", "date"].includes(kind.value);
      days.hidden = !["weekly", "sunrise", "sunset"].includes(kind.value);
      delay.parentElement!.hidden = start.parentElement!.hidden = ![
        "once",
        "interval",
      ].includes(kind.value);
    };
    kind.onchange = visibility;
    visibility();
    form.append(
      el(
        "p",
        "После сна выполняется только последний допустимый запуск. При выключенном Mac автономное выполнение не подтверждено.",
      ),
    );
    this.submit(form, "Сохранить задачу", () => {
      const rule: Rule = {
        kind: kind.value,
        at: at.value || "23:30",
        timezone: timezone.value,
        weekdays: checks.flatMap((c, i) => (c.checked ? [i] : [])),
        interval_s: Math.round(Number(delay.value) * 60),
      };
      if (!rule.weekdays!.length) throw new Error("Выбери день недели.");
      if (kind.value === "date") rule.date = date.value;
      if (["sunrise", "sunset"].includes(kind.value)) {
        rule.latitude = Number(latitude.value);
        rule.longitude = Number(longitude.value);
        rule.offset_minutes = Number(offset.value);
      }
      const next_at = start.value
        ? new Date(start.value).toISOString()
        : new Date(Date.now() + Number(delay.value) * 60000).toISOString();
      this.command("job_save", {
        value: {
          id: job?.id ?? "",
          revision: job?.revision ?? 0,
          name: name.value,
          targets: selected,
          action: action(),
          rule,
          next_at,
          enabled: enabled.checked,
          place_id: place.value,
          conditions: Object.fromEntries(
            conditions.flatMap(({ key, select }) =>
              select.value === "any" ? [] : [[key, select.value === "on"]],
            ),
          ),
          catch_up_s: Math.round(Number(grace.value) * 60),
        },
      });
    });
  }
  private localInput(iso: string): string {
    const date = new Date(iso);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
  }
  private renderJobs(): void {
    const section = el("section", "", "lighting-panel");
    section.append(el("h2", "Таймеры и расписания"));
    const jobs = this.data?.lighting?.jobs ?? [];
    if (!jobs.length)
      section.append(el("p", "Выбери ленту и создай таймер или расписание."));
    for (const job of jobs) {
      const row = el("article", "", "lighting-row");
      row.append(
        el("strong", job.name),
        el(
          "span",
          `${outcomes[job.status] ?? job.status} · ${job.enabled ? new Date(job.next_at).toLocaleString("ru-RU") : "отключено"}`,
        ),
        this.button("Изменить", () => this.jobDialog(job)),
        this.button(job.enabled ? "Пауза" : "Включить", () =>
          this.command("job_save", {
            value: {
              ...job,
              enabled: !job.enabled,
              next_at: job.enabled
                ? job.next_at
                : new Date(Date.now() + 10000).toISOString(),
            },
          }),
        ),
        this.button("Отменить / удалить", () =>
          this.command("job_delete", { id: job.id, revision: job.revision }),
        ),
      );
      section.append(row);
    }
    this.root.append(section);
  }
  private renderConditions(): void {
    const details = el("details", "", "lighting-panel");
    details.append(el("summary", "Пользовательские условия расписаний"));
    const flags = this.data?.lighting?.flags ?? {};
    const form = el("form");
    for (const [name, enabled] of Object.entries(flags)) {
      const row = el("div", "", "lighting-row");
      row.append(
        el("strong", name),
        this.button(enabled ? "Включено" : "Выключено", () => {
          this.command("flags", { flags: { ...flags, [name]: !enabled } });
        }),
        this.button("Удалить", () => {
          const updated = { ...flags };
          delete updated[name];
          this.command("flags", { flags: updated });
        }),
      );
      details.append(row);
    }
    const name = this.input(form, "Новое условие", "text", "");
    name.maxLength = 60;
    name.required = true;
    const add = el("button", "Добавить условие");
    add.type = "submit";
    add.disabled = !this.online;
    form.append(add);
    form.onsubmit = (event) => {
      event.preventDefault();
      const key = name.value.trim();
      if (!key || Object.hasOwn(flags, key)) {
        this.feedback.textContent = "Укажи новое название условия.";
        return;
      }
      this.command("flags", { flags: { ...flags, [key]: true } });
      name.value = "";
    };
    details.append(form);
    this.root.append(details);
  }
  private renderJournal(): void {
    const details = el("details", "", "lighting-panel");
    details.append(el("summary", "Журнал освещения"));
    for (const item of this.data?.lighting?.journal ?? []) {
      const row = el(
        "p",
        `${new Date(item.at).toLocaleString("ru-RU")} · ${outcomes[item.status] ?? item.status} · ${item.message}`,
      );
      details.append(row);
    }
    this.root.append(details);
  }
  private importPalette(): void {
    const input = el("input");
    input.type = "file";
    input.accept = ".json,application/json";
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      if (file.size > 65536) {
        this.feedback.textContent = "Палитра больше 64 КБ.";
        return;
      }
      try {
        this.command("import", { palette: JSON.parse(await file.text()) });
      } catch {
        this.feedback.textContent = "Не удалось прочитать JSON палитры.";
      }
    };
    input.click();
  }
  private download(value: object): void {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
    );
    const link = el("a");
    link.href = url;
    link.download = "chushpan-palette.json";
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

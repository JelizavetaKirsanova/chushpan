import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { stripTypeScriptTypes } from "node:module";
import { Window } from "happy-dom";

function source(file: string): string {
  return stripTypeScriptTypes(
    readFileSync(new URL("../src/" + file, import.meta.url), "utf8"),
    { mode: "transform" },
  )
    .replace(/^import .*;\n/gm, "")
    .replace(/\bexport (?=(?:class|function|const))/g, "");
}
function fixture() {
  const window = new Window();
  window.confirm = () => true;
  window.document.body.innerHTML = '<div id="lighting-root"></div>';
  const context = {
    window,
    document: window.document,
    CustomEvent: window.CustomEvent,
    crypto: globalThis.crypto,
    Intl,
    exports: {} as any,
    URL: globalThis.URL,
    Blob: globalThis.Blob,
  };
  vm.runInNewContext(
    "const {ColorEditor,colorDraft}=(()=>{" +
      source("color_editor.ts") +
      ";return {ColorEditor,colorDraft};})();\n" +
      source("lighting.ts") +
      "\nexports.LightingView=LightingView;",
    context,
  );
  const events: any[] = [];
  const view = new context.exports.LightingView(
    (event: any) => events.push(event),
    () => {},
  );
  const device = {
    id: "one",
    revision: 1,
    name: "ELK-BLEDOB",
    kind: "led_strip",
    protocol: "ble",
    endpoint: "private-endpoint",
    place_id: "home",
    room: "",
    state: {},
    sent: { color: "#FF0099", brightness: 30 },
    last_contact: null,
    state_at: null,
    result_status: "unverified",
    favorites: [],
    features: {
      power_on: true,
      power_off: true,
      color: true,
      brightness: true,
      probe: true,
      colors: ["rgb"],
    },
    connection: "disconnected",
    usable: true,
    reason: "",
    busy: false,
  };
  const state = {
    type: "devices",
    items: [
      device,
      {
        ...device,
        id: "two",
        name: "ELK-BLEDDM 39",
        features: {
          ...device.features,
          power_on: false,
          power_off: false,
          brightness: false,
        },
      },
    ],
    places: [
      { id: "general", name: "Общие" },
      { id: "home", name: "Дом" },
    ],
    location: {
      place_id: "home",
      name: "Дом",
      status: "ready",
      mode: "manual",
    },
    kinds: [],
    lighting: {
      presets: [],
      jobs: [],
      flags: {},
      journal: [],
      notice: "Таймеры выполняет Mac.",
    },
  };
  view.connection(true);
  view.receive(state);
  const button = (label: string, scope: ParentNode = window.document) => {
    const b = [...scope.querySelectorAll("button")].find(
      (b) => b.textContent === label,
    );
    assert.ok(b, label);
    b.click();
    return b;
  };
  const select = () => {
    const input = window.document.querySelector<HTMLInputElement>(
      '[aria-label="Выбрать ELK-BLEDOB"]',
    )!;
    input.checked = true;
    input.dispatchEvent(new window.Event("change"));
  };
  return {
    window,
    events,
    state,
    view,
    button,
    select,
    close: () => window.happyDOM.close(),
  };
}

test("cards describe capabilities and sent state without claiming physical success", async () => {
  const f = fixture();
  try {
    assert.equal(
      f.window.document.querySelectorAll(".lighting-card").length,
      2,
    );
    const second = f.window.document.querySelectorAll(".lighting-card")[1];
    assert.match(second.textContent!, /Не подтверждено \/ недоступно/);
    assert.ok(
      [...second.querySelectorAll("button")].find(
        (b) => b.textContent === "Вкл",
      )!.disabled,
    );
    assert.match(second.textContent!, /Отправленный цвет/);
    assert.doesNotMatch(second.textContent!, /private-endpoint/);
    assert.equal(f.events.length, 0);
  } finally {
    await f.close();
  }
});
test("preview never sends BLE and explicit group Apply contains selected targets", async () => {
  const f = fixture();
  try {
    f.button("Выбрать все здесь");
    const hex = f.window.document.querySelector<HTMLInputElement>(
      '[aria-label="HEX цвета"]',
    )!;
    hex.value = "#112233";
    hex.dispatchEvent(new f.window.Event("input"));
    assert.ok(f.events.every((e) => e.op === "color_preview"));
    f.button("Применить");
    const applied = f.events.at(-1);
    assert.equal(applied.operation, "apply");
    assert.deepEqual(Array.from(applied.targets), ["one", "two"]);
    assert.equal(applied.action.color, "#112233");
  } finally {
    await f.close();
  }
});
test("preset dialog saves locally and timer dialog submits a durable document", async () => {
  const f = fixture();
  try {
    f.button("Создать пресет");
    let form = f.window.document.querySelector("dialog form")!;
    const inputs = form.querySelectorAll<HTMLInputElement>("input");
    inputs[0].value = "Мой вечер";
    inputs[1].checked = true;
    form.dispatchEvent(new f.window.Event("submit", { cancelable: true }));
    assert.equal(f.events.at(-1).operation, "preset_save");
    f.select();
    f.button("Таймер", f.window.document.querySelector(".lighting-panel")!);
    form = f.window.document.querySelector("dialog form")!;
    form.dispatchEvent(new f.window.Event("submit", { cancelable: true }));
    const event = f.events.at(-1);
    assert.equal(event.operation, "job_save");
    assert.equal(event.value.rule.kind, "once");
    assert.equal(event.value.action.power, false);
    assert.ok(Date.parse(event.value.next_at) > Date.now());
  } finally {
    await f.close();
  }
});
test("disconnect disables all command buttons and displays connection state", async () => {
  const f = fixture();
  try {
    f.view.connection(false);
    assert.ok(
      [...f.window.document.querySelectorAll("button")].every(
        (b) => b.disabled,
      ),
    );
    assert.match(
      f.window.document.querySelector(".lighting-feedback")!.textContent!,
      /Нет соединения/,
    );
  } finally {
    await f.close();
  }
});

test("reconnect unlocks normal power commands without assuming the strip is off", async () => {
  const f = fixture();
  try {
    const [first, second] = f.state.items;
    const card = () => f.window.document.querySelectorAll(".lighting-card")[1];
    f.view.receive({
      ...f.state,
      items: [
        first,
        {
          ...second,
          connection: "error",
          connection_error: "Лента вне зоны Bluetooth",
        },
      ],
    });
    assert.match(card().textContent!, /Лента вне зоны Bluetooth/);
    assert.match(card().textContent!, /Состояние питания неизвестно/);
    assert.ok(f.button("Вкл", card()).disabled);
    f.button("Переподключить", card());
    assert.equal(f.events.at(-1).op, "inspect");
    assert.equal(f.events.at(-1).id, "two");
    const ready = {
      ...second,
      features: { ...second.features, power_on: true, power_off: true },
    };
    f.view.receive({ ...f.state, items: [first, ready] });
    assert.match(card().textContent!, /Управление питанием доступно/);
    assert.match(card().textContent!, /Состояние питания неизвестно/);
    assert.equal(f.button("Вкл", card()).disabled, false);
    assert.equal(f.events.at(-1).operation, "apply");
    assert.equal(f.events.at(-1).action.power, true);
    f.view.receive({ ...f.state, items: [first, { ...ready, busy: true }] });
    assert.equal(card().getAttribute("aria-busy"), "true");
    assert.ok(f.button("Выкл", card()).disabled);
    f.view.receive({
      ...f.state,
      items: [first, { ...ready, control_reason: "Mac Agent недоступен" }],
    });
    assert.match(card().textContent!, /Mac Agent недоступен/);
    assert.ok(f.button("Вкл", card()).disabled);
  } finally {
    await f.close();
  }
});

import type {HudEvent} from './types';
import {icon} from './dashboard';
import './automations.css';

type Json = string | number | boolean | null | {[key: string]: Json} | Json[];
export interface Condition {source: 'variable' | 'available' | 'time'; key: string; operator: 'eq' | 'ne' | 'gt' | 'lt'; value: Json}
export interface Block {kind: 'action' | 'delay' | 'sequence' | 'parallel' | 'if'; capability: string; parameters: Record<string, Json>; seconds: number; condition: Condition | null; children: Block[]; otherwise: Block[]; on_error: 'stop' | 'continue'}
export interface Scenario {id: string; revision: number; name: string; description: string; icon: string; enabled: boolean; mode: 'exact' | 'smart'; phrases: string[]; actions: Block[]; conditions: Condition[]; triggers: {kind: 'voice' | 'manual' | 'schedule'; at: string; weekdays: number[]}[]; variables: {name: string; kind: 'number' | 'string' | 'boolean'; default: Json}[]; confirmation: 'auto' | 'always'}
interface Parameter {type: string; enum?: Json[]; minimum?: number; maximum?: number; maxLength?: number; pattern?: string}
interface Capability {id: string; name: string; device: string; description: string; parameters: Record<string, Parameter>; required: string[]; permissions: string[]; risk: string; availability: string}
interface Recognition {scenario_id: string | null; name?: string; confidence: number; method: string; candidates: {id: string; name: string; confidence: number}[]}
interface Step {path: string; capability: string; kind: string; status: string; duration_s?: number; result?: Record<string, Json>; error?: string}
interface Run {id: string; scenario_id: string; name: string; status: string; steps: Step[]; duration_s?: number; started_at: string; error?: string}
interface Device {id: string; name: string; type: string; status: string; actions: string[]; permissions: string[]; note: string; last_success?: string; last_operation?: string; error?: string; details?: Json}
interface Permission {id: string; name: string; status: string; note: string; url: string}
export interface AutomationsEvent {type: 'automations'; scenarios: Scenario[]; capabilities: Capability[]; runs: Run[]; devices: Device[]; training: {id: number; scenario_id: string; phrase: string; operation: string; created_at: string}[]; permissions?: {items: Permission[]; error?: string; scope: string}; error?: string; message?: string; saved?: string; updated?: string; deleted?: string; recognition?: Recognition; captured?: string; result?: {message?: string; error?: string; status?: string}}

const el = <T extends keyof HTMLElementTagNameMap>(tag: T, value = '', className = ''): HTMLElementTagNameMap[T] => {
  const node = document.createElement(tag); node.textContent = value; node.className = className; return node;
};
const clone = <T>(value: T): T => structuredClone(value);
export function newBlock(kind: Block['kind'] = 'action', capability = ''): Block {
  return {kind, capability, parameters: {}, seconds: 1, condition: kind === 'if' ? {source: 'variable', key: '', operator: 'eq', value: true} : null, children: [], otherwise: [], on_error: 'stop'};
}
export function newScenario(): Scenario {
  return {id: '', revision: 0, name: 'Новый сценарий', description: '', icon: '✦', enabled: true, mode: 'smart', phrases: [], actions: [], conditions: [], triggers: [{kind: 'voice', at: '22:00', weekdays: [0,1,2,3,4,5,6]}, {kind: 'manual', at: '22:00', weekdays: [0,1,2,3,4,5,6]}], variables: [], confirmation: 'auto'};
}
export function moveBlock(blocks: Block[], index: number, offset: number): void {
  const target = index + offset;
  if (index < 0 || index >= blocks.length || target < 0 || target >= blocks.length) return;
  [blocks[index], blocks[target]] = [blocks[target], blocks[index]];
}
const status: Record<string, string> = {confirmed: 'Проверено', sent: 'Отправлено, физический результат не подтверждён', unverified: 'Результат не подтверждён', failed: 'Ошибка', blocked: 'Заблокировано', interrupted: 'Прервано', running: 'Выполняется', skipped: 'Пропущено', not_checked: 'Не проверено', unavailable: 'Недоступно', connected: 'Подключено', observed: 'Ответ получен', error: 'Ошибка', granted: 'Предоставлено', denied: 'Не предоставлено', manual: 'Требуется ручная настройка', unsupported: 'Не поддерживается', unknown: 'Не удалось определить'};
const parameterNames: Record<string, string> = {level: 'Уровень, %', mute: 'Без звука', power: 'Питание', color: 'Цвет RGB', brightness: 'Яркость, %', brightness_delta: 'Изменение яркости', name: 'Название', input: 'Входной текст / URL', command: 'Музыкальная команда', action: 'Действие'};

export class AutomationsView {
  private data: AutomationsEvent | null = null;
  private online = false;
  private draft: Scenario | null = null;
  private dirty = false;
  private saving = false;
  private list = el('div', '', 'scenario-list');
  private editor = el('div', '', 'scenario-editor');
  private feedback = el('p', 'Подключение к Чушпану…', 'scenario-feedback');
  private deviceRoot = el('div', '', 'integration-grid');
  private permissionRoot = el('div', '', 'integration-grid');
  private commandRoot = el('div');
  private trainingRoot = el('div');
  private runRoot = el('div');
  private phraseText = '';
  private phraseInput: HTMLInputElement | null = null;
  private recognition = el('div', '', 'scenario-recognition');
  private trainingSelect = el('select');
  private permissions: AutomationsEvent['permissions'];
  private dragSource: {blocks: Block[]; index: number} | null = null;
  private sectionFeedback: HTMLElement[] = [];

  constructor(private send: (value: object) => void, navigate: (view: string) => void, private wake: () => void, private chat: (text: string) => void) {
    this.feedback.setAttribute('role', 'status'); this.feedback.setAttribute('aria-live', 'polite');
    const page = document.querySelector<HTMLElement>('.automations-page')!;
    page.querySelector('h1')!.textContent = 'Сценарии';
    page.querySelector('.page-heading p')!.textContent = 'Собери действия подключённых интеграций и добавь собственные голосовые фразы.';
    const toolbar = el('div', '', 'page-toolbar');
    toolbar.append(this.button('Новый сценарий', () => {this.draft = newScenario(); this.dirty = false; this.renderEditor();}), this.button('Обновить', () => this.send({op: 'refresh'})), this.button('Остановить ожидание', () => this.send({op: 'cancel'})));
    const layout = el('div', '', 'scenario-layout'); layout.append(this.list, this.editor);
    const history = el('details'); history.append(el('summary', 'История запусков'), this.runRoot);
    page.querySelector('.page-heading')!.after(toolbar, this.feedback, layout, history);
    const old = page.querySelector('.integration-grid');
    if (old) {const details = el('details', '', 'legacy-ritual'); details.append(el('summary', 'Встроенные сценарии и комната')); old.before(details); details.append(old);}
    const scenarioNav = document.querySelector<HTMLButtonElement>('[data-view="automations"]')!;
    scenarioNav.querySelector('span:last-of-type')!.textContent = 'Сценарии';
    scenarioNav.title = 'Сценарии'; scenarioNav.setAttribute('aria-label', 'Сценарии');
    const sections: [string, string, HTMLElement][] = [['devices', 'Устройства', this.deviceRoot], ['permissions', 'Разрешения', this.permissionRoot], ['commands', 'Команды и понимание', this.commandRoot], ['training', 'История обучения', this.trainingRoot]];
    for (const [view, label, root] of sections) {
      const nav = el('button'); nav.dataset.view = view; nav.title = label; nav.setAttribute('aria-label', label);
      const glyph = el('span'); glyph.dataset.icon = {devices: 'lamp', permissions: 'settings', commands: 'mic', training: 'history'}[view]; glyph.innerHTML = icon(glyph.dataset.icon!);
      nav.append(glyph, el('span', label));
      document.querySelector('[data-view="settings"]')!.before(nav);
      nav.onclick = () => navigate(view);
      const section = el('section', '', `workspace-page automation-section ${view}-page`); section.setAttribute('aria-label', label);
      section.append(el('h1', label));
      if (view === 'devices') section.append(el('p', 'Проверка читает состояние. Кнопка тестирования выбранного действия изменяет устройство.', 'muted'), this.button('Обновить список', () => this.send({op: 'refresh'})));
      if (view === 'permissions') section.append(el('p', 'Проверка не запрашивает доступ. macOS выдаёт разрешения конкретному приложению запуска.', 'muted'), this.button('Проверить разрешения', () => this.send({op: 'permissions'})));
      const feedback = el('p', '', 'scenario-feedback'); feedback.setAttribute('role', 'status'); feedback.setAttribute('aria-live', 'polite');
      this.sectionFeedback.push(feedback); section.append(feedback, root); document.querySelector('.hud')!.append(section);
    }
    this.renderCommands();
  }

  private button(label: string, action: () => void): HTMLButtonElement {
    const button = el('button', label); button.type = 'button'; button.disabled = !this.online;
    button.onclick = action; return button;
  }
  private input(value: string, change: (value: string) => void, type = 'text'): HTMLInputElement {
    const input = el('input'); input.type = type; input.value = value;
    input.oninput = () => {change(input.value); this.dirty = true;}; return input;
  }
  private select<T extends string>(values: [T, string][], current: T, change: (value: T) => void): HTMLSelectElement {
    const select = el('select');
    for (const [value, label] of values) {const option = el('option', label); option.value = value; select.append(option);}
    select.value = current; select.onchange = () => {change(select.value as T); this.dirty = true;}; return select;
  }
  private label(name: string, control: HTMLElement): HTMLLabelElement {const label = el('label'); label.append(el('span', name), control); return label;}
  connection(online: boolean): void {
    this.online = online;
    if (!online) this.saving = false;
    document.querySelectorAll<HTMLButtonElement | HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>('.scenario-layout button, .automation-section button, .automations-page > .page-toolbar button, .scenario-layout input, .scenario-layout select, .scenario-layout textarea, .automation-section input, .automation-section select').forEach(control => control.disabled = !online);
    this.feedback.textContent = online ? 'Подключено.' : 'Нет соединения. Сохранение и выполнение недоступны.';
    this.updateFeedback();
    if (online) this.send({op: 'refresh'});
  }
  receive(event: HudEvent): void {
    if (event.type !== 'automations') return;
    this.data = event;
    if (event.permissions) this.permissions = event.permissions;
    if (event.saved) {
      this.draft = clone(event.scenarios.find(s => s.id === event.saved) ?? newScenario());
      this.dirty = false; this.saving = false; this.renderEditor();
    } else if (event.deleted === this.draft?.id) {this.draft = null; this.editor.replaceChildren();}
    else if (event.updated === this.draft?.id && !this.dirty) {this.draft = clone(event.scenarios.find(s => s.id === event.updated)!); this.renderEditor();}
    if (event.error) {this.saving = false; this.feedback.textContent = event.error; this.feedback.dataset.error = 'true';}
    else if (event.message) {this.feedback.textContent = event.message; delete this.feedback.dataset.error;}
    if (event.captured !== undefined) {
      this.phraseText = event.captured;
      if (this.phraseInput) this.phraseInput.value = event.captured;
    }
    if (event.recognition) {
      const r = event.recognition;
      this.recognition.replaceChildren(el('strong', r.name ?? (r.method === 'ambiguous' ? 'Нужно уточнение' : 'Сценарий не найден')), el('p', `Уверенность сопоставления: ${(r.confidence * 100).toFixed(1)}% · ${r.method === 'exact' ? 'точная фраза' : r.method === 'semantic' ? 'локальное смысловое сопоставление' : r.method}`));
      for (const candidate of r.candidates) this.recognition.append(el('p', `${candidate.name}: ${(candidate.confidence * 100).toFixed(1)}%`));
      this.recognition.append(el('small', 'Это оценка сопоставления сценария, а не вероятность правильного распознавания речи. Проверка ничего не запускает.'));
    }
    if (event.result?.status === 'confirmation_required') {
      const confirm = this.button('Ввести код подтверждения', () => {const code = window.prompt('Код из сообщения Чушпана'); if (code) this.chat(`Подтверждаю ${code}`);});
      this.feedback.append(el('br'), confirm);
    }
    this.updateFeedback();
    this.renderList(); this.renderDevices(); this.renderPermissions(); this.renderTraining(); this.renderRuns();
    const selected = this.trainingSelect.value;
    this.trainingSelect.replaceChildren(...event.scenarios.map(s => {const option = el('option', s.name); option.value = s.id; return option;}));
    if (event.scenarios.some(s => s.id === selected)) this.trainingSelect.value = selected;
  }
  private updateFeedback(): void {
    for (const node of this.sectionFeedback) {node.textContent = this.feedback.textContent; node.dataset.error = this.feedback.dataset.error ?? '';}
  }

  private renderList(): void {
    this.list.replaceChildren();
    for (const scenario of this.data?.scenarios ?? []) {
      const card = el('article', '', 'scenario-card');
      card.append(el('h2', `${scenario.icon} ${scenario.name}`), el('p', scenario.description), el('small', `${scenario.enabled ? 'Включён' : 'Выключен'} · ${scenario.phrases.length} фраз · версия ${scenario.revision}`));
      const actions = el('div', '', 'button-row');
      actions.append(this.button('Редактировать', () => {
        if (this.dirty && !window.confirm('Отбросить несохранённые изменения?')) return;
        this.draft = clone(scenario); this.dirty = false; this.renderEditor();
      }), this.button('Копировать', () => this.send({op: 'copy', id: scenario.id})), this.button('Запустить', () => this.send({op: 'run', id: scenario.id, revision: scenario.revision})));
      const run = actions.lastElementChild as HTMLButtonElement;
      run.disabled ||= !scenario.enabled || !scenario.triggers.some(t => t.kind === 'manual');
      card.append(actions); this.list.append(card);
    }
    if (!this.data?.scenarios.length) this.list.append(el('p', 'Создай первый сценарий.'));
  }

  private renderEditor(): void {
    const draft = this.draft;
    this.editor.replaceChildren();
    if (!draft) return;
    const form = el('form'); form.onsubmit = event => {event.preventDefault(); if (!this.online || this.saving) return; this.saving = true; this.feedback.textContent = 'Сохраняю…'; this.send({op: 'save', scenario: draft});};
    const heading = el('div', '', 'section-heading'); heading.append(el('h2', 'Редактор сценария'), el('small', draft.id ? `Версия ${draft.revision}` : 'Ещё не сохранён'));
    const name = this.input(draft.name, v => draft.name = v); name.required = true; name.maxLength = 120;
    const description = el('textarea'); description.value = draft.description; description.maxLength = 1000; description.oninput = () => {draft.description = description.value; this.dirty = true;};
    const enabled = el('input'); enabled.type = 'checkbox'; enabled.checked = draft.enabled; enabled.onchange = () => {draft.enabled = enabled.checked; this.dirty = true;};
    form.append(heading, this.label('Название', name), this.label('Описание', description), this.label('Иконка', this.input(draft.icon, v => draft.icon = v)), this.label('Включён', enabled), this.label('Распознавание', this.select([['exact', 'Точное'], ['smart', 'Умное · локальное']], draft.mode, v => draft.mode = v)), this.label('Подтверждение', this.select([['auto', 'Автоматически для безопасных действий'], ['always', 'Всегда запрашивать код']], draft.confirmation, v => draft.confirmation = v)));
    form.append(el('h3', 'Голосовые фразы'));
    const phraseList = el('div', '', 'phrase-list');
    draft.phrases.forEach((phrase, index) => {const row = el('div', '', 'phrase-row'); const input = this.input(phrase, v => draft.phrases[index] = v); input.setAttribute('aria-label', `Фраза ${index + 1}`); row.append(input, this.button('Удалить', () => {draft.phrases.splice(index, 1); this.dirty = true; this.renderEditor();})); phraseList.append(row);});
    form.append(phraseList, this.button('Добавить фразу', () => {draft.phrases.push(''); this.dirty = true; this.renderEditor();}), el('small', 'Количество фраз не ограничено. Дубликаты и конфликты проверяются при сохранении.'));
    form.append(el('h3', 'Переменные'));
    for (const variable of draft.variables) {
      const row = el('div', '', 'phrase-row');
      row.append(this.label('Имя', this.input(variable.name, v => variable.name = v)), this.label('Тип', this.select([['number', 'Число'], ['string', 'Текст'], ['boolean', 'Да / нет']], variable.kind, v => {variable.kind = v; variable.default = v === 'number' ? 50 : v === 'boolean' ? true : ''; this.renderEditor();})), this.label('Значение', this.input(String(variable.default), v => variable.default = variable.kind === 'number' ? Number(v) : variable.kind === 'boolean' ? v === 'true' : v)), this.button('Удалить', () => {draft.variables.splice(draft.variables.indexOf(variable), 1); this.dirty = true; this.renderEditor();})); form.append(row);
    }
    form.append(this.button('Добавить переменную', () => {draft.variables.push({name: `value${draft.variables.length + 1}`, kind: 'number', default: 50}); this.dirty = true; this.renderEditor();}));
    form.append(el('h3', 'Условия запуска'));
    this.conditionList(form, draft.conditions);
    form.append(el('h3', 'Триггеры'));
    for (const kind of ['voice', 'manual', 'schedule'] as const) {
      const trigger = draft.triggers.find(t => t.kind === kind);
      const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.checked = !!trigger;
      checkbox.onchange = () => {draft.triggers = draft.triggers.filter(t => t.kind !== kind); if (checkbox.checked) draft.triggers.push({kind, at: '22:00', weekdays: [0,1,2,3,4,5,6]}); this.dirty = true; this.renderEditor();};
      form.append(this.label({voice: 'По голосовой фразе', manual: 'Кнопка запуска / тестирования', schedule: 'По расписанию'}[kind], checkbox));
      if (trigger?.kind === 'schedule') {
        form.append(this.label('Время в часовом поясе Чушпана', this.input(trigger.at, v => trigger.at = v, 'time')));
        const days = el('div', '', 'button-row');
        ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].forEach((name, day) => {const check = el('input'); check.type = 'checkbox'; check.checked = trigger.weekdays.includes(day); check.onchange = () => {trigger.weekdays = check.checked ? [...trigger.weekdays, day] : trigger.weekdays.filter(d => d !== day); this.dirty = true;}; days.append(this.label(name, check));}); form.append(days);
      }
    }
    form.append(el('h3', 'Действия'), this.blocks(draft.actions));
    const controls = el('div', '', 'button-row'); const save = el('button', 'Сохранить'); save.type = 'submit'; save.className = 'primary'; save.disabled = !this.online;
    controls.append(save, this.button('Проверить распознавание', () => {const phrase = window.prompt('Фраза для проверки (ничего не выполняется)'); if (phrase) this.send({op: 'recognize', text: phrase});}));
    if (draft.id) controls.append(this.button('Тест: выполнить сохранённую версию', () => this.send({op: 'run', id: draft.id, revision: draft.revision})), this.button('Удалить сценарий', () => {if (window.confirm(`Удалить «${draft.name}»?`)) this.send({op: 'delete', id: draft.id, revision: draft.revision});}));
    form.append(controls, el('p', 'Тест выполняет реальные действия сохранённой версии. Изменения сначала нужно сохранить.', 'muted')); this.editor.append(form);
  }

  private conditionList(root: HTMLElement, conditions: Condition[]): void {
    conditions.forEach((condition, i) => {const row = el('div', '', 'scenario-condition'); row.append(this.conditionEditor(condition), this.button('Удалить условие', () => {conditions.splice(i, 1); this.dirty = true; this.renderEditor();})); root.append(row);});
    root.append(this.button('Добавить условие', () => {conditions.push({source: 'variable', key: this.draft?.variables[0]?.name ?? '', operator: 'eq', value: true}); this.dirty = true; this.renderEditor();}));
  }
  private conditionEditor(condition: Condition): HTMLElement {
    const row = el('div', '', 'condition-fields');
    row.append(this.label('Источник', this.select([['variable', 'Переменная'], ['available', 'Проверенная доступность'], ['time', 'Текущий час']], condition.source, v => {condition.source = v; condition.key = v === 'time' ? 'hour' : v === 'available' ? this.data?.capabilities[0]?.id ?? '' : this.draft?.variables[0]?.name ?? ''; condition.value = v === 'time' ? 22 : true; this.renderEditor();})));
    const keys: [string, string][] = condition.source === 'variable' ? (this.draft?.variables ?? []).map(v => [v.name, v.name]) : condition.source === 'available' ? (this.data?.capabilities ?? []).map(c => [c.id, c.name]) : [['hour', 'Час, 0–23']];
    row.append(this.label('Параметр', this.select(keys, condition.key, v => condition.key = v)), this.label('Сравнение', this.select([['eq', '='], ['ne', '≠'], ['gt', '>'], ['lt', '<']], condition.operator, v => condition.operator = v)), this.label('Значение', this.input(String(condition.value), v => {condition.value = v === 'true' ? true : v === 'false' ? false : v.trim() !== '' && Number.isFinite(Number(v)) ? Number(v) : v;})));
    return row;
  }
  private blocks(blocks: Block[]): HTMLElement {
    const root = el('div', '', 'scenario-blocks');
    blocks.forEach((block, index) => {
      const card = el('article', '', 'scenario-block'); card.draggable = true;
      card.ondragstart = event => {if ((event.target as HTMLElement).closest('input, select, textarea')) {event.preventDefault(); return;} event.stopPropagation(); this.dragSource = {blocks, index}; event.dataTransfer?.setData('text/plain', String(index));};
      card.ondragover = event => {event.preventDefault(); event.stopPropagation();};
      card.ondrop = event => {event.preventDefault(); event.stopPropagation(); const source = this.dragSource; this.dragSource = null; if (source?.blocks === blocks) {const [item] = blocks.splice(source.index, 1); blocks.splice(index, 0, item); this.dirty = true; this.renderEditor();}};
      const heading = el('div', '', 'section-heading');
      heading.append(el('strong', `${index + 1}. ${{action: 'Действие', delay: 'Задержка', sequence: 'Последовательность', parallel: 'Параллельно', if: 'IF / ELSE'}[block.kind]}`), this.button('↑', () => {moveBlock(blocks, index, -1); this.dirty = true; this.renderEditor();}), this.button('↓', () => {moveBlock(blocks, index, 1); this.dirty = true; this.renderEditor();}), this.button('Удалить блок', () => {blocks.splice(index, 1); this.dirty = true; this.renderEditor();})); card.append(heading);
      card.append(this.label('При ошибке', this.select([['stop', 'Остановить сценарий'], ['continue', 'Продолжить, сохранив ошибку']], block.on_error, v => block.on_error = v)));
      if (block.kind === 'action') {
        card.append(this.label('Действие устройства', this.select((this.data?.capabilities ?? []).map(c => [c.id, c.name]), block.capability, v => {block.capability = v; block.parameters = {}; this.dirty = true; this.renderEditor();})));
        const capability = this.data?.capabilities.find(c => c.id === block.capability);
        if (capability) {
          card.append(el('p', capability.description, 'muted'), el('small', `Доступ: ${capability.permissions.join(', ') || 'системные API'} · риск: ${capability.risk}`));
          for (const [key, schema] of Object.entries(capability.parameters)) card.append(this.parameter(block, key, schema, capability.required.includes(key)));
        } else card.append(el('p', 'Интеграция отключена или не подключена. Выбери доступное действие.', 'error'));
      } else if (block.kind === 'delay') {
        const seconds = this.input(String(block.seconds), v => block.seconds = Number(v), 'number'); seconds.min = '0'; seconds.max = '3600'; seconds.step = '.1'; card.append(this.label('Секунды', seconds));
      } else {
        if (block.kind === 'if') card.append(this.conditionEditor(block.condition!));
        if (block.kind === 'parallel') card.append(el('small', 'Ветки одного устройства запрещены. При ошибке ожидается завершение всех начатых веток.'));
        card.append(el('h4', block.kind === 'if' ? 'THEN · если условие выполнено' : 'Блоки'), this.blocks(block.children));
        if (block.kind === 'if') card.append(el('h4', 'ELSE · иначе'), this.blocks(block.otherwise));
      }
      root.append(card);
    });
    const toolbar = el('div', '', 'button-row');
    for (const [kind, name] of [['action', '+ Действие'], ['delay', '+ Задержка'], ['sequence', '+ Последовательность'], ['parallel', '+ Параллельно'], ['if', '+ IF / ELSE']] as const) toolbar.append(this.button(name, () => {blocks.push(newBlock(kind, this.data?.capabilities[0]?.id ?? '')); this.dirty = true; this.renderEditor();}));
    root.append(toolbar); return root;
  }
  private parameter(block: Block, key: string, schema: Parameter, required: boolean): HTMLElement {
    const row = el('div', '', 'parameter-row');
    const value = block.parameters[key];
    if (block.capability === 'shortcut.run' && key === 'name' && typeof value === 'string' && schema.enum) {
      const canonical = schema.enum.filter(v => typeof v === 'string' && v.toLowerCase() === value.toLowerCase());
      if (canonical.length === 1) block.parameters[key] = canonical[0];
    }
    const source = typeof value === 'object' && value !== null && !Array.isArray(value) ? String(value.variable) : 'literal';
    const choices: [string, string][] = [['literal', 'Значение'], ...(this.draft?.variables ?? []).map(v => [v.name, `Переменная: ${v.name}`] as [string, string])];
    row.append(this.label('Источник', this.select(choices, source, v => {if (v === 'literal') delete block.parameters[key]; else block.parameters[key] = {variable: v}; this.renderEditor();})));
    if (source !== 'literal') {row.append(el('strong', parameterNames[key] ?? key)); return row;}
    const assign = (v: string) => {if (v === '' && !required) delete block.parameters[key]; else block.parameters[key] = schema.type === 'integer' || schema.type === 'number' ? Number(v) : schema.type === 'boolean' ? v === 'true' : v;};
    let control: HTMLInputElement | HTMLSelectElement;
    if (schema.enum || schema.type === 'boolean') {
      const options = schema.enum ?? [true, false];
      control = this.select([['', required ? 'Выбери значение' : 'Не изменять'], ...options.map(v => [String(v), v === true ? 'Да' : v === false ? 'Нет' : String(v)] as [string, string])], block.parameters[key] === undefined ? '' : String(block.parameters[key]), assign);
    } else {
      control = this.input(value === undefined ? '' : String(value), assign, ['integer', 'number'].includes(schema.type) ? 'number' : 'text');
      if (schema.minimum !== undefined) control.min = String(schema.minimum);
      if (schema.maximum !== undefined) control.max = String(schema.maximum);
      if (schema.maxLength) control.maxLength = schema.maxLength;
      if (key === 'color') {control.placeholder = '#FF0020'; control.pattern = '#[0-9A-Fa-f]{6}'; control.oninput = () => {assign(control.value.toUpperCase()); this.dirty = true;};}
    }
    control.required = required;
    row.append(this.label((parameterNames[key] ?? key) + (required ? ' *' : ''), control)); return row;
  }

  private renderDevices(): void {
    this.deviceRoot.replaceChildren();
    for (const device of this.data?.devices ?? []) {
      const card = el('article', '', 'dashboard-card');
      card.append(el('h2', device.name), el('p', `${device.type} · ${status[device.status] ?? device.status}`), el('p', device.note), el('p', `Разрешения: ${device.permissions?.join(', ') || 'системные API'}`), el('p', device.error ?? '', 'error'));
      if (device.last_success) card.append(el('p', `Последняя успешная операция: ${device.last_operation} · ${new Date(device.last_success).toLocaleString()}`));
      if (device.actions.length) card.append(this.button(device.id === 'led' ? 'Прочитать сохранённый статус' : 'Проверить подключение', () => this.send({op: 'probe', device: device.id})));
      for (const id of device.actions) {
        const capability = this.data?.capabilities.find(c => c.id === id); if (!capability) continue;
        const details = el('details'); details.append(el('summary', capability.name), el('p', capability.description), el('p', `Разрешения: ${capability.permissions.join(', ') || 'системные API'}`));
        details.append(this.button('Настроить и протестировать действие', () => {this.draft = newScenario(); this.draft.name = capability.name; this.draft.actions = [newBlock('action', id)]; this.dirty = false; this.renderEditor(); (document.querySelector('[data-view="automations"]') as HTMLButtonElement).click();})); card.append(details);
      }
      if (device.details) {const details = el('details'); details.append(el('summary', 'Ответ интеграции'), el('pre', JSON.stringify(device.details, null, 2))); card.append(details);}
      this.deviceRoot.append(card);
    }
  }
  private renderPermissions(): void {
    this.permissionRoot.replaceChildren();
    if (!this.permissions) {this.permissionRoot.append(el('p', 'Нажми «Проверить разрешения». Непроверенный доступ не считается предоставленным.')); return;}
    this.permissionRoot.append(el('p', this.permissions.scope));
    if (this.permissions.error) this.permissionRoot.append(el('p', this.permissions.error, 'error'));
    for (const permission of this.permissions.items) {
      const card = el('article', '', 'dashboard-card'); const link = el('a', 'Открыть системные настройки');
      link.href = permission.url;
      card.append(el('h2', permission.name), el('strong', status[permission.status] ?? permission.status), el('p', permission.note), link); this.permissionRoot.append(card);
    }
  }
  private renderCommands(): void {
    const form = el('form', '', 'command-training');
    this.phraseInput = this.input(this.phraseText, v => this.phraseText = v); this.phraseInput.placeholder = 'Произнесённая команда';
    const recognize = el('button', 'Проверить понимание'); recognize.type = 'submit';
    form.onsubmit = event => {event.preventDefault(); if (this.phraseText.trim()) this.send({op: 'recognize', text: this.phraseText});};
    form.append(el('h2', 'Обучить Чушпана'), el('p', 'Проверь распознанный текст и выбери сценарий. Фраза сохранится только по кнопке.'), this.label('Фраза', this.phraseInput), recognize,
      this.button('Записать одну фразу · до 20 секунд', () => {this.send({op: 'capture', consent: true}); this.wake();}), this.button('Отменить запись', () => this.send({op: 'capture_cancel'})), this.recognition, this.label('Привязать к сценарию', this.trainingSelect),
      this.button('Сохранить фразу', () => {if (this.trainingSelect.value && this.phraseText.trim()) this.send({op: 'teach', id: this.trainingSelect.value, phrase: this.phraseText, consent: true});}));
    this.commandRoot.append(form, el('p', 'Распознавание использует локальный Whisper и существующий микрофонный поток. Аудиозапись не сохраняется. Для запуска голосом из режима ожидания обратись к Чушпану.', 'muted'));
  }
  private renderTraining(): void {
    this.trainingRoot.replaceChildren();
    for (const scenario of this.data?.scenarios ?? []) {
      if (!scenario.phrases.length) continue;
      const card = el('article', '', 'dashboard-card'); card.append(el('h2', scenario.name));
      for (const phrase of scenario.phrases) {
        const row = el('div', '', 'phrase-row'); row.append(el('p', phrase), this.button('Исправить', () => {const corrected = window.prompt('Исправь фразу', phrase); if (corrected) this.send({op: 'teach', id: scenario.id, old: phrase, phrase: corrected, consent: true});}), this.button('Удалить правило', () => this.send({op: 'teach', id: scenario.id, old: phrase, phrase: null, consent: true}))); card.append(row);
      }
      this.trainingRoot.append(card);
    }
    const audit = el('details'); audit.append(el('summary', 'История изменений обучения'));
    for (const entry of this.data?.training ?? []) audit.append(el('p', `${new Date(entry.created_at).toLocaleString()} · ${{add: 'Добавлено', edit: 'Изменено', delete: 'Удалено'}[entry.operation] ?? entry.operation} · ${entry.phrase}`));
    this.trainingRoot.append(audit);
  }
  private renderRuns(): void {
    this.runRoot.replaceChildren();
    for (const run of this.data?.runs ?? []) {
      const details = el('details', '', 'scenario-run');
      details.append(el('summary', `${run.name} · ${status[run.status] ?? run.status} · ${run.duration_s === undefined ? 'длительность ещё не измерена' : run.duration_s.toFixed(3) + ' с'} · ${new Date(run.started_at).toLocaleString()}`));
      if (run.error) details.append(el('p', run.error, 'error'));
      for (const step of run.steps) {const row = el('div'); row.append(el('p', `${step.path}. ${this.data?.capabilities.find(c => c.id === step.capability)?.name ?? step.kind} · ${status[step.status] ?? step.status} · ${step.duration_s?.toFixed(3) ?? '…'} с`)); if (step.result || step.error) row.append(el('pre', step.error ?? JSON.stringify(step.result, null, 2))); details.append(row);}
      this.runRoot.append(details);
    }
  }
}

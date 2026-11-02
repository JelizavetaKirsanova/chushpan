import type {HudEvent} from './types';
import {icon} from './dashboard';
import './automations.css';
import {addVariant, clone, newBlock, newId, newScenario, searchCommands, moveBlock} from './automation_state';
import type {Json, Block, Condition, Place, Scenario, Variant} from './automation_state';
export type {Scenario, Block, Condition} from './automation_state';
export {newBlock, newScenario, moveBlock} from './automation_state';

interface Parameter {type: string; enum?: Json[]; minimum?: number; maximum?: number; maxLength?: number; pattern?: string}
interface Capability {id: string; name: string; device: string; description: string; parameters: Record<string, Parameter>; required: string[]; permissions: string[]; risk: string; availability: string}
interface Recognition {scenario_id: string | null; name?: string; confidence: number; method: string; candidates: {id: string; name: string; confidence: number}[]}
interface Step {reason?: string; path: string; capability: string; kind: string; status: string; duration_s?: number; result?: Record<string, Json>; error?: string}
interface Run {variant_name?: string; place_name?: string; id: string; scenario_id: string; name: string; status: string; steps: Step[]; duration_s?: number; started_at: string; error?: string}
interface Device {id: string; name: string; type: string; status: string; actions: string[]; permissions: string[]; note: string; last_success?: string; last_operation?: string; error?: string; details?: Json}
interface Permission {id: string; name: string; status: string; note: string; url: string}
interface PreviewStep {path: string; kind: string; status: string; name?: string; capability: string; parameters?: Record<string, Json>; seconds?: number; reason?: string; condition_result?: boolean | null; risk?: string}
interface Preview {status: 'dry_run'; name: string; message: string; selection: {variant_name: string; place_name?: string} | null; steps: PreviewStep[]; conditions: (boolean | null)[]}
export interface AutomationsEvent {places: Place[]; location: {place_id: string | null; name: string | null; mode: 'auto' | 'manual'; status?: string; city?: string; message?: string}; revisions?: {id: string; items: {revision: number; name: string; created_at: string}[]}; preview?: Preview; place_saved?: string; type: 'automations'; scenarios: Scenario[]; capabilities: Capability[]; runs: Run[]; devices: Device[]; training: {id: number; scenario_id: string; phrase: string; operation: string; created_at: string}[]; permissions?: {items: Permission[]; error?: string; scope: string}; error?: string; message?: string; saved?: string; updated?: string; deleted?: string; recognition?: Recognition; captured?: string; result?: {message?: string; error?: string; status?: string}}

const el = <T extends keyof HTMLElementTagNameMap>(tag: T, value = '', className = ''): HTMLElementTagNameMap[T] => {
  const node = document.createElement(tag); node.textContent = value; node.className = className; return node;
};
const status: Record<string, string> = {confirmed: 'Проверено', sent: 'Отправлено, физический результат не подтверждён', unverified: 'Результат не подтверждён', failed: 'Ошибка', blocked: 'Заблокировано', interrupted: 'Прервано', running: 'Выполняется', skipped: 'Пропущено', not_checked: 'Не проверено', unavailable: 'Недоступно', connected: 'Подключено', observed: 'Ответ получен', error: 'Ошибка', granted: 'Предоставлено', denied: 'Не предоставлено', manual: 'Требуется ручная настройка', unsupported: 'Не поддерживается', unknown: 'Не удалось определить'};
const parameterNames: Record<string, string> = {level: 'Уровень, %', mute: 'Без звука', power: 'Питание', color: 'Цвет RGB', brightness: 'Яркость, %', brightness_delta: 'Изменение яркости', name: 'Название', input: 'Входной текст / URL', command: 'Музыкальная команда', action: 'Действие', query: 'Исполнитель, композиция или плейлист', direction: 'Изменение громкости', kind: 'Что включить', seconds: 'Секунды'};
const valueNames: Record<string,string> = {up:'Громче',down:'Тише',pause:'Пауза',resume:'Продолжить',stop:'Остановить',next:'Следующий трек',previous:'Предыдущий трек',open:'Открыть плеер',like:'Отметить понравившимся',shuffle_on:'Перемешивание: включить',shuffle_off:'Перемешивание: выключить',repeat_on:'Повтор: включить',repeat_off:'Повтор: выключить',repeat_one:'Повторять один трек',sleep:'Сон Mac',lock:'Блокировка экрана',shutdown:'Выключение Mac',track:'Композиция',artist:'Исполнитель',album:'Альбом',playlist:'Плейлист'};

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
  private activeVariant = '';
  private searchText = '';
  private placeRoot = el('div', '', 'place-settings');
  private locationRoot = el('div', '', 'location-selection');
  private previewRoot = el('div', '', 'command-preview');
  private revisionRoot = el('div', '', 'command-revisions');
  private lastDeleted: {id: string; revision: number} | null = null;
  private locationStamp = '';
  private placesDirty = false;


  constructor(private send: (value: object) => void, navigate: (view: string) => void, private wake: () => void, private chat: (text: string) => void) {
    this.feedback.setAttribute('role', 'status'); this.feedback.setAttribute('aria-live', 'polite');
    const page = document.querySelector<HTMLElement>('.automations-page')!;
    page.querySelector('h1')!.textContent = 'Мои команды';
    page.querySelector('.page-heading p')!.textContent = 'Одна команда — общие фразы и независимые версии для дома, общаги и других мест.';
    const toolbar = el('div', '', 'page-toolbar');
    const search = el('input'); search.type = 'search'; search.placeholder = 'Найти команду или фразу'; search.setAttribute('aria-label', 'Поиск моих команд'); search.oninput = () => {this.searchText = search.value; this.renderList();};
    toolbar.append(this.button('Новая команда', () => {if (!this.discardAllowed()) return; this.draft = newScenario(); this.activeVariant = ''; this.dirty = false; this.renderEditor();}), search, this.button('Обновить', () => this.send({op: 'refresh'})), this.button('Остановить ожидание', () => this.send({op: 'cancel'})));
    const layout = el('div', '', 'scenario-layout'); layout.append(this.list, this.editor);
    const history = el('details'); history.append(el('summary', 'История запусков'), this.runRoot);
    const places = el('details'); places.append(el('summary', 'Места и автоматический выбор версии'), this.placeRoot);
    page.querySelector('.page-heading')!.after(toolbar, this.feedback, this.locationRoot, places, layout, this.previewRoot, this.revisionRoot, history);
    const old = page.querySelector('.integration-grid');
    if (old) {const details = el('details', '', 'legacy-ritual'); details.append(el('summary', 'Встроенные сценарии и комната')); old.before(details); details.append(old);}
    const scenarioNav = document.querySelector<HTMLButtonElement>('[data-view="automations"]')!;
    scenarioNav.querySelector('span:last-of-type')!.textContent = 'Мои команды';
    scenarioNav.title = 'Мои команды'; scenarioNav.setAttribute('aria-label', 'Мои команды');
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
    if (event.type === 'dashboard') {const stamp = JSON.stringify(event.location); if (stamp !== this.locationStamp) {this.locationStamp = stamp; if (this.online) this.send({op: 'refresh'});} return;}
    if (event.type !== 'automations') return;
    if (!Array.isArray(event.places) || !event.location) {
      this.online = false;
      this.feedback.textContent = 'Для загрузки обновления один раз перезапусти Чушпана. После этого команды и версии будут применяться без перезапуска.';
      document.querySelectorAll<HTMLButtonElement>('.automations-page button, .automation-section button').forEach(button => button.disabled = true);
      this.updateFeedback(); return;
    }
    this.data = event;
    if (event.permissions) this.permissions = event.permissions;
    if (event.saved && (!this.dirty || this.saving)) {
      this.draft = clone(event.scenarios.find(s => s.id === event.saved) ?? newScenario());
      this.dirty = false; this.saving = false; this.renderEditor();
    } else if (event.deleted && event.deleted === this.draft?.id) {this.draft = null; this.dirty = false; this.activeVariant = ''; this.editor.replaceChildren(); this.previewRoot.replaceChildren();}
    else if (event.updated && event.updated === this.draft?.id && !this.dirty) {this.draft = clone(event.scenarios.find(s => s.id === event.updated)!); this.renderEditor();}
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
    if (event.place_saved) this.placesDirty = false;
    this.renderLocation(); this.renderPlaces();
    if (event.preview) this.renderPreview(event.preview);
    if (event.revisions) this.renderRevisions(event.revisions);
    this.renderList(); this.renderDevices(); this.renderPermissions(); this.renderTraining(); this.renderRuns();
    const selected = this.trainingSelect.value;
    this.trainingSelect.replaceChildren(...event.scenarios.map(s => {const option = el('option', s.name); option.value = s.id; return option;}));
    if (event.scenarios.some(s => s.id === selected)) this.trainingSelect.value = selected;
  }
  private updateFeedback(): void {
    for (const node of this.sectionFeedback) {node.textContent = this.feedback.textContent; node.dataset.error = this.feedback.dataset.error ?? '';}
  }

  private discardAllowed(): boolean {return !this.dirty || window.confirm('Отменить несохранённые изменения команды?');}

  private renderLocation(): void {
    if (!this.data) return;
    const location = this.data.location;
    this.locationRoot.replaceChildren(el('strong', `Сейчас: ${location.name ?? 'место не определено'}`));
    this.locationRoot.append(this.label('Выбор места для голосовых команд', this.select(
      [['auto', 'Определять автоматически по текущему городу'], ...this.data.places.map(p => [p.id, p.name] as [string, string])],
      location.mode === 'auto' ? 'auto' : location.place_id ?? 'auto',
      id => this.send({op: 'location', mode: id === 'auto' ? 'auto' : 'manual', place_id: id === 'auto' ? '' : id}))),
      el('small', location.mode === 'auto' ? (location.city ? `Наблюдаемый город: ${location.city}` : 'Используется уже подключённое определение города. При неизвестном месте выбери его вручную.') : 'Ручной выбор сохранён и действует до переключения в автоматический режим.'));
    if (this.lastDeleted) this.locationRoot.append(this.button('Отменить удаление команды', () => {const deleted = this.lastDeleted!; this.saving = true; this.send({op: 'restore', id: deleted.id, from_revision: deleted.revision, expected_revision: 0}); this.lastDeleted = null;}));
  }

  private renderPlaces(): void {
    if (this.placesDirty) return;
    this.placeRoot.replaceChildren();
    for (const place of this.data?.places ?? []) this.placeRoot.append(this.placeForm(clone(place)));
    this.placeRoot.append(this.button('Добавить своё место', () => {this.placesDirty = true; this.placeRoot.append(this.placeForm({id: newId(), revision: 0, name: 'Новое место', cities: []}));}), this.button('Отменить изменения мест', () => {this.placesDirty = false; this.renderPlaces();}));
  }

  private placeForm(place: Place): HTMLElement {
    const form = el('div', '', 'place-card');
    const name = el('input'); name.value = place.name; name.maxLength = 120; name.oninput = () => {place.name = name.value; this.placesDirty = true;};
    const cities = el('textarea'); cities.value = place.cities.join('\n'); cities.rows = 2;
    cities.placeholder = 'Tallinn\nТаллин'; cities.oninput = () => {place.cities = cities.value.split('\n').map(v => v.trim()).filter(Boolean); this.placesDirty = true;};
    form.append(this.label('Название места', name), this.label('Города для автоматического выбора — каждый с новой строки', cities),
      this.button('Сохранить место', () => this.send({op: 'place_save', place})),
      this.button(place.revision ? 'Удалить место' : 'Отменить добавление', () => {if (place.revision) this.send({op: 'place_delete', id: place.id, revision: place.revision}); else form.remove();}));
    return form;
  }

  private variantEditor(draft: Scenario): HTMLElement {
    const root = el('section', '', 'variant-editor');
    root.append(el('h3', 'Общие действия и версии для мест'), el('p', 'Общие шаги выполняются первыми. Затем — шаги версии, выбранной по текущему месту. Голосовые фразы принадлежат всей команде.', 'muted'));
    const tabs = el('div', '', 'variant-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Версии команды');
    const variants = draft.variants;
    if (!variants.some(v => v.id === this.activeVariant)) this.activeVariant = '';
    for (const item of [{id: '', name: 'Общие шаги'}, ...variants]) {
      const button = this.button(item.name, () => {this.activeVariant = item.id; this.renderEditor();});
      button.setAttribute('role', 'tab'); button.setAttribute('aria-selected', String(this.activeVariant === item.id)); tabs.append(button);
    }
    root.append(tabs);
    const variant = variants.find(v => v.id === this.activeVariant);
    if (variant) {
      const fields = el('div', '', 'variant-fields');
      const enabled = el('input'); enabled.type = 'checkbox'; enabled.checked = variant.enabled; enabled.onchange = () => {variant.enabled = enabled.checked; this.dirty = true;};
      fields.append(this.label('Название версии', this.input(variant.name, v => variant.name = v)),
        this.label('Место этой версии', this.select((this.data?.places ?? []).filter(p => p.id === variant.place_id || !variants.some(v => v.place_id === p.id)).map(p => [p.id, p.name]), variant.place_id, v => variant.place_id = v)), this.label('Версия включена', enabled),
        this.button('Удалить версию', () => {if (window.confirm(`Удалить версию «${variant.name}» из черновика?`)) {draft.variants = variants.filter(v => v.id !== variant.id); this.activeVariant = ''; this.dirty = true; this.renderEditor();}}));
      root.append(fields, el('h4', 'Условия этой версии')); this.conditionList(root, variant.conditions);
      root.append(el('h4', `Шаги версии «${variant.name}»`), this.blocks(variant.actions));
    } else root.append(el('h4', 'Общие шаги для всех мест'), this.blocks(draft.actions));
    const available = (this.data?.places ?? []).filter(p => !draft.variants.some(v => v.place_id === p.id));
    const additions = el('div', '', 'variant-add');
    if (available.length) {
      let placeId = available[0].id;
      const target = this.select(available.map(p => [p.id, p.name]), placeId, v => placeId = v);
      const create = (source?: Variant) => {const place = available.find(p => p.id === placeId)!; const added = addVariant(draft, place, source); this.activeVariant = added.id; this.dirty = true; this.renderEditor();};
      additions.append(this.label(variant ? 'Место для новой версии или копии' : 'Место для новой версии', target), this.button('Добавить пустую версию', () => create()));
      if (variant) additions.append(this.button('Скопировать версию в другое место', () => create(variant)));
    } else additions.append(el('p', 'Для всех мест уже есть версии. Дополнительные места можно добавить в настройках выше.', 'muted'));
    root.append(additions);
    if (variants.length && !variant) root.append(el('p', 'Чтобы проверить определённую версию без выполнения, открой её вкладку и нажми «Безопасный тест черновика».', 'muted'));
    return root;
  }

  private renderPreview(preview: Preview): void {
    this.previewRoot.replaceChildren(el('h2', `Предварительный просмотр: ${preview.name}`), el('p', preview.message));
    this.previewRoot.append(el('strong', preview.selection ? `Версия: ${preview.selection.variant_name}` : 'Общие шаги'));
    for (const condition of preview.conditions) this.previewRoot.append(el('p', condition === null ? 'Условие не определено: состояние устройства не проверено' : condition ? 'Условие запуска выполнено' : 'Условие запуска не выполнено; реальный запуск пропустит команду'));
    for (const step of preview.steps) {
      const card = el('article', '', 'preview-step');
      card.append(el('strong', `${step.path}. ${step.name ?? {delay: 'Задержка', parallel: 'Параллельно', sequence: 'Последовательность', if: 'IF / ELSE'}[step.kind] ?? step.capability}`), el('small', step.status === 'skipped' ? step.reason ?? 'Пропущено' : 'Запланировано; устройство не вызывалось'));
      if (step.seconds !== undefined) card.append(el('p', `${step.seconds} секунд (ожидание не запускается)`));
      if (step.condition_result !== undefined) card.append(el('p', `Условие IF: ${step.condition_result === null ? 'неизвестно; показаны обе ветки' : step.condition_result ? 'THEN' : 'ELSE'}`));
      for (const [key, value] of Object.entries(step.parameters ?? {})) card.append(el('p', `${parameterNames[key] ?? key}: ${value === true ? 'Да' : value === false ? 'Нет' : String(value)}`));
      if (step.risk === 'dangerous') card.append(el('p', 'Реальное выполнение потребует отдельного подтверждения.', 'error'));
      this.previewRoot.append(card);
    }
    this.previewRoot.scrollIntoView({block: 'start', behavior: 'smooth'});
  }

  private renderRevisions(revisions: NonNullable<AutomationsEvent['revisions']>): void {
    this.revisionRoot.replaceChildren(el('h3', 'Сохранённые редакции команды'));
    const current = this.data?.scenarios.find(s => s.id === revisions.id);
    for (const revision of revisions.items) {
      const row = el('div', '', 'phrase-row');
      row.append(el('span', `Редакция ${revision.revision} · ${new Date(revision.created_at).toLocaleString()}`), this.button('Восстановить редакцию', () => {if (this.discardAllowed()) {this.saving = true; this.send({op: 'restore', id: revisions.id, from_revision: revision.revision, expected_revision: current?.revision ?? 0});}})); this.revisionRoot.append(row);
    }
  }

  private renderList(): void {
    this.list.replaceChildren();
    const matches = searchCommands(this.data?.scenarios ?? [], this.searchText);
    for (const scenario of matches) {
      const card = el('article', '', 'scenario-card');
      card.append(el('h2', `${scenario.icon} ${scenario.name}`), el('p', scenario.description), el('small', `${scenario.enabled ? 'Включён' : 'Выключен'} · ${scenario.phrases.length} фраз · ${scenario.variants.length} версий по местам · редакция ${scenario.revision}`));
      const actions = el('div', '', 'button-row');
      actions.append(this.button('Редактировать', () => {
        if (this.dirty && !window.confirm('Отбросить несохранённые изменения?')) return;
        this.draft = clone(scenario); this.activeVariant = ''; this.dirty = false; this.previewRoot.replaceChildren(); this.revisionRoot.replaceChildren(); this.renderEditor();
      }), this.button('Копировать', () => {if (this.discardAllowed()) {this.saving = true; this.send({op: 'copy', id: scenario.id});}}), this.button('Запустить', () => this.send({op: 'run', id: scenario.id, revision: scenario.revision})));
      const run = actions.lastElementChild as HTMLButtonElement;
      run.disabled ||= !scenario.enabled || !scenario.triggers.some(t => t.kind === 'manual');
      card.append(actions); this.list.append(card);
    }
    if (!matches.length) this.list.append(el('p', this.searchText ? 'Команд не найдено.' : 'Создай первую команду.'));
  }

  private renderEditor(): void {
    const draft = this.draft;
    this.editor.replaceChildren();
    if (!draft) return;
    const form = el('form'); form.onsubmit = event => {event.preventDefault(); if (!this.online || this.saving) return; this.saving = true; this.feedback.textContent = 'Сохраняю…'; this.send({op: 'save', scenario: draft});};
    const heading = el('div', '', 'section-heading'); heading.append(el('h2', 'Редактор команды'), el('small', draft.id ? `Редакция ${draft.revision}` : 'Черновик'));
    if (draft.id) heading.append(this.button('История редакций', () => this.send({op: 'revisions', id: draft.id})));
    heading.append(this.button('Отменить изменения', () => {if (!this.discardAllowed()) return; const saved = this.data?.scenarios.find(s => s.id === draft.id); this.draft = saved ? clone(saved) : newScenario(); this.dirty = false; this.activeVariant = ''; this.renderEditor();}));
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
      const control = variable.kind === 'boolean' ? this.select([['true','Да'],['false','Нет']],String(variable.default), v => variable.default = v === 'true') : this.input(String(variable.default), v => variable.default = variable.kind === 'number' ? Number(v) : v, variable.kind === 'number' ? 'number' : 'text');
      row.append(this.label('Имя', this.input(variable.name, v => variable.name = v)), this.label('Тип', this.select([['number', 'Число'], ['string', 'Текст'], ['boolean', 'Да / нет']], variable.kind, v => {variable.kind = v; variable.default = v === 'number' ? 50 : v === 'boolean' ? true : ''; this.renderEditor();})), this.label('Значение', control), this.button('Удалить', () => {draft.variables.splice(draft.variables.indexOf(variable), 1); this.dirty = true; this.renderEditor();})); form.append(row);
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
    form.append(this.variantEditor(draft));
    const controls = el('div', '', 'button-row'); const save = el('button', 'Применить команду'); save.type = 'submit'; save.className = 'primary'; save.disabled = !this.online;
    controls.append(save, this.button('Безопасный тест черновика', () => this.send({op: 'preview', scenario: draft, variant: this.activeVariant})), this.button('Проверить распознавание', () => {const phrase = window.prompt('Фраза для проверки (ничего не выполняется)'); if (phrase) this.send({op: 'recognize', text: phrase});}));
    if (draft.id) controls.append(this.button('Выполнить сохранённую команду', () => this.send({op: 'run', id: draft.id, revision: draft.revision})), this.button('Удалить команду', () => {if (window.confirm(`Удалить «${draft.name}»?`)) {this.lastDeleted = {id: draft.id, revision: draft.revision}; this.send({op: 'delete', id: draft.id, revision: draft.revision});}}));
    form.append(controls, el('p', 'Безопасный тест показывает план без обращения к устройствам. «Выполнить» запускает сохранённую команду и выбирает версию по текущему месту. Применение сразу обновляет голосовые команды.', 'muted')); this.editor.append(form);
  }

  private conditionList(root: HTMLElement, conditions: Condition[]): void {
    conditions.forEach((condition, i) => {const row = el('div', '', 'scenario-condition'); row.append(this.conditionEditor(condition), this.button('Удалить условие', () => {conditions.splice(i, 1); this.dirty = true; this.renderEditor();})); root.append(row);});
    root.append(this.button('Добавить условие', () => {conditions.push({source: 'variable', key: this.draft?.variables[0]?.name ?? '', operator: 'eq', value: true}); this.dirty = true; this.renderEditor();}));
  }
  private conditionEditor(condition: Condition): HTMLElement {
    const row = el('div', '', 'condition-fields');
    row.append(this.label('Источник', this.select([['variable', 'Переменная'], ['available', 'Проверенная доступность'], ['time', 'Текущий час']], condition.source, v => {condition.source = v; condition.key = v === 'time' ? 'hour' : v === 'available' ? this.data?.capabilities[0]?.id ?? '' : this.draft?.variables[0]?.name ?? ''; condition.value = v === 'time' ? 22 : true; this.renderEditor();})));
    const keys: [string, string][] = condition.source === 'variable' ? (this.draft?.variables ?? []).map(v => [v.name, v.name]) : condition.source === 'available' ? (this.data?.capabilities ?? []).map(c => [c.id, c.name]) : [['hour', 'Час, 0–23']];
    const variable = this.draft?.variables.find(v => v.name === condition.key);
    const kind = condition.source === 'available' ? 'boolean' : condition.source === 'time' ? 'number' : variable?.kind ?? 'string';
    const comparison: [Condition['operator'],string][] = kind === 'boolean' ? [['eq','='],['ne','≠']] : [['eq','='],['ne','≠'],['gt','>'],['lt','<']];
    const control = kind === 'boolean' ? this.select([['true','Да'],['false','Нет']],String(condition.value),v => condition.value = v === 'true') : this.input(String(condition.value), v => condition.value = kind === 'number' ? Number(v) : v, kind === 'number' ? 'number' : 'text');
    row.append(this.label('Параметр', this.select(keys, condition.key, v => {condition.key = v; condition.value = this.draft?.variables.find(item => item.name === v)?.default ?? (condition.source === 'time' ? 22 : true); condition.operator = 'eq'; this.renderEditor();})), this.label('Сравнение', this.select(comparison, condition.operator, v => condition.operator = v)), this.label('Значение', control));
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
      const enabled = el('input'); enabled.type = 'checkbox'; enabled.checked = block.enabled; enabled.onchange = () => {block.enabled = enabled.checked; this.dirty = true; this.renderEditor();}; card.dataset.enabled = String(block.enabled); card.append(this.label('Шаг включён', enabled));
      if (block.kind === 'action') card.append(this.label('Доступность устройства', this.select([['required', 'Обязательный шаг'], ['optional', 'Пропустить, если устройство отсутствует']], block.required ? 'required' : 'optional', v => block.required = v === 'required')));
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
      control = this.select([['', required ? 'Выбери значение' : 'Не изменять'], ...options.map(v => [String(v), v === true ? 'Да' : v === false ? 'Нет' : valueNames[String(v)] ?? String(v)] as [string, string])], block.parameters[key] === undefined ? '' : String(block.parameters[key]), assign);
    } else {
      control = this.input(value === undefined ? '' : String(value), assign, ['integer', 'number'].includes(schema.type) ? 'number' : 'text');
      if (schema.minimum !== undefined) control.min = String(schema.minimum);
      if (schema.maximum !== undefined) control.max = String(schema.maximum);
      if (schema.maxLength) control.maxLength = schema.maxLength;
      if (key === 'color') {control.placeholder = '#FF0020'; control.pattern = '#[0-9A-Fa-f]{6}'; control.oninput = () => {assign(control.value.toUpperCase()); this.dirty = true;};}
    }
    const active = this.draft?.variants.find(v => v.id === this.activeVariant);
    control.required = required && block.enabled && (active?.enabled ?? true);
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
      details.append(el('summary', `${run.name}${run.variant_name ? ' / ' + run.variant_name : ''} · ${status[run.status] ?? run.status} · ${run.duration_s === undefined ? 'длительность ещё не измерена' : run.duration_s.toFixed(3) + ' с'} · ${new Date(run.started_at).toLocaleString()}`));
      if (run.error) details.append(el('p', run.error, 'error'));
      for (const step of run.steps) {const row = el('div'); row.append(el('p', `${step.path}. ${this.data?.capabilities.find(c => c.id === step.capability)?.name ?? step.kind} · ${status[step.status] ?? step.status} · ${step.duration_s?.toFixed(3) ?? '…'} с`)); if (step.reason) row.append(el('p', {disabled: 'Шаг выключен', unavailable: 'Устройство недоступно; необязательный шаг пропущен'}[step.reason] ?? step.reason)); if (step.result || step.error) row.append(el('pre', step.error ?? JSON.stringify(step.result, null, 2))); details.append(row);}
      this.runRoot.append(details);
    }
  }
}

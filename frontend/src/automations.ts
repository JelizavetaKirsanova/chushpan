import type {HudEvent} from './types';
import './automations.css';
import {addVariant, clone, newBlock, newId, newScenario, searchCommands, moveBlock, reorderBlock, countBlocks, normalizePhrase} from './automation_state';
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
interface PreviewStep {delay_before_s?: number; availability?: string; device?: string; path: string; kind: string; status: string; name?: string; capability: string; parameters?: Record<string, Json>; seconds?: number; reason?: string; condition_result?: boolean | null; risk?: string}
interface Preview {errors?: string[]; timeout_s?: number | null; status: 'dry_run'; name: string; message: string; selection: {variant_name: string; place_name?: string} | null; steps: PreviewStep[]; conditions: (boolean | null)[]}
export interface AutomationsEvent {editor_version?: number; places: Place[]; location: {place_id: string | null; name: string | null; mode: 'auto' | 'manual'; status?: string; city?: string; message?: string}; revisions?: {id: string; items: {revision: number; name: string; created_at: string}[]}; preview?: Preview; place_saved?: string; type: 'automations'; scenarios: Scenario[]; capabilities: Capability[]; runs: Run[]; devices: Device[]; training: {id: number; scenario_id: string; phrase: string; operation: string; created_at: string}[]; permissions?: {items: Permission[]; error?: string; scope: string}; error?: string; message?: string; saved?: string; updated?: string; deleted?: string; recognition?: Recognition; captured?: string; result?: {message?: string; error?: string; status?: string}}

const el = <T extends keyof HTMLElementTagNameMap>(tag: T, value = '', className = ''): HTMLElementTagNameMap[T] => {
  const node = document.createElement(tag); node.textContent = value; node.className = className; return node;
};
const status: Record<string, string> = {confirmed: 'Проверено', sent: 'Отправлено, физический результат не подтверждён', unverified: 'Результат не подтверждён', failed: 'Ошибка', blocked: 'Заблокировано', interrupted: 'Прервано', running: 'Выполняется', skipped: 'Пропущено', not_checked: 'Не проверено', unavailable: 'Недоступно', connected: 'Подключено', observed: 'Ответ получен', error: 'Ошибка', granted: 'Предоставлено', denied: 'Не предоставлено', manual: 'Требуется ручная настройка', unsupported: 'Не поддерживается', unknown: 'Не удалось определить'};
const parameterNames: Record<string, string> = {level: 'Уровень, %', site: 'Сайт', mute: 'Без звука', power: 'Питание', color: 'Цвет', brightness: 'Яркость, %', brightness_delta: 'Изменение яркости', name: 'Название', input: 'Входной текст / URL', command: 'Музыкальная команда', action: 'Действие', query: 'Исполнитель, композиция или плейлист', direction: 'Изменение громкости', kind: 'Что включить', seconds: 'Секунды'};
const valueNames: Record<string,string> = {switch:'Переключиться',quit:'Закрыть',youtube:'YouTube',up:'Громче',down:'Тише',pause:'Пауза',resume:'Продолжить',stop:'Остановить',next:'Следующий трек',previous:'Предыдущий трек',open:'Открыть плеер',like:'Отметить понравившимся',shuffle_on:'Перемешивание: включить',shuffle_off:'Перемешивание: выключить',repeat_on:'Повтор: включить',repeat_off:'Повтор: выключить',repeat_one:'Повторять один трек',sleep:'Сон Mac',lock:'Блокировка экрана',shutdown:'Выключение Mac',track:'Композиция',artist:'Исполнитель',album:'Альбом',playlist:'Плейлист'};

export class AutomationsView {
  private data: AutomationsEvent | null = null;
  private online = false;
  private draft: Scenario | null = null;
  private dirty = false;
  private saving = false;
  private running = false;
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
  private permissions: AutomationsEvent['permissions'];
  private dragSource: {blocks: Block[]; index: number} | null = null;
  private sectionFeedback: HTMLElement[] = [];
  private activeVariant = '';
  private searchText = '';
  private placeRoot = el('div', '', 'place-settings');
  private locationRoot = el('div', '', 'location-selection');
  private editorLocationRoot = el('div', '', 'editor-location-current');
  private previewRoot = el('div', '', 'command-preview');
  private revisionRoot = el('div', '', 'command-revisions');
  private lastDeleted: {id: string; revision: number} | null = null;
  private locationStamp = '';
  private placesDirty = false;
  private advancedOpen = false;
  private openedBlocks = new WeakSet<Block>();
  private selectedDevice = '';
  private capturing = false;
  private toggling = new Set<string>();
  private legacyControls = el('div');
  private placeFilter = '';
  private pendingPhrase = '';
  private executionRoot = el('div', '', 'scenario-execution');
  private dialogueStatus = el('div', '', 'scenario-dialogue-status');



  constructor(private send: (value: object) => void, private navigate: (view: string) => void, private wake: () => void, private chat: (text: string) => void) {
    this.feedback.setAttribute('role', 'status'); this.feedback.setAttribute('aria-live', 'polite');
    const page = document.querySelector<HTMLElement>('.automations-page')!;
    page.querySelector('h1')!.textContent = 'Сценарии';
    page.querySelector('.page-heading p')!.textContent = 'Скажи одну фразу — Чушпан сделает остальное.';
    const toolbar = el('div', '', 'page-toolbar');
    const search = el('input'); search.type = 'search'; search.placeholder = 'Найти сценарий'; search.setAttribute('aria-label', 'Поиск моих команд'); search.oninput = () => {this.searchText = search.value; this.renderList();};
    const create = this.button('+ Создать сценарий', () => {if (!this.discardAllowed()) return; this.draft = newScenario(); this.draft.name = ''; this.pendingPhrase = ''; this.activeVariant = ''; this.dirty = false; this.advancedOpen = false; this.previewRoot.replaceChildren(); this.revisionRoot.replaceChildren(); this.renderEditor();}); create.className = 'primary';
    const placeFilter = this.select([['', 'Все места'], ['general', 'Общий'], ...(this.data?.places ?? []).map(p => [p.id, p.name] as [string, string])], '', value => {this.placeFilter = value; this.renderList();}, false);
    placeFilter.setAttribute('aria-label', 'Фильтр по местоположению'); placeFilter.id = 'scenario-place-filter';
    toolbar.append(search, placeFilter, create);
    const layout = el('div', '', 'scenario-layout'); layout.append(this.list, this.editor);
    page.querySelector('.page-heading')!.after(toolbar, this.feedback, layout);
    const old = page.querySelector('.integration-grid');
    if (old) {const details = el('details', '', 'legacy-ritual'); details.append(el('summary', 'Встроенные сценарии'), old); page.append(details);}
    const devicePage = document.querySelector<HTMLElement>('.smart-home-page')!;
    this.legacyControls.append(...devicePage.querySelectorAll('.led-card'));
    devicePage.className = 'workspace-page automation-section devices-page'; devicePage.setAttribute('aria-label', 'Устройства');
    devicePage.replaceChildren(el('div', '', 'page-heading'), this.deviceRoot);
    devicePage.firstElementChild!.append(el('h1', 'Устройства'), el('p', 'Подключения и доступные действия.'));
    const settings = document.querySelector('.settings-page')!;
    settings.querySelector('#settings-items')!.closest('details')!.append(document.querySelector('.bottom')!);
    for (const card of settings.querySelectorAll<HTMLElement>(':scope > form, :scope > section.settings-card')) {
      const title = card.querySelector('h2'); if (!title) continue;
      const detail = el('details', '', 'settings-card'); detail.append(el('summary', title.textContent ?? 'Параметры'));
      card.before(detail); detail.append(card); card.classList.remove('settings-card'); title.hidden = true;
    }
    const places = el('details', '', 'settings-card'); places.append(el('summary', 'Места и версии сценариев'), this.locationRoot, this.placeRoot);
    const permissions = el('details', '', 'settings-card permission-settings'); permissions.append(el('summary', 'Разрешения'), el('p', 'Доступ macOS для приложения, в котором работает Чушпан.', 'muted'), this.button('Проверить разрешения', () => this.send({op: 'permissions'})), this.permissionRoot);
    const feedback = el('p', '', 'scenario-feedback'); feedback.setAttribute('role', 'status'); feedback.setAttribute('aria-live', 'polite'); permissions.append(feedback); this.sectionFeedback.push(feedback);
    settings.querySelector('.page-heading')!.after(permissions, places);
    const history = el('section', '', 'scenario-history'); history.append(el('h2', 'История сценариев'), this.runRoot);
    document.querySelector('.performance-page .page-heading')!.after(history);
    document.querySelector('.talk')?.append(this.dialogueStatus);
    window.addEventListener('beforeunload', event => {if (this.dirty) {event.preventDefault(); event.returnValue = '';}});
    document.addEventListener('workspace:navigate', event => {
      if ((event as CustomEvent<string>).detail !== 'automations' && !this.discardAllowed()) event.preventDefault();
    });
    this.renderEditor();
  }

  private button(label: string, action: () => void): HTMLButtonElement {
    const button = el('button', label); button.type = 'button'; button.disabled = !this.online;
    button.onclick = action; return button;
  }
  private input(value: string, change: (value: string) => void, type = 'text'): HTMLInputElement {
    const input = el('input'); input.type = type; input.value = value;
    input.oninput = () => {change(input.value); this.dirty = true;}; return input;
  }
  private select<T extends string>(values: [T, string][], current: T, change: (value: T) => void, markDirty = true): HTMLSelectElement {
    const select = el('select');
    for (const [value, label] of values) {const option = el('option', label); option.value = value; select.append(option);}
    select.value = current; select.onchange = () => {if (markDirty) this.dirty = true; change(select.value as T);}; return select;
  }
  private label(name: string, control: HTMLElement): HTMLLabelElement {const label = el('label'); label.append(el('span', name), control); return label;}
  connection(online: boolean): void {
    this.online = online;
    if (!online) this.saving = false;
    document.querySelectorAll<HTMLButtonElement | HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>('.scenario-layout button, .automation-section button, .automations-page > .page-toolbar button, .scenario-layout input, .scenario-layout select, .scenario-layout textarea, .automation-section input, .automation-section select, .permission-settings button, .place-settings button, .location-selection select').forEach(control => {
      if (!online) {control.dataset.wasDisabled ??= String(control.disabled); control.disabled = true;}
      else if (control.dataset.wasDisabled !== undefined) {control.disabled = control.dataset.wasDisabled === 'true'; delete control.dataset.wasDisabled;}
      else if (control.tagName === 'BUTTON') control.disabled = false;
    });
    this.feedback.textContent = online ? '' : 'Нет соединения. Сохранение и выполнение недоступны.';
    this.updateFeedback();
    if (online) this.send({op: 'refresh'});
  }
  receive(event: HudEvent): void {
    if (event.type === 'dashboard') {const stamp = JSON.stringify(event.location); if (stamp !== this.locationStamp) {this.locationStamp = stamp; if (this.online) this.send({op: 'refresh'});} return;}
    if (event.type !== 'automations') return;
    if (!Array.isArray(event.places) || !event.location || event.editor_version !== 2) {
      this.online = false;
      this.feedback.textContent = 'Для загрузки обновления один раз перезапусти Чушпана. После этого команды и версии будут применяться без перезапуска.';
      document.querySelectorAll<HTMLButtonElement | HTMLSelectElement>('.automations-page button, .automation-section button, .permission-settings button, .place-settings button, .location-selection select').forEach(control => control.disabled = true);
      this.updateFeedback(); return;
    }
    this.data = event;
    if (event.result || event.error) this.running = false;
    if (event.permissions) this.permissions = event.permissions;
    if (event.saved && this.toggling.delete(event.saved)) {
      if (this.draft?.id === event.saved && !this.dirty) {this.draft = clone(event.scenarios.find(s => s.id === event.saved)!); this.renderEditor();}
    } else if (event.saved && (!this.dirty || this.saving)) {
      this.draft = this.document(event.scenarios.find(s => s.id === event.saved) ?? newScenario());
      this.dirty = false; this.saving = false; this.renderEditor();
    } else if (event.deleted && event.deleted === this.draft?.id) {this.draft = null; this.dirty = false; this.activeVariant = ''; this.previewRoot.replaceChildren(); this.renderEditor();}
    else if (event.updated && event.updated === this.draft?.id && !this.dirty) {this.draft = clone(event.scenarios.find(s => s.id === event.updated)!); this.renderEditor();}
    if (event.error) {const wasSaving = this.saving; this.saving = false; if (wasSaving) this.renderEditor(); this.toggling.clear(); this.feedback.replaceChildren(el('span', /фраз|конфликт|дубликат|уже.*использ/i.test(event.error) ? 'Эта фраза уже используется. Измени её или выбери другую.' : 'Не удалось применить изменение. Проверь заполненные поля и попробуй снова.')); const details = el('details'); details.append(el('summary', 'Подробнее'), el('p', event.error)); this.feedback.append(details); this.feedback.dataset.error = 'true';}
    else if (event.message) {this.feedback.textContent = event.message; delete this.feedback.dataset.error;}
    if (event.captured !== undefined) {
      this.capturing = false;
      this.phraseText = event.captured;
      this.renderCommands();
    }
    if (event.recognition) {
      const r = event.recognition;
      this.recognition.replaceChildren(el('strong', r.name ? `Понятно: «${r.name}»` : r.method === 'ambiguous' ? 'Подходит несколько сценариев. Попробуй более точную фразу.' : 'Пока не понимаю эту фразу. Добавь её к сценарию.'));
      this.recognition.append(el('small', 'Это проверка сохранённых фраз. Действия не запускаются.'));
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

  }
  private updateFeedback(): void {
    for (const node of this.sectionFeedback) {node.textContent = this.feedback.textContent; node.dataset.error = this.feedback.dataset.error ?? '';}
  }

  private document(scenario: Scenario): Scenario {
    return Object.assign(newScenario(), {version_policy: 'append'}, clone(scenario));
  }
  private discardAllowed(): boolean {return !this.dirty || window.confirm('Отменить несохранённые изменения сценария?');}
  private glyph(name: string): HTMLElement {
    const paths: Record<string, string> = {
      layers: '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
      sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
      volume: '<path d="m11 4-6 5H2v6h3l6 5V4Zm4 4a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
      shortcut: '<path d="m12 2 9 6-9 6-9-6 9-6Zm-9 10 9 6 9-6M3 16l9 6 9-6"/>',
      clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
      settings: '<circle cx="12" cy="12" r="3"/><path d="m9 3-1 3-3 1-2 3 2 2-1 3 3 2 1 3h4l2-3 3-1 2-3-2-2 1-3-3-2-1-3H9Z"/>',
      home: '<path d="m3 10 9-8 9 8M5 9v12h5v-7h4v7h5V9"/>',
      building: '<path d="M5 21V3h14v18M3 21h18M9 7h1m4 0h1m-6 4h1m4 0h1m-6 4h1m4 0h1m-4 6v-3"/>',
      pin: '<path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
      trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
      back: '<path d="m12 4-8 8 8 8M4 12h17"/>',
      app: '<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M3 8h18"/>',
      moon: '<path d="M20 14A9 9 0 0 1 10 3 9 9 0 1 0 20 14Z"/>',
    };
    const span = el('span', '', 'scenario-glyph');
    span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] ?? paths.layers}</svg>`;
    return span;
  }
  private blockGlyph(block: Block): string {
    return block.kind === 'delay' ? 'clock' : /brightness/.test(block.capability) ? 'sun' : /volume|audio|music/.test(block.capability) ? 'volume' : block.capability === 'shortcut.run' ? 'shortcut' : block.capability === 'mac.open_app' ? 'app' : 'layers';
  }
  private checkbox(label: string, value: boolean, change: (v: boolean) => void): HTMLElement {
    const input = el('input'); input.type = 'checkbox'; input.checked = value;
    input.onchange = () => {this.dirty = true; change(input.checked);};
    const root = this.label(label, input); root.className = 'scenario-check'; return root;
  }
  private commitPhrase(): boolean {
    const phrase = this.pendingPhrase.trim();
    if (!phrase || !this.draft) return true;
    const normalized = normalizePhrase(phrase);
    if (!normalized || this.draft.phrases.some(p => normalizePhrase(p) === normalized)) {
      this.feedback.textContent = 'Эта фраза уже добавлена или не содержит слов.'; this.feedback.dataset.error = 'true'; return false;
    }
    this.draft.phrases.push(phrase); this.pendingPhrase = ''; this.dirty = true; return true;
  }


  private renderLocation(): void {
    if (!this.data) return;
    const location = this.data.location;
    const choices: [string, string][] = [['auto', 'Автоматически'], ...this.data.places.map(p => [p.id, p.name] as [string, string])];
    const currentPlace = this.select(choices, location.mode === 'auto' ? 'auto' : location.place_id ?? 'auto', () => {});
    currentPlace.setAttribute('aria-label', 'Текущее место');
    currentPlace.onchange = () => this.send({op: 'location', mode: currentPlace.value === 'auto' ? 'auto' : 'manual', place_id: currentPlace.value === 'auto' ? '' : currentPlace.value});
    this.editorLocationRoot.replaceChildren(el('span', `Сейчас: ${location.name ?? 'место неизвестно'}`), currentPlace);
    this.locationRoot.replaceChildren(el('strong', `Сейчас: ${location.name ?? 'место не определено'}`));
    this.locationRoot.append(this.label('Выбор места для голосовых команд', this.select(
      [['auto', 'Определять автоматически по текущему городу'], ...this.data.places.map(p => [p.id, p.name] as [string, string])],
      location.mode === 'auto' ? 'auto' : location.place_id ?? 'auto',
      id => this.send({op: 'location', mode: id === 'auto' ? 'auto' : 'manual', place_id: id === 'auto' ? '' : id}), false)),
      el('small', location.mode === 'auto' ? (location.city ? `Наблюдаемый город: ${location.city}` : 'Используется уже подключённое определение города. При неизвестном месте выбери его вручную.') : 'Ручной выбор сохранён и действует до переключения в автоматический режим.'));

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
    root.append(el('h3', 'Общие действия и версии для мест'), el('p', 'Голосовые фразы общие для всех мест. Здесь можно настроить условия и дополнительные версии.', 'muted'));
    const variants = draft.variants;
    if (!variants.some(v => v.id === this.activeVariant)) this.activeVariant = '';
    const variant = variants.find(v => v.id === this.activeVariant);
    if (variant) {
      const fields = el('div', '', 'variant-fields');
      const enabled = el('input'); enabled.type = 'checkbox'; enabled.checked = variant.enabled; enabled.onchange = () => {variant.enabled = enabled.checked; this.dirty = true;};
      fields.append(this.label('Название версии', this.input(variant.name, v => variant.name = v)),
        this.label('Место этой версии', this.select((this.data?.places ?? []).filter(p => p.id === variant.place_id || !variants.some(v => v.place_id === p.id)).map(p => [p.id, p.name]), variant.place_id, v => variant.place_id = v)), this.label('Версия включена', enabled),
        this.button('Удалить версию', () => {if (window.confirm(`Удалить версию «${variant.name}» из черновика?`)) {draft.variants = variants.filter(v => v.id !== variant.id); this.activeVariant = ''; this.dirty = true; this.renderEditor();}}));
      root.append(fields, el('h4', 'Условия этой версии')); this.conditionList(root, variant.conditions);
    }
    const available = (this.data?.places ?? []).filter(p => !draft.variants.some(v => v.place_id === p.id));
    const additions = el('div', '', 'variant-add');
    if (available.length) {
      let placeId = available[0].id;
      const target = this.select(available.map(p => [p.id, p.name]), placeId, v => placeId = v, false);
      const create = (source?: Variant) => {const place = available.find(p => p.id === placeId)!; const added = addVariant(draft, place, source); this.activeVariant = added.id; this.dirty = true; this.previewRoot.replaceChildren(); this.renderEditor();};
      additions.append(this.label(variant ? 'Место для новой версии или копии' : 'Место для новой версии', target), this.button('Добавить пустую версию', () => create()));
      if (variant) additions.append(this.button('Скопировать версию в другое место', () => create(variant)));
    } else additions.append(el('p', 'Для всех мест уже есть версии. Дополнительные места можно добавить в настройках приложения.', 'muted'));
    root.append(additions);
    if (variants.length && !variant) root.append(el('p', 'Чтобы проверить определённую версию без выполнения, открой её вкладку и нажми «Безопасный тест».', 'muted'));
    return root;
  }

  private renderPreview(preview: Preview): void {
    this.previewRoot.replaceChildren(el('h2', `Предварительный просмотр: ${preview.name}`), el('p', preview.message));
    if (preview.errors?.length) {const errors = el('details'); errors.append(el('summary', 'Найдены ошибки. Проверь параметры блоков.')); for (const error of preview.errors) errors.append(el('p', error, 'error')); this.previewRoot.append(errors);}
    if (preview.timeout_s) this.previewRoot.append(el('small', `Ограничение времени: ${preview.timeout_s} с`));
    this.previewRoot.append(el('strong', preview.selection ? `Версия: ${preview.selection.variant_name}` : 'Общие шаги'));
    for (const condition of preview.conditions) this.previewRoot.append(el('p', condition === null ? 'Условие не определено: состояние устройства не проверено' : condition ? 'Условие запуска выполнено' : 'Условие запуска не выполнено; реальный запуск пропустит команду'));
    for (const step of preview.steps) {
      const card = el('article', '', 'preview-step');
      card.append(el('strong', `${step.path}. ${step.name ?? {delay: 'Задержка', parallel: 'Параллельно', sequence: 'Последовательность', if: 'Если / иначе'}[step.kind] ?? 'Недоступное действие'}`), el('small', step.status === 'skipped' ? step.reason ?? 'Пропущено' : 'Запланировано; устройство не вызывалось'));
      if (step.status === 'invalid') {card.append(el('p', 'Проверь параметры этого блока.', 'error')); if (step.reason) {const reason = el('details'); reason.append(el('summary', 'Причина'), el('p', step.reason)); card.append(reason);}}
      if (step.delay_before_s) card.append(el('small', `Перед блоком — пауза ${step.delay_before_s} с`));
      if (step.availability === 'unavailable') card.append(el('p', 'Устройство недоступно — реальный запуск остановится.', 'error'));
      else if (step.availability === 'not_checked') card.append(el('small', 'Доступность устройства не проверена; симуляция не запрашивает устройства.'));
      if (step.device) card.append(el('small', `Устройство: ${this.data?.devices.find(d => d.id === step.device)?.name ?? {mac: 'MacBook', tv: 'Внешний дисплей', shortcuts: 'Apple Shortcuts', led: 'LED-лента', music: 'Музыка'}[step.device] ?? step.device}`));
      if (step.seconds !== undefined) card.append(el('p', `${step.seconds} секунд (ожидание не запускается)`));
      if (step.condition_result !== undefined) card.append(el('p', `Условие: ${step.condition_result === null ? 'неизвестно; показаны обе ветки' : step.condition_result ? 'Да' : 'Нет'}`));
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

  private edit(scenario: Scenario): void {
    if (!this.discardAllowed()) return;
    this.feedback.replaceChildren(); delete this.feedback.dataset.error;
    this.draft = this.document(scenario); this.pendingPhrase = ''; this.activeVariant = ''; this.dirty = false; this.advancedOpen = false;
    this.previewRoot.replaceChildren(); this.revisionRoot.replaceChildren(); this.recognition.replaceChildren(); this.phraseText = ''; this.renderEditor();
  }
  private runButton(scenario: Scenario): HTMLButtonElement {
    const button = this.button('Запустить на устройствах', () => {if (this.dirty) {this.feedback.textContent = 'Сначала сохрани изменения.'; return;} if (this.running) return; const version = scenario.variants.find(v => v.id === this.activeVariant)?.name ?? 'Общий'; if (window.confirm(`Запустить «${scenario.name}», версия «${version}», на реальных устройствах?`)) {this.running = true; this.editor.querySelectorAll<HTMLButtonElement>('.real-run').forEach(button => button.disabled = true); this.send({op: 'run', id: scenario.id, revision: scenario.revision, variant: this.activeVariant || (scenario.version_policy === 'fallback' ? 'general' : '')});}});
    button.className = 'real-run';
    button.disabled ||= this.dirty || !scenario.enabled || !scenario.triggers.some(t => t.kind === 'manual'); return button;
  }
  private remove(scenario: Scenario): void {
    if (!window.confirm(`Удалить «${scenario.name}»?`)) return;
    this.lastDeleted = {id: scenario.id, revision: scenario.revision}; this.send({op: 'delete', id: scenario.id, revision: scenario.revision});
  }
  private blockTitle(block: Block): string {
    return block.kind === 'action' ? this.data?.capabilities.find(c => c.id === block.capability)?.name ?? 'Недоступное действие' : {delay: 'Подождать', sequence: 'По порядку', parallel: 'Одновременно', if: 'Если / иначе'}[block.kind];
  }
  private blockDescription(block: Block): string {
    if (!block.enabled) return 'Выключено';
    if (block.kind === 'delay') return `${block.seconds} с`;
    if (block.kind !== 'action') return `${block.children.length} действий`;
    return Object.entries(block.parameters).map(([key, value]) => `${parameterNames[key] ?? 'Значение'}: ${typeof value === 'object' ? 'из переменной' : value === true ? 'Да' : value === false ? 'Нет' : valueNames[String(value)] ?? value}`).join(' · ') || 'Настрой параметры';
  }
  private renderList(): void {
    this.list.replaceChildren();
    const filter = document.querySelector<HTMLSelectElement>('#scenario-place-filter');
    if (filter) {const values = [['', 'Все места'], ['general', 'Общий'], ...(this.data?.places ?? []).map(p => [p.id, p.name])]; filter.replaceChildren(...values.map(([value, name]) => {const option = el('option', name); option.value = value; return option;})); filter.value = this.placeFilter;}
    const matches = searchCommands(this.data?.scenarios ?? [], this.searchText).filter(s => !this.placeFilter || (this.placeFilter === 'general' ? s.general_enabled !== false : s.variants.some(v => v.place_id === this.placeFilter)));
    for (const scenario of matches) {
      const row = el('article', '', 'scenario-card');
      const open = this.button('', () => this.edit(scenario)); open.className = 'scenario-open'; open.setAttribute('aria-label', `Открыть сценарий ${scenario.name}`);
      const text = el('span', '', 'scenario-summary');
      const total = countBlocks(scenario.actions) + scenario.variants.reduce((n, v) => n + countBlocks(v.actions), 0);
      text.append(el('strong', scenario.name), el('small', scenario.phrases.map(p => `«${p}»`).join(' · ') || 'Запуск кнопкой'), el('small', `${[...(scenario.general_enabled !== false ? ['Общий'] : []), ...scenario.variants.map(v => v.name)].join(' · ')}   ·   Блоков: ${total}   ·   ${scenario.enabled ? 'Включён' : 'Выключен'}`, 'scenario-meta'));
      open.append(el('span', scenario.icon, 'scenario-icon'), text);
      const enabled = this.button('', () => {if (this.draft?.id === scenario.id && !this.discardAllowed()) return; this.toggling.add(scenario.id); this.send({op: 'save', scenario: {...clone(scenario), enabled: !scenario.enabled}});}); enabled.className = 'scenario-switch'; enabled.setAttribute('role', 'switch'); enabled.setAttribute('aria-checked', String(scenario.enabled)); enabled.setAttribute('aria-label', `Сценарий ${scenario.name} включён`);
      const menu = el('details', '', 'scenario-menu'); menu.append(el('summary', '⋯')); menu.firstElementChild!.setAttribute('aria-label', `Действия со сценарием ${scenario.name}`);
      const items = el('div', '', 'menu-items'); items.append(this.button('Редактировать', () => {menu.open = false; this.edit(scenario);}), this.button('Дублировать', () => {if (this.discardAllowed()) {this.saving = true; this.send({op: 'copy', id: scenario.id});}}), this.button('Удалить', () => this.remove(scenario)));
      menu.append(items); row.append(open, enabled, menu); this.list.append(row);
    }
    if (!matches.length) this.list.append(el('p', this.searchText ? 'Сценариев не найдено.' : 'Твои маленькие привычки начинаются с первого сценария.', 'empty-state'));
    if (this.lastDeleted) this.list.append(this.button('Отменить удаление', () => {const deleted = this.lastDeleted!; this.saving = true; this.send({op: 'restore', id: deleted.id, from_revision: deleted.revision, expected_revision: 0}); this.lastDeleted = null;}));
  }

  private renderEditor(): void {
    const draft = this.draft;
    this.editor.replaceChildren();
    if (this.dirty) this.previewRoot.replaceChildren();
    const page = document.querySelector<HTMLElement>('.automations-page')!;
    page.dataset.editing = String(!!draft);
    this.editor.parentElement!.dataset.editing = String(!!draft);
    if (!draft) return;
    const form = el('form');
    form.addEventListener('input', () => {this.previewRoot.replaceChildren(); for (const button of form.querySelectorAll<HTMLButtonElement>('.real-run')) button.disabled = true;});
    form.addEventListener('change', () => {this.previewRoot.replaceChildren(); for (const button of form.querySelectorAll<HTMLButtonElement>('.real-run')) button.disabled = true;});
    const saveDraft = () => {
      if (!this.online || this.saving || !this.commitPhrase()) return;
      if (!form.reportValidity()) return;
      if (!draft.general_enabled && !draft.variants.length) {this.feedback.textContent = 'Выбери хотя бы одно местоположение.'; return;}
      this.saving = true; this.feedback.textContent = 'Сохраняю…'; this.send({op: 'save', scenario: draft}); form.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement>('input,button,select,textarea').forEach(control => control.disabled = true);
    };
    form.onsubmit = event => {event.preventDefault(); saveDraft();};
    const heading = el('div', '', 'editor-heading');
    const back = this.button('', () => {if (!this.discardAllowed()) return; this.draft = null; this.dirty = false; this.pendingPhrase = ''; this.previewRoot.replaceChildren(); this.renderEditor(); this.renderList();});
    back.append(this.glyph('back')); back.setAttribute('aria-label', 'Все сценарии');
    const title = el('h1', draft.id ? 'Редактирование сценария' : 'Новый сценарий');
    const save = el('button', 'Сохранить'); save.type = 'submit'; save.className = 'primary'; save.disabled = !this.online || this.saving;
    const undo = this.button('Отменить изменения', () => {if (!this.discardAllowed()) return; const saved = this.data?.scenarios.find(s => s.id === draft.id); this.draft = saved ? this.document(saved) : newScenario(); if (!saved) this.draft.name = ''; this.pendingPhrase = ''; this.dirty = false; this.activeVariant = ''; this.previewRoot.replaceChildren(); this.renderEditor();}); undo.className = 'editor-undo';
    heading.append(back, title, undo);
    if (draft.id) {const remove = this.button('', () => this.remove(draft)); remove.append(this.glyph('trash')); remove.setAttribute('aria-label', 'Удалить сценарий'); remove.title = 'Удалить сценарий'; heading.append(remove);}
    heading.append(save);
    const first = el('section', '', 'editor-step scenario-basics');
    const icons = el('details', '', 'scenario-icon-picker'); const icon = el('summary', draft.icon); icon.setAttribute('aria-label', 'Иконка сценария'); icons.append(icon);
    const library = el('div', '', 'scenario-icon-library');
    for (const value of ['✦', '☾', '☀', '♫', '⌂', '▶', '♡', '⚡', '◈', '☕', '☁', '◎']) {const choice = this.button(value, () => {draft.icon = value; this.dirty = true; this.renderEditor();}); choice.setAttribute('aria-label', `Выбрать иконку ${value}`); library.append(choice);} icons.append(library);
    const fields = el('div', '', 'scenario-basic-fields');
    const name = this.input(draft.name, v => draft.name = v); name.required = true; name.maxLength = 120; name.placeholder = 'Например, Я спать';
    const phrases = el('div', '', 'phrase-tags');
    draft.phrases.forEach((phrase, index) => {
      const chip = el('span', '', 'phrase-tag');
      const input = this.input(phrase, v => {draft.phrases[index] = v; input.style.width = `${Math.max(3, v.length)}ch`;}); input.required = true; input.maxLength = 500; input.setAttribute('aria-label', `Фраза ${index + 1}`); input.style.width = `${Math.max(3, phrase.length)}ch`;
      const remove = this.button('×', () => {draft.phrases.splice(index, 1); this.dirty = true; this.renderEditor();}); remove.setAttribute('aria-label', `Удалить фразу ${index + 1}`); chip.append(input, remove); phrases.append(chip);
    });
    const entry = this.input(this.pendingPhrase, v => this.pendingPhrase = v); entry.className = 'phrase-entry'; entry.placeholder = 'Добавить фразу…'; entry.maxLength = 500; entry.setAttribute('aria-label', 'Добавить фразу');
    const addPhrase = () => {if (this.commitPhrase()) {this.renderEditor(); this.editor.querySelector<HTMLInputElement>('.phrase-entry')?.focus();}};
    entry.onkeydown = event => {if (event.key === 'Enter') {event.preventDefault(); addPhrase();}};
    const plus = this.button('+', addPhrase); plus.setAttribute('aria-label', 'Добавить фразу'); plus.title = 'Добавить фразу';
    phrases.append(entry, plus);
    fields.append(this.label('Название', name), el('span', 'Фразы для вызова', 'field-caption'), phrases);
    first.append(icons, fields);
    const locations = el('div', '', 'scenario-locations'); locations.append(el('span', 'Местоположение', 'field-caption'));
    const locationsControl = el('div', '', 'scenario-location-options');
    for (const place of [{id: 'general', name: 'Общий'}, ...(this.data?.places ?? [])]) {
      const selected = place.id === 'general' ? draft.general_enabled : draft.variants.some(v => v.place_id === place.id);
      const option = this.button('', () => {
        if (place.id === 'general') {draft.general_enabled = !draft.general_enabled; this.activeVariant = draft.general_enabled ? '' : draft.variants[0]?.id ?? '';}
        else if (!selected) {this.activeVariant = addVariant(draft, place as Place).id;}
        else {const variant = draft.variants.find(v => v.place_id === place.id)!; if (variant.actions.length && !window.confirm(`Удалить версию «${place.name}» и её блоки из черновика?`)) return; draft.variants = draft.variants.filter(v => v.id !== variant.id); if (this.activeVariant === variant.id) this.activeVariant = draft.general_enabled ? '' : draft.variants[0]?.id ?? '';}
        this.dirty = true; this.previewRoot.replaceChildren(); this.renderEditor();
      });
      option.append(this.glyph(place.id === 'general' ? 'home' : place.id === 'dorm' ? 'building' : 'pin'), el('span', place.id === 'home' && place.name === 'Дом' ? 'Дом (Силламяэ)' : place.name)); option.setAttribute('aria-pressed', String(selected)); option.setAttribute('aria-label', `Местоположение: ${place.name}`); if (place.id === 'general' && draft.version_policy === 'append') {option.disabled = true; option.title = 'В прежнем порядке общие блоки обязательны. Порядок можно изменить в дополнительных настройках.';} locationsControl.append(option);
    }
    locations.append(locationsControl, el('small', 'Можно выбрать несколько')); first.append(locations, this.editorLocationRoot); this.renderLocation();
    const second = el('section', '', 'editor-step scenario-constructor');
    const blockHeading = el('div', '', 'scenario-section-heading'); const h = el('h2', 'Блоки сценария'); h.prepend(this.glyph('layers')); blockHeading.append(h, el('span', 'Выполняются по порядку ↓', 'muted')); second.append(blockHeading);
    const variant = draft.variants.find(v => v.id === this.activeVariant);
    if (draft.variants.length) {
      const tabs = el('div', '', 'variant-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Версии для мест');
      for (const item of [...(draft.general_enabled ? [{id: '', name: 'Общий'}] : []), ...draft.variants]) {const tab = this.button(item.name, () => {this.activeVariant = item.id; this.previewRoot.replaceChildren(); this.renderEditor();}); tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(this.activeVariant === item.id)); tabs.append(tab);}
      second.append(tabs, el('p', draft.version_policy === 'append' ? 'Сохранён прежний порядок: общие блоки, затем версия места.' : 'Версия места заменяет общую. Общий сценарий используется там, где нет отдельной версии.', 'variant-hint'));
      if (variant) {
        const tools = el('div', '', 'variant-copy');
        const targets = [...(draft.general_enabled ? [{id: '', name: 'Общий'}] : []), ...draft.variants.filter(v => v.id !== variant.id)];
        if (targets.length) {
          let destination = targets[0].id;
          tools.append(this.select(targets.map(v => [v.id, v.name]), destination, v => destination = v, false), this.button('Копировать блоки', () => {const target = draft.variants.find(v => v.id === destination)?.actions ?? draft.actions; if (target.length && !window.confirm('Заменить блоки выбранной версии копией?')) return; target.splice(0, target.length, ...clone(variant.actions)); this.dirty = true; this.renderEditor();}));
          tools.querySelector('select')!.setAttribute('aria-label', 'Куда скопировать блоки');
        }
        second.append(tools);
      }
    }
    second.append(this.blocks(variant?.actions ?? draft.actions, false));
    const extra = el('section', '', 'editor-step scenario-options'); const extraHeading = el('h2', 'Дополнительно'); extraHeading.prepend(this.glyph('settings')); extra.append(extraHeading);
    const options = el('div', '', 'scenario-quick-options');
    const delay = this.input(String(draft.inter_block_delay_s), v => draft.inter_block_delay_s = Number(v), 'number'); delay.min = '0'; delay.max = '60'; delay.step = 'any';
    options.append(this.label('Задержка между блоками, с', delay), this.checkbox('Показывать статус выполнения в диалоге', draft.show_status, v => draft.show_status = v), this.checkbox('Всегда требовать подтверждение', draft.confirmation === 'always', v => draft.confirmation = v ? 'always' : 'auto'));
    extra.append(options, el('p', 'Чувствительные действия всегда проходят обязательные проверки разрешений.', 'muted'));
    const advanced = el('details', '', 'editor-advanced'); advanced.open = this.advancedOpen; advanced.ontoggle = () => this.advancedOpen = advanced.open; advanced.append(el('summary', 'Другие настройки'), this.advancedEditor(draft), this.revisionRoot, this.trainingRoot); extra.append(advanced);
    const third = el('section', '', 'editor-step editor-save');
    const controls = el('div', '', 'button-row'); controls.append(this.button('Проверить сценарий', () => {if (!this.commitPhrase()) return; this.send({op: 'preview', scenario: draft, variant: this.activeVariant || (draft.version_policy === 'fallback' ? 'general' : '')});}));
    if (draft.id) {const run = this.runButton(draft); run.disabled ||= this.dirty; controls.append(run);}
    third.append(controls, el('p', !draft.enabled ? 'Сценарий выключен.' : !draft.triggers.some(t => t.kind === 'voice') ? 'Голосовой запуск выключен.' : 'Проверка — безопасная симуляция. Реальный запуск доступен после сохранения.', 'muted'), this.previewRoot, this.executionRoot);
    form.append(heading, first, second, extra, third); this.editor.append(form); this.renderTraining(); this.renderRuns();
  }

  private advancedEditor(draft: Scenario): HTMLElement {
    const form = el('div', '', 'advanced-fields');
    const enabled = el('input'); enabled.type = 'checkbox'; enabled.checked = draft.enabled; enabled.onchange = () => {draft.enabled = enabled.checked; this.dirty = true; this.renderEditor();};
    form.append(this.label('Сценарий включён', enabled));
    const description = el('textarea'); description.value = draft.description; description.maxLength = 1000; description.oninput = () => {draft.description = description.value; this.dirty = true;};
    form.append(this.label('Описание', description), this.label('Иконка', this.input(draft.icon, v => draft.icon = v)), this.label('Распознавание', this.select([['exact', 'Только добавленные фразы'], ['smart', 'Фразы и похожие обращения']], draft.mode, v => draft.mode = v)), this.label('Подтверждение', this.select([['auto', 'Для безопасных действий не требуется'], ['always', 'Всегда запрашивать код']], draft.confirmation, v => draft.confirmation = v)));
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
    form.append(this.label('Выбор версии', this.select([['fallback', 'Версия места заменяет общую'], ['append', 'Общие блоки, затем версия места (прежний порядок)']], draft.version_policy, v => {draft.version_policy = v; if (v === 'append') draft.general_enabled = true; this.renderEditor();})), this.label('При ошибке', this.select([['block', 'Как указано в каждом блоке'], ['stop', 'Остановить сценарий'], ['continue', 'Продолжить, сохранив ошибку']], draft.error_policy, v => draft.error_policy = v)));
    const timeout = this.input(draft.timeout_s === null ? '' : String(draft.timeout_s), v => draft.timeout_s = v === '' ? null : Number(v), 'number'); timeout.min = '1'; timeout.max = '3600'; timeout.placeholder = 'Без ограничения'; form.append(this.label('Ограничение времени, с', timeout));
    form.append(el('h3', 'Порядок выполнения'), this.blockTools(draft.variants.find(v => v.id === this.activeVariant)?.actions ?? draft.actions), this.variantEditor(draft));
    if (draft.revision) form.append(this.button('История редакций', () => this.send({op: 'revisions', id: draft.id})), this.runButton(draft), this.button('Удалить сценарий', () => this.remove(draft)));
    form.append(this.button('Остановить ожидание', () => this.send({op: 'cancel'})));
    const training = el('details', '', 'phrase-training'); training.append(el('summary', 'Проверить голосовые фразы'), this.commandRoot); form.append(training); this.renderCommands();
    return form;
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
  private actionBlock(kind: Block['kind'], capability: string): Block {
    const block = newBlock(kind, capability);
    if (kind === 'action' && capability === 'led.set') block.parameters.color = '#B48CCB';
    return block;
  }
  private blocks(blocks: Block[], _showTools = true): HTMLElement {
    const root = el('div', '', 'scenario-blocks');
    const add = (kind: Block['kind'], capability = '') => {
      const block = this.actionBlock(kind, capability);
      if (kind === 'action') {
        const cap = this.data?.capabilities.find(c => c.id === capability);
        for (const key of cap?.required ?? []) {
          const schema = cap!.parameters[key];
          if (schema.enum?.length === 1) block.parameters[key] = schema.enum[0];
          else if (schema.type === 'integer' || schema.type === 'number') block.parameters[key] = Math.max(schema.minimum ?? 0, Math.min(schema.maximum ?? 100, 50));
        }
        if (capability === 'mac.volume') block.parameters.level = 20;
      }
      if (kind === 'if') block.condition = {source: 'time', key: 'hour', operator: 'gt', value: 21};
      blocks.push(block); if (!['delay', 'action'].includes(kind) || capability === 'led.set') this.openedBlocks.add(block);
      this.dirty = true; this.renderEditor();
    };
    if (!blocks.length) root.append(el('p', 'Добавь первое действие — всё остальное Чушпан выполнит по порядку.', 'blocks-empty'));
    blocks.forEach((block, index) => {
      const card = el('details', '', 'scenario-block'); card.open = this.openedBlocks.has(block); card.dataset.enabled = String(block.enabled);
      card.ontoggle = () => {if (card.open) this.openedBlocks.add(block); else this.openedBlocks.delete(block);};
      card.ondragover = event => {if (this.dragSource?.blocks !== blocks) return; event.preventDefault(); event.stopPropagation(); card.classList.add('drop-target');};
      card.ondragleave = () => card.classList.remove('drop-target');
      card.ondrop = event => {event.preventDefault(); event.stopPropagation(); const source = this.dragSource; this.dragSource = null; if (source?.blocks === blocks && reorderBlock(blocks, source.index, index)) {this.dirty = true; this.renderEditor();}};
      const heading = el('summary', '', 'action-summary');
      const handle = el('span', '⠿', 'block-drag-handle'); handle.draggable = true; handle.title = 'Перетащи, чтобы изменить порядок'; handle.setAttribute('aria-hidden', 'true');
      handle.ondragstart = event => {event.stopPropagation(); this.dragSource = {blocks, index}; event.dataTransfer?.setData('text/plain', String(index)); if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';};
      handle.ondragend = () => {this.dragSource = null; this.editor.querySelectorAll('.drop-target').forEach(node => node.classList.remove('drop-target'));};
      const text = el('span', '', 'action-title'); text.append(el('strong', this.blockTitle(block)), el('small', this.blockDescription(block)));
      heading.append(handle, el('span', String(index + 1), 'action-number'), this.glyph(this.blockGlyph(block)), text);
      const inline = el('div', '', 'action-inline'); const body = el('div', '', 'action-fields');
      const controls = el('div', '', 'action-controls');
      const up = this.button('↑', () => {moveBlock(blocks, index, -1); this.dirty = true; this.renderEditor();}); up.setAttribute('aria-label', 'Переместить действие выше'); up.disabled ||= index === 0;
      const down = this.button('↓', () => {moveBlock(blocks, index, 1); this.dirty = true; this.renderEditor();}); down.setAttribute('aria-label', 'Переместить действие ниже'); down.disabled ||= index === blocks.length - 1;
      controls.append(up, down, this.button('Дублировать блок', () => {blocks.splice(index + 1, 0, clone(block)); this.dirty = true; this.renderEditor();}), this.button('Удалить действие', () => {blocks.splice(index, 1); this.dirty = true; this.renderEditor();}));
      const extra = el('details', '', 'action-extra'); extra.append(el('summary', 'Настройки блока'));
      extra.append(this.checkbox('Шаг включён', block.enabled, v => {block.enabled = v; this.renderEditor();}));
      if (block.kind === 'action') extra.append(this.label('Доступность устройства', this.select([['required', 'Обязательный шаг'], ['optional', 'Пропустить, если устройство отсутствует']], block.required ? 'required' : 'optional', v => block.required = v === 'required')));
      extra.append(this.label('При ошибке блока', this.select([['stop', 'Остановить сценарий'], ['continue', 'Продолжить, сохранив ошибку']], block.on_error, v => block.on_error = v)));
      if (block.kind === 'action') {
        const capability = this.data?.capabilities.find(c => c.id === block.capability);
        if (capability) {
          const simple = /^(mac.brightness|display.brightness|mac.volume|shortcut.run|mac.open_app)$/.test(block.capability);
          card.classList.toggle('compact-action', simple);
          for (const [key, schema] of Object.entries(capability.parameters)) {
            const required = capability.required.includes(key), row = this.parameter(block, key, schema, required);
            if (simple && (required || key === 'level' || key === 'name')) inline.append(row);
            else if (required || key in block.parameters || block.capability === 'led.set' && ['color', 'brightness', 'power'].includes(key)) body.append(row);
            else extra.append(row);
          }
          if (capability.risk === 'dangerous') body.append(el('p', 'При запуске потребуется подтверждение.', 'muted'));
          if (block.capability === 'led.set') body.prepend(el('p', this.data?.devices.find(d => d.id === 'led')?.name ?? 'Поддерживаемая LED-лента', 'muted'));
          if (block.capability === 'shortcut.run') body.append(el('p', capability.parameters.name?.enum?.length ? 'Разрешённые команды из настроек Чушпана. Результат проверяется при запуске.' : 'Нет разрешённых команд. Добавь Shortcut в настройках Чушпана.', 'muted'));
        } else body.append(el('p', 'Интеграция недоступна. Настрой её в разделе «Устройства».', 'error'), this.button('Открыть устройства', () => this.navigate('devices')));
      } else if (block.kind === 'delay') {
        card.classList.add('compact-action'); let unit = block.seconds >= 60 && block.seconds % 60 === 0 ? 60 : 1;
        const seconds = this.input(String(block.seconds / unit), v => block.seconds = Number(v) * unit, 'number'); seconds.min = '0'; seconds.max = String(3600 / unit); seconds.step = 'any';
        inline.append(this.label('Длительность', seconds), this.select([['1', 'сек'], ['60', 'мин']], String(unit), v => {unit = Number(v); seconds.value = String(block.seconds / unit); seconds.max = String(3600 / unit);}));
        inline.querySelector('select')!.setAttribute('aria-label', 'Единица времени');
      } else {
        if (block.kind === 'if') body.append(this.conditionEditor(block.condition!));
        if (block.kind === 'parallel') body.append(el('small', 'Ветки выполняются одновременно на разных устройствах.'));
        body.append(el('h4', block.kind === 'if' ? 'Да' : 'Действия'), this.blocks(block.children));
        if (block.kind === 'if') body.append(el('h4', 'Нет'), this.blocks(block.otherwise));
      }
      const more = this.button('⋮', () => {card.open = !card.open; extra.open = card.open;}); more.className = 'block-more'; more.setAttribute('aria-label', `Настройки блока ${index + 1}`); more.onclick = event => {event.preventDefault(); event.stopPropagation(); card.open = !card.open; extra.open = card.open;};
      heading.append(inline, more);
      card.oninput = card.onchange = () => {text.lastElementChild!.textContent = this.blockDescription(block);};
      body.append(controls, extra); card.append(heading, body); root.append(card);
    });
    const picker = el('details', '', 'action-picker'); picker.append(el('summary', 'Добавить блок'));
    const panel = el('div', '', 'block-catalogue'); const search = el('input'); search.type = 'search'; search.placeholder = 'Найти действие…'; search.setAttribute('aria-label', 'Поиск блоков'); panel.append(search);
    const choices = el('div', '', 'action-choices');
    const render = () => {
      choices.replaceChildren();
      const query = search.value.trim().toLocaleLowerCase('ru');
      const categories: Record<string, {kind: Block['kind']; id: string; name: string; note: string; disabled: boolean}[]> = {'Устройства': [], 'Приложения': [], 'Система': [{kind: 'delay', id: '', name: 'Подождать', note: 'Пауза между действиями', disabled: false}], 'Логика': ['sequence', 'parallel', 'if'].map(kind => ({kind: kind as Block['kind'], id: '', name: {sequence: 'Последовательное выполнение', parallel: 'Параллельное выполнение', if: 'Условие: если / иначе'}[kind]!, note: 'Вложенные блоки', disabled: false}))};
      for (const cap of this.data?.capabilities ?? []) {
        const category = ['led.set', 'mac.brightness', 'display.brightness', 'mac.volume'].includes(cap.id) ? 'Устройства' : cap.id === 'mac.power' ? 'Система' : 'Приложения';
        const unavailable = cap.availability === 'unavailable' || cap.id === 'shortcut.run' && !cap.parameters.name?.enum?.length;
        categories[category].push({kind: 'action', id: cap.id, name: cap.name, note: unavailable ? 'Нужна настройка интеграции' : cap.device === 'shortcuts' ? 'Команды, разрешённые в Чушпане' : cap.description, disabled: unavailable});
      }
      let matches = 0;
      for (const [name, items] of Object.entries(categories)) {
        const visible = items.filter(item => `${item.name} ${name}`.toLocaleLowerCase('ru').includes(query)); if (!visible.length) continue;
        const group = el('div', '', 'action-group'); group.append(el('h4', name));
        for (const item of visible) {
          const button = this.button(item.name, () => add(item.kind, item.id)); button.disabled ||= item.disabled; group.append(button); matches++;
          if (item.disabled) group.append(el('small', item.note), this.button('Настроить', () => this.navigate('settings')));
        }
        choices.append(group);
      }
      if (!matches) choices.append(el('p', 'Подходящих действий нет. Доступные интеграции находятся в разделе «Устройства».', 'muted'));
    };
    search.oninput = render; render(); panel.append(choices); picker.append(panel); root.append(picker);
    return root;
  }
  private blockTools(blocks: Block[]): HTMLElement {
    const root = el('div', '', 'button-row');
    for (const [kind, name] of [['delay', 'Задержка'], ['sequence', 'Последовательность'], ['parallel', 'Параллельные действия'], ['if', 'Если / иначе']] as const) root.append(this.button(name, () => {const block = newBlock(kind); blocks.push(block); this.openedBlocks.add(block); this.dirty = true; this.renderEditor();}));
    return root;
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
    if (choices.length > 1 || source !== 'literal') {const binding = el('details', '', 'parameter-source'); binding.open = source !== 'literal'; binding.append(el('summary', 'Из переменной'), this.label('Источник', this.select(choices, source, v => {if (v === 'literal') delete block.parameters[key]; else block.parameters[key] = {variable: v}; this.renderEditor();}))); row.append(binding);}
    if (source !== 'literal') {row.append(el('strong', parameterNames[key] ?? key)); return row;}
    const assign = (v: string) => {if (v === '' && !required) delete block.parameters[key]; else block.parameters[key] = schema.type === 'integer' || schema.type === 'number' ? Number(v) : schema.type === 'boolean' ? v === 'true' : v;};
    let control: HTMLInputElement | HTMLSelectElement;
    if (schema.enum || schema.type === 'boolean') {
      const options = [...(schema.enum ?? [true, false])];
      if (typeof block.parameters[key] === 'string' && schema.enum && !options.includes(block.parameters[key])) options.unshift(block.parameters[key]);
      control = this.select([['', required ? 'Выбери значение' : 'Не изменять'], ...options.map(v => [String(v), v === true ? 'Да' : v === false ? 'Нет' : valueNames[String(v)] ?? String(v)] as [string, string])], block.parameters[key] === undefined ? '' : String(block.parameters[key]), assign);
    } else {
      control = this.input(value === undefined ? '' : String(value), assign, ['integer', 'number'].includes(schema.type) ? 'number' : 'text');
      if (!required) control.placeholder = 'Не изменять';
      if (schema.minimum !== undefined) control.min = String(schema.minimum);
      if (schema.maximum !== undefined) control.max = String(schema.maximum);
      if (schema.maxLength) control.maxLength = schema.maxLength;
      if (key === 'color') {control.type = 'color'; control.oninput = () => {assign(control.value.toUpperCase()); this.dirty = true;};}
    }
    if ((schema.type === 'integer' || schema.type === 'number') && schema.minimum === 0 && schema.maximum === 100 && !schema.enum) {
      const slider = el('input'); slider.type = 'range'; slider.min = '0'; slider.max = '100'; slider.value = String(value ?? 50); slider.setAttribute('aria-label', `${parameterNames[key] ?? key}: ползунок`);
      const number = control; number.classList.add('slider-number');
      const sync = () => {slider.style.setProperty('--progress', `${slider.value}%`);}; sync();
      slider.oninput = () => {assign(slider.value); number.value = slider.value; sync(); this.dirty = true;};
      number.addEventListener('input', () => {slider.value = number.value; sync();});
      row.append(slider); row.classList.add('slider-parameter');
    }
    const active = this.draft?.variants.find(v => v.id === this.activeVariant);
    control.required = required && block.enabled && (active?.enabled ?? true);
    row.append(this.label((parameterNames[key] ?? key) + (required ? ' *' : ''), control)); return row;
  }

  private renderDevices(): void {
    this.deviceRoot.className = 'device-list'; this.deviceRoot.replaceChildren();
    const capabilities = this.data?.capabilities ?? [];
    for (const device of this.data?.devices ?? []) {
      const card = el('details', '', 'device-row'); card.open = this.selectedDevice === device.id;
      card.ontoggle = () => {if (card.open) this.selectedDevice = device.id; else if (this.selectedDevice === device.id) this.selectedDevice = '';};
      const connected = ['connected', 'confirmed', 'observed'].includes(device.status);
      const label = connected ? 'Подключено' : ['unavailable', 'error', 'failed'].includes(device.status) ? 'Отключено' : 'Подключение не проверено';
      const heading = el('summary'); const text = el('span'); text.append(el('strong', device.name), el('small', device.actions.map(id => capabilities.find(c => c.id === id)?.name).filter(Boolean).join(' · ') || 'Нет доступных действий'));
      heading.append(text, el('span', label, connected ? 'device-connected' : 'device-status')); card.append(heading);
      const detail = el('div', '', 'device-detail');
      if (device.note) detail.append(el('p', device.note, 'muted'));
      for (const id of device.actions) {
        const capability = capabilities.find(c => c.id === id); if (!capability) continue;
        const create = this.button(capability.name, () => {if (!this.discardAllowed()) return; this.draft = newScenario(); this.draft.name = capability.name; const block = this.actionBlock('action', id); this.draft.actions = [block]; this.openedBlocks.add(block); this.activeVariant = ''; this.dirty = false; this.advancedOpen = false; this.renderEditor(); this.navigate('automations');}); create.dataset.createAction = 'true'; detail.append(create);
      }
      if (device.id === 'led') {const controls = el('details'); controls.append(el('summary', 'Управление подсветкой'), this.legacyControls); detail.append(controls);}
      const extra = el('details'); extra.append(el('summary', 'Дополнительно'));
      if (device.actions.length) extra.append(this.button('Проверить подключение', () => this.send({op: 'probe', device: device.id})));
      if (device.last_success) extra.append(el('p', `Последняя успешная операция: ${device.last_operation ?? ''} · ${new Date(device.last_success).toLocaleString()}`));
      if (device.error) extra.append(el('p', device.error, 'error'));
      if (device.details) extra.append(el('pre', JSON.stringify(device.details, null, 2)));
      detail.append(extra); card.append(detail); this.deviceRoot.append(card);
    }
    if (!this.data?.devices.length) this.deviceRoot.append(el('p', 'Устройства появятся после подключения Чушпана.', 'muted'));
  }
  private renderPermissions(): void {
    this.permissionRoot.className = 'permission-list'; this.permissionRoot.replaceChildren();
    if (!this.permissions) {this.permissionRoot.append(el('p', 'Проверь доступ, чтобы увидеть состояние разрешений.', 'muted')); return;}
    if (this.permissions.error) {const details = el('details'); details.append(el('summary', 'Не удалось проверить доступ. Подробнее'), el('p', this.permissions.error)); this.permissionRoot.append(details);}
    for (const permission of this.permissions.items) {
      const row = el('article', '', 'permission-row'); const text = el('div');
      const label = permission.status === 'granted' ? 'Разрешено' : ['unsupported', 'unavailable'].includes(permission.status) ? 'Недоступно' : 'Требуется доступ';
      text.append(el('strong', permission.name), el('small', label)); row.append(text);
      if (permission.status !== 'granted' && permission.url) {const link = el('a', 'Настроить'); link.href = permission.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.setAttribute('aria-label', `Настроить: ${permission.name}`); row.append(link);}
      if (permission.note) {const details = el('details'); details.append(el('summary', 'Подробнее'), el('p', permission.note)); row.append(details);}
      this.permissionRoot.append(row);
    }
  }
  private renderCommands(): void {
    this.commandRoot.replaceChildren();
    const section = el('div', '', 'command-training');
    this.phraseInput = el('input'); this.phraseInput.value = this.phraseText; this.phraseInput.placeholder = 'Как ты ещё можешь сказать?'; this.phraseInput.oninput = () => this.phraseText = this.phraseInput!.value;
    section.append(this.label('Новая фраза', this.phraseInput), this.button('Проверить понимание', () => {if (this.phraseText.trim()) this.send({op: 'recognize', text: this.phraseText});}), this.button('Добавить к фразам', () => {if (!this.draft || !this.phraseText.trim()) return; this.draft.phrases.push(this.phraseText.trim()); this.phraseText = ''; this.dirty = true; this.renderEditor();}), this.recognition);
    const recording = el('details'); recording.append(el('summary', 'Произнести фразу'), el('p', 'Следующая реплика будет использована только для обучения. Прослушивание — до 20 секунд. Аудио не сохраняется.', 'muted'), this.button('Начать прослушивание', () => {this.capturing = true; this.send({op: 'capture', consent: true}); this.wake(); this.renderCommands();}));
    if (this.capturing) {recording.open = true; recording.append(this.button('Отменить прослушивание', () => {this.capturing = false; this.send({op: 'capture_cancel'}); this.renderCommands();}));}
    section.append(recording, el('small', 'Новая фраза применится вместе с сохранением сценария.')); this.commandRoot.append(section);
  }
  private renderTraining(): void {
    this.trainingRoot.replaceChildren();
    const entries = (this.data?.training ?? []).filter(entry => entry.scenario_id === this.draft?.id);
    if (!entries.length) return;
    const audit = el('details'); audit.append(el('summary', 'История обучения'));
    for (const entry of entries) audit.append(el('p', `${new Date(entry.created_at).toLocaleString()} · ${{add: 'Добавлено', edit: 'Изменено', delete: 'Удалено'}[entry.operation] ?? entry.operation} · ${entry.phrase}`));
    this.trainingRoot.append(audit);
  }
  private renderRuns(): void {
    this.editor.querySelectorAll<HTMLButtonElement>('.real-run').forEach(button => button.disabled = !this.online || this.dirty || this.running || !this.draft?.enabled || !this.draft.triggers.some(t => t.kind === 'manual') || !!this.data?.runs.some(run => run.scenario_id === this.draft?.id && run.status === 'running'));
    this.runRoot.replaceChildren(); this.executionRoot.replaceChildren(); this.dialogueStatus.replaceChildren();
    if (!this.data?.runs.length) this.runRoot.append(el('p', 'Сценарии ещё не запускались.', 'muted'));
    for (const run of this.data?.runs ?? []) {
      const details = el('details', '', 'scenario-run');
      details.append(el('summary', `${run.name}${run.variant_name ? ' / ' + run.variant_name : ''} · ${status[run.status] ?? run.status} · ${run.duration_s === undefined ? 'длительность ещё не измерена' : run.duration_s.toFixed(3) + ' с'} · ${new Date(run.started_at).toLocaleString()}`));
      if (run.error) details.append(el('p', run.error, 'error'));
      for (const step of run.steps) {const row = el('div'); row.append(el('p', `${step.path}. ${this.data?.capabilities.find(c => c.id === step.capability)?.name ?? {delay: 'Пауза', sequence: 'По порядку', parallel: 'Одновременно', if: 'Условие'}[step.kind] ?? 'Действие'} · ${status[step.status] ?? step.status} · ${step.duration_s?.toFixed(3) ?? '…'} с`)); if (step.reason) row.append(el('p', {disabled: 'Шаг выключен', unavailable: 'Устройство недоступно; необязательный шаг пропущен'}[step.reason] ?? step.reason)); if (step.error) row.append(el('p', step.error, 'error')); if (step.result) {const diagnostic = el('details'); diagnostic.append(el('summary', 'Подробности интеграции'), el('pre', JSON.stringify(step.result, null, 2))); row.append(diagnostic);} details.append(row);}
      this.runRoot.append(details);
      if (this.draft?.id === run.scenario_id && !this.executionRoot.childElementCount) {const receipt = details.cloneNode(true) as HTMLDetailsElement; receipt.open = true; this.executionRoot.append(el('h3', 'Последнее выполнение'), receipt);}
      if (this.data?.runs[0]?.id === run.id && !this.dialogueStatus.childElementCount && this.data?.scenarios.find(s => s.id === run.scenario_id)?.show_status !== false) {this.dialogueStatus.append(el('strong', `${run.name} · ${status[run.status] ?? run.status}`)); for (const step of run.steps) this.dialogueStatus.append(el('div', `${step.path}. ${this.data?.capabilities.find(c => c.id === step.capability)?.name ?? {delay: 'Пауза', sequence: 'По порядку', parallel: 'Одновременно', if: 'Условие'}[step.kind] ?? 'Действие'} · ${status[step.status] ?? step.status}`));}

    }
  }
}

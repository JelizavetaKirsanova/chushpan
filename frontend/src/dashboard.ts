import type { HudEvent, WeatherEvent } from './types';
import type { Character } from './character';
import { eventTime, measured, monthDays, safeColor, shouldAmbient, type AmbientSettings, type CalendarCard, type ConversationItem, type DashboardData, type DashboardStats } from './dashboard_state';
import { phaseLabels } from './workspace_state';

const node = (id: string) => document.getElementById(id)!;
const paths: Record<string, string> = {
  heart: 'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z',
  shield: 'M12 3 3 7v5c0 5 9 10 9 10s9-5 9-10V7ZM8 12l3 3 5-6',
  cpu: 'M5 5h14v14H5ZM9 9h6v6H9ZM9 2v3m6-3v3m-6 14v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3',
  link: 'M10 13a5 5 0 0 0 7 0l4-4a5 5 0 0 0-7-7l-2 2m2 7a5 5 0 0 0-7 0l-4 4a5 5 0 0 0 7 7l2-2',
  chat: 'M21 11a8 8 0 0 1-8 8H5l-3 3v-9a8 8 0 1 1 19-2Z',
  history: 'M3 12a9 9 0 1 0 3-6M3 3v6h6M12 7v5l3 2',
  calendar: 'M8 2v4m8-4v4M3 9h18M5 4h14a2 2 0 0 1 2 2v14H3V6a2 2 0 0 1 2-2Zm2 9h2m3 0h2m3 0h1M7 17h2m3 0h2',
  chart: 'M4 20v-7h4v7m2 0V7h4v13m2 0V3h4v17',
  note: 'M14 3H5a2 2 0 0 0-2 2v15h13l5-5V5a2 2 0 0 0-2-2ZM7 8h10M7 12h8m1 8v-5h5',
  settings: 'm10 3-1 3-3 1-3-1-2 4 2 2v3l-2 2 2 4 3-1 3 1 1 3h4l1-3 3-1 3 1 2-4-2-2v-3l2-2-2-4-3 1-3-1-1-3Zm2 6a3 3 0 1 1 0 6 3 3 0 0 1 0-6Z',
  lamp: 'M9 18h6m-6 3h6M8 14c-6-8 2-15 7-10s1 9-1 10v2H9v-2Z',
  sparkles: 'm12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3Zm-8 0v4M2 4h4m14 14v4m-2-2h4',
  target: 'M20 10a8 8 0 1 1-6-6m-2 4a4 4 0 1 0 4 4M12 12l8-8m-3 0h3v3',
  search: 'M10 3a7 7 0 1 1 0 14 7 7 0 0 1 0-14Zm5 12 6 6',
  pin: 'M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0ZM12 7a3 3 0 1 1 0 6 3 3 0 0 1 0-6Z',
  moon: 'M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10Z',
  mic: 'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0Zm-3 6a6 6 0 0 0 12 0m-6 6v4m-4 0h8',
  send: 'm22 2-7 20-4-9-9-4Zm0 0L11 13',
  volume: 'M11 4 6 8H2v8h4l5 4ZM15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14',
  activity: 'M2 12h4l3-8 5 16 3-8h5',
  news: 'M4 3h16v18H4ZM7 7h10M7 11h4v4H7Zm7 0h3m-3 4h3M7 18h10',
};
export function icon(name: string): string { return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] ?? paths.sparkles}"/></svg>`; }
function el(tag: string, text = '', className = ''): HTMLElement { const value = document.createElement(tag); value.textContent = text; value.className = className; return value; }
function button(label: string, action: () => void, className = ''): HTMLButtonElement { const value = el('button', label, className) as HTMLButtonElement; value.type = 'button'; value.onclick = action; return value; }
type NativeState = {idle_s: number; minimized: boolean; screens: number; ambient_active?: boolean;};
type NativeApi = {desktop_state?: () => Promise<NativeState>; ambient?: (active: boolean, display: string, fullscreen: boolean) => Promise<{ok: boolean; reason?: string}>;};
const native = () => (window as unknown as {pywebview?: {api?: NativeApi}}).pywebview?.api;

export class DashboardView {
  private online = false;
  private busy = false;
  private microphone = "unknown";
  private currentStatus = "";
  private data: DashboardData | null = null;
  private pending = '';
  private month = new Date().getMonth();
  private year = new Date().getFullYear();
  private selected = '';
  private ambient = false;
  private previousView = 'home';
  private lastInput = Date.now();
  private lastSystemIdle: number | null = null;
  private searchTimer = 0;
  private statsDays = 1;
  private editSave: ((value: string) => void) | null = null;
  private nativeBusy = false;
  private battery = false;
  hasLocation = false;

  constructor(private post: (value: object) => void, private chat: (value: {text: string; speak?: boolean}) => void,
    private navigate: (view: string) => void, private search: (value: object) => void, private character: Character,
    private weather: (value: WeatherEvent) => void) {
    this.icons();
    document.querySelectorAll<HTMLButtonElement>('.workspace-nav button[data-view]').forEach(control => { const label = control.querySelector('span:not([data-icon])')?.textContent ?? control.textContent ?? 'Раздел'; control.setAttribute('aria-label', label.trim()); control.title = label.trim(); });
    document.addEventListener('click', event => this.click(event));
    document.addEventListener('pointerdown', () => { this.lastInput = Date.now(); if (this.ambient) this.setAmbient(false); }, {capture: true});
    document.addEventListener('keydown', () => { this.lastInput = Date.now(); if (this.ambient) this.setAmbient(false); }, {capture: true});
    node('chat-composer').addEventListener('submit', event => { event.preventDefault(); this.submitChat(); });
    node('chat-input').addEventListener('keydown', event => { if ((event as KeyboardEvent).key === 'Enter' && !(event as KeyboardEvent).shiftKey) { event.preventDefault(); this.submitChat(); } });
    for (const id of ['history-search', 'recent-search']) node(id).addEventListener('input', () => {
      const value = (node(id) as HTMLInputElement).value;
      for (const other of ['history-search', 'recent-search']) if (other !== id) (node(other) as HTMLInputElement).value = value;
      clearTimeout(this.searchTimer); this.searchTimer = window.setTimeout(() => this.search({query: value, trash: (node('history-trash') as HTMLInputElement).checked}), 250);
    });
    node('history-trash').addEventListener('change', () => this.search({query: (node('history-search') as HTMLInputElement).value, trash: (node('history-trash') as HTMLInputElement).checked}));
    node('memory-search').addEventListener('input', () => this.renderMemory());
    node('ambient-settings').addEventListener('submit', event => { event.preventDefault(); this.send({op: 'settings', value: this.readSettings()}); });
    node('manual-location').addEventListener('submit', event => { event.preventDefault(); this.send({op: 'manual_city', city: (node('manual-city') as HTMLInputElement).value}); });
    node('performance-period').addEventListener('change', () => { this.statsDays = Number((node('performance-period') as HTMLSelectElement).value); this.send({op: 'stats', days: this.statsDays}); });
    node('led-level').addEventListener('input', () => { node('led-level-value').textContent = `${(node('led-level') as HTMLInputElement).value}%`; });
    node('led-brightness').addEventListener('submit', event => { event.preventDefault(); this.command(`Сделай яркость ленты ${(node('led-level') as HTMLInputElement).value} процентов`); });
    node('edit-cancel').addEventListener('click', () => (node('edit-dialog') as HTMLDialogElement).close());
    node('edit-form').addEventListener('submit', event => { event.preventDefault(); if (!this.available()) { this.feedback('Нет соединения с Чушпаном. Текст сохранён в форме.'); return; } const long = !node('edit-long-label').hidden; const value = (node(long ? 'edit-long' : 'edit-value') as HTMLInputElement).value.trim(); if (!value && (long || !node('edit-value-label').hidden)) return; this.editSave?.(value); (node('edit-dialog') as HTMLDialogElement).close(); });
    node('confirm-action').addEventListener('click', () => { const current = this.data?.confirmation; if (current) this.send({op: 'confirm', fingerprint: current.fingerprint, code: current.code}); });
    document.addEventListener('visibilitychange', () => { if (!document.hidden && this.online) this.refresh(); });
    const colors: [string, string, string][] = [['Красный', '#e86f83', 'красную'], ['Синий', '#7b8fe9', 'синюю'], ['Фиолетовый', '#b18ae8', 'фиолетовую'], ['Розовый', '#eea0b6', 'розовую'], ['Светло-розовый', '#f4d0e1', 'светло-розовую'], ['Тёплый белый', '#f6debc', 'тёплую белую']];
    node('led-colors').replaceChildren(...colors.map(([label, color, word]) => { const control = button(label, () => this.command(`Сделай ${word} ленту`), 'color-choice'); control.style.setProperty('--swatch', color); return control; }));
    this.renderMonth();
    window.setInterval(() => void this.pollAmbient(), 5000);
  }
  private icons(): void { document.querySelectorAll<HTMLElement>('[data-icon]').forEach(target => target.innerHTML = icon(target.dataset.icon!)); }
  private available(): boolean { return this.online && !!this.data?.ready; }
  private feedback(text: string): void { node('dashboard-feedback').textContent = text; node('dashboard-feedback').hidden = !text; }
  private send(value: {op: string; [key: string]: unknown}): void {
    if (!this.online || (!this.data?.ready && !['refresh', 'stats'].includes(value.op))) { this.feedback('Нет соединения с Чушпаном. Изменение не отправлено.'); return; }
    this.feedback(''); this.post(value);
  }
  connection(online: boolean): void {
    this.online = online;
    if (!online) { this.pending = ''; this.status('Ошибка подключения'); this.character.error(); this.feedback('Связь с Чушпаном потеряна. Переподключаюсь…'); }
    else { this.feedback(''); this.refresh(); }
    this.controls();
  }
  private controls(): void {
    const available = this.online && !!this.data?.ready;
    (node('chat-send') as HTMLButtonElement).disabled = !available || this.busy || !!this.pending;
    document.querySelectorAll<HTMLButtonElement>('#ambient-settings button[type=submit], #manual-location button[type=submit], [data-action=new-chat], [data-action=add-note], [data-action=add-goal], [data-action=add-task], #confirm-action').forEach(control => control.disabled = !available || this.busy);
    document.querySelectorAll<HTMLInputElement>('#dashboard-goals input').forEach(control => control.disabled = !available);
    document.querySelectorAll<HTMLButtonElement>('[data-op], [data-command], #run-night').forEach(control => control.disabled = !available || this.busy);
    if (this.data) {
      (node('run-night') as HTMLButtonElement).disabled ||= !this.data.integrations.night;
      document.querySelectorAll<HTMLButtonElement>('.led-card button').forEach(control => control.disabled = !available || this.busy || !this.data!.integrations.led);
    }
  }
  refresh(): void { if (this.online) this.send({op: 'refresh', days: this.statsDays}); }
  private submitChat(): void {
    const text = (node('chat-input') as HTMLTextAreaElement).value.trim();
    if (!text || !this.online || !this.data?.ready || this.busy || this.pending) return;
    this.pending = text; this.chat({text, speak: (node('chat-speak') as HTMLInputElement).checked}); this.controls();
  }
  private command(text: string): void { if (this.online && this.data?.ready && !this.busy) { this.setAmbient(false); this.navigate('home'); this.chat({text}); } }
  private status(text: string): void {
    this.currentStatus=text;
    if(this.online&&!this.busy&&this.microphone==='stopped')text='Микрофон выключен';
    else if(this.online&&!this.busy&&this.microphone==='starting')text='Подготовка микрофона';
    node('nav-state').textContent=text;node('caption-state').textContent=text;
  }
  receive(event: HudEvent): void {
    if (event.type === 'voice_settings') {this.microphone=event.microphone;this.status(this.currentStatus);}
    else if(event.type==='session' && event.active && event.listening && !this.busy){this.status(event.recording===false?'Ожидает продолжение':'Слушает');}
    else if (event.type === 'dashboard') { this.data = event; this.hasLocation = !!event.location.city; this.render(); }
    else if (event.type === 'dashboard_stats') this.renderStats(event);
    else if (event.type === 'dashboard_calendar') { this.selected = event.date; this.renderEvents(node('calendar-events'), event); node('calendar-selected-title').textContent = this.dateLabel(event.date); this.renderMonth(); }
    else if (event.type === 'history') this.renderRecent(event.conversations);
    else if (event.type === 'state') {
      this.busy = event.state === 'thinking' || event.state === 'speaking';
      if (this.busy) this.setAmbient(false);
      this.status({sleeping: 'Ожидает обращения', listening: 'Слушает', thinking: 'Думает', speaking: 'Отвечает', booting: 'Загружается'}[event.state]); this.controls();
    } else if (event.type === 'activity') {
      this.busy = !['idle', 'listening'].includes(event.phase);
      if (this.busy) this.setAmbient(false);
      this.status(phaseLabels[event.phase] ?? 'Выполняет запрос'); this.controls();
    } else if (event.type === 'tool') { this.busy = true; this.status('Выполняет команду'); this.controls(); }
    else if (event.type === 'user') {
      this.setAmbient(false); document.querySelector<HTMLElement>('.hud')!.dataset.chat = 'true';
      if (event.text === this.pending) { if ((node('chat-input') as HTMLTextAreaElement).value.trim() === this.pending) (node('chat-input') as HTMLTextAreaElement).value = ''; this.pending = ''; }
      this.controls();
    } else if (event.type === 'transcript') document.querySelector<HTMLElement>('.hud')!.dataset.chat = String(event.lines.length > 0);
    else if (event.type === 'reply_end') { this.pending = ''; this.refresh(); this.controls(); }
    else if (event.type === 'notice') { this.pending = ''; this.feedback(event.message); if (event.error) this.character.error(); this.controls(); }
    else if (event.type === 'performance_live' && event.trace && event.trace.status !== 'running') {
      if (event.trace.stages.some(s => s.stage === 'tool_execution' && s.outcome === 'ok') && event.trace.status === 'completed') this.character.success();
    } else if (event.type === 'system') { this.battery = event.battery?.on_power_adapter === false; this.motion(); }
    else if (event.type === 'settings_info' || event.type === 'hello') this.refresh();
  }
  private render(): void {
    const data = this.data!;
    const location = data.location;
    node('location-caption').textContent = location.city ? `${location.source === 'manual' ? 'Выбран город' : 'Ты сейчас'}: ${location.city}` : 'Твой город';
    node('location-zone').textContent = location.zone || 'Местоположение неизвестно';
    node('location-source').textContent = location.message ?? (location.status === 'stale' ? 'Последний подтверждённый город' : location.source === 'manual' ? 'Выбран вручную · метка города' : location.city ? 'По геопозиции Mac · метка города' : 'Выбери город в настройках');
    node('location-feedback').textContent = location.message ?? (location.city ? `${location.city} · ${location.source === 'manual' ? 'выбран вручную' : 'геопозиция Mac'}` : 'Город пока не определён.');
    node('calendar-feedback').textContent = data.calendar.message ?? (data.integrations.calendar_enabled ? 'Календарь подключён · события доступны только для чтения.' : 'Календарь не подключён.');
    node('location-caption').textContent = location.city && location.status !== 'ready' ? `Последний город: ${location.city}` : node('location-caption').textContent;
    node('weather-feedback').textContent = data.weather.message ?? (data.weather.updated_at ? `Обновлено ${this.time(data.weather.updated_at)}` : 'Выбери город, чтобы узнать погоду.');
    if (data.weather.data) {
      const weather = data.weather.data;
      this.weather({type: 'weather', ...weather, daytime: null, sunrise: null, sunset: null});
      const condition = weather.now.conditions?.toLowerCase() ?? '';
      node('weather-art').textContent = /гроза/.test(condition) ? '☁' : /снег/.test(condition) ? '❄' : /ясно/.test(condition) ? '☀' : /дожд|морось|ливень/.test(condition) ? '☂' : '☁';
      node('ambient-weather').textContent = `${weather.place} · ${weather.now.temperature ?? '—'} °C · ${weather.now.conditions ?? 'нет данных'}`;
      const today = weather.days[0];
      node('weather-forecast').textContent = today ? `Сегодня: ${today.conditions ?? ''}${today.min != null && today.max != null ? `, от ${today.min} до ${today.max}°` : ''}${today.rain_chance_percent != null && today.rain_chance_percent >= 30 ? ` · осадки ${today.rain_chance_percent}%` : ''}` : '';
    }
    node('today-date').textContent = this.dateLabel(data.date);
    this.renderEvents(node('today-events'), data.calendar, true);
    (node('calendar-connect-card') as HTMLButtonElement).hidden = data.integrations.calendar_enabled;
    if (!this.selected) { this.selected = data.date; const [year, month] = data.date.split('-').map(Number); this.year = year; this.month = month - 1; }
    if (this.selected === data.calendar.date) { this.renderEvents(node('calendar-events'), data.calendar); node('calendar-selected-title').textContent = this.dateLabel(this.selected); }
    this.renderMonth(); this.renderMemory(); this.renderGoals(); this.motion();
    const next = data.calendar.events.find(e => new Date(e.end).getTime() > Date.now());
    node('ambient-event').textContent = next ? `${eventTime(next, data.calendar.timezone ?? data.timezone)} · ${next.title}` : data.calendar.status === 'ready' ? 'На сегодня больше нет событий' : 'Календарь не подключён';
    node('night-availability').textContent = data.integrations.night ? 'Сценарий подключён. Разрешения проверяются при запуске.' : 'Сценарий недоступен в текущей конфигурации.';
    node('led-availability').textContent = data.integrations.led ? 'Управление настроено · соединение проверяется при команде' : 'Подсветка отключена в настройках';
    node('action-confirmation').hidden = !data.confirmation;
    node('confirmation-summary').textContent = data.confirmation?.summary ?? '';
    node('confirm-action').textContent = data.confirmation ? `Подтверждаю ${data.confirmation.code}` : '';
    if (!node('ambient-settings').contains(document.activeElement)) {
      (node('ambient-enabled') as HTMLInputElement).checked = data.settings.ambient_enabled;
      (node('ambient-minutes') as HTMLSelectElement).value = String(data.settings.ambient_minutes);
      (node('ambient-display') as HTMLSelectElement).value = data.settings.ambient_display;
      (node('ambient-fullscreen') as HTMLInputElement).checked = data.settings.ambient_fullscreen;
      (node('ambient-battery') as HTMLInputElement).checked = data.settings.reduce_on_battery;
    }
    this.controls();
  }
  private renderRecent(conversations: ConversationItem[]): void {
    const target = node('recent-conversations');
    target.replaceChildren(...conversations.filter(c => !c.deleted_at).slice(0, 7).map(c => {
      const control = button(c.title || c.preview || `Разговор ${c.id}`, () => { this.navigate('history'); this.search({query: (node('recent-search') as HTMLInputElement).value, selected: c.id}); }, 'recent-conversation');
      const mark = el('span', c.pinned ? '⌁' : '♡'); control.prepend(mark); control.title = c.title || c.preview || `Разговор ${c.id}`; return control;
    }));
    if (!target.children.length) target.append(el('p', 'Пока нет разговоров', 'muted'));
  }
  private dateLabel(day: string): string { return new Date(`${day}T12:00:00Z`).toLocaleDateString('ru-RU', {weekday: 'short', day: 'numeric', month: 'long', timeZone: 'UTC'}); }
  private time(value: string): string { return new Date(value).toLocaleTimeString('ru-RU', {timeZone: this.data?.timezone, hour: '2-digit', minute: '2-digit'}); }
  private renderEvents(target: HTMLElement, card: CalendarCard, compact = false): void {
    target.replaceChildren();
    if (card.status !== 'ready') { target.append(el('p', card.message ?? 'Подключи календарь, чтобы увидеть планы.', 'empty-card')); return; }
    const events = compact ? card.events.slice(0, 4) : card.events;
    for (const item of events) {
      const control = button('', () => this.send({op: 'calendar_open', id: item.id}), 'calendar-event');
      control.style.setProperty('--event-color', safeColor(item.color)); control.append(el('time', eventTime(item, card.timezone ?? this.data!.timezone)), el('span', item.title));
      control.title = `${item.calendar} · ${item.title}`; target.append(control);
    }
    if (!events.length) target.append(el('p', 'На этот день событий нет. Можно выдохнуть ♡', 'empty-card'));
    if (compact && card.events.length > events.length) target.append(button(`Ещё ${card.events.length - events.length} событий`, () => { this.navigate('calendar'); this.selectDay(card.date); }, 'text-button'));
  }
  private renderMonth(): void {
    const date = new Date(Date.UTC(this.year, this.month, 1));
    const title = date.toLocaleDateString('ru-RU', {month: 'long', year: 'numeric', timeZone: 'UTC'}).replace(' г.', '');
    document.querySelectorAll('.calendar-month-title').forEach(target => target.textContent = title[0].toUpperCase() + title.slice(1));
    for (const id of ['calendar-mini-grid', 'calendar-full-grid']) node(id).replaceChildren(...monthDays(this.year, this.month).map(day => {
      const control = button(String(day.day), () => { this.navigate('calendar'); this.selectDay(day.date); }, 'calendar-day');
      control.dataset.current = String(day.current); control.dataset.today = String(day.date === this.data?.date); control.setAttribute('aria-pressed', String(day.date === this.selected)); control.setAttribute('aria-label', this.dateLabel(day.date)); return control;
    }));
  }
  private selectDay(day: string): void { this.selected = day; this.renderMonth(); this.send({op: 'calendar_day', date: day}); }
  private renderGoals(): void {
    const items = this.data!.items.filter(i => i.kind !== 'note');
    node('dashboard-goals').replaceChildren(...items.map(item => {
      const row = el('label', '', 'goal-row'); const check = document.createElement('input'); check.type = 'checkbox'; check.checked = !!item.completed; check.onchange = () => this.send({op: 'item_update', id: item.id, completed: check.checked}); row.append(check, el('span', item.content)); row.dataset.complete = String(!!item.completed);
      const remove = button('×', () => this.confirm('Убрать задачу?', item.content, () => this.send({op: 'item_delete', id: item.id})), 'row-remove'); remove.setAttribute('aria-label', `Убрать задачу: ${item.content}`); row.append(remove); return row;
    }));
    if (!items.length) node('dashboard-goals').append(el('p', 'Что хочется сделать на этой неделе? Добавь первую цель.', 'empty-card'));
  }
  private renderMemory(): void {
    if (!this.data) return;
    const query = (node('memory-search') as HTMLInputElement).value.toLocaleLowerCase('ru');
    const notes = this.data.items.filter(i => i.kind === 'note' && i.content.toLocaleLowerCase('ru').includes(query));
    node('dashboard-notes').replaceChildren(...notes.map(item => {
      const row = el('article', '', 'dashboard-card note-card'); row.append(el('p', item.content)); const actions = el('div', '', 'button-row'); actions.append(button('Изменить', () => this.edit('Изменить заметку', item.content, true, content => this.send({op: 'item_update', id: item.id, content}))), button('Убрать', () => this.confirm('Убрать заметку?', item.content, () => this.send({op: 'item_delete', id: item.id})))); row.append(actions); return row;
    }));
    const memories = this.data.memories.filter(i => i.status === 'active' && i.content.toLocaleLowerCase('ru').includes(query));
    node('dashboard-memories').replaceChildren(...memories.map(item => {
      const categories: Record<string, string> = {profile: 'Профиль', preference: 'Предпочтение', habit: 'Привычка', project: 'Проект', episode: 'Событие', correction: 'Исправление', personality: 'Характер'};
      const row = el('article', '', 'dashboard-card note-card'); row.append(el('small', `Факт ${item.id} · ${categories[item.category] ?? 'Память'}`, 'muted'), el('p', item.content)); const actions = el('div', '', 'button-row'); actions.append(button('Исправить', () => this.edit('Исправить воспоминание', item.content, true, content => this.send({op: 'memory_update', id: item.id, revision: item.revision, content}), 600)), button('Забыть', () => this.send({op: 'memory_delete', id: item.id, revision: item.revision}))); row.append(actions); return row;
    }));
    for (const id of ['dashboard-notes', 'dashboard-memories']) if (!node(id).children.length) node(id).append(el('p', query ? 'Ничего не найдено.' : id === 'dashboard-notes' ? 'Здесь появятся твои заметки.' : 'Сохранённых фактов пока нет.', 'empty-card'));
  }
  private renderStats(stats: DashboardStats): void {
    const pairs: [string, string][] = [['Разговоры', String(stats.conversations)], ['Сообщения', String(stats.messages)], ['Отклик', measured(stats.response_s)], ['Команды', String(stats.commands)], ['Ошибки', String(stats.errors)]];
    if (stats.days === 1) node('dashboard-stats').replaceChildren(...pairs.map(([label, value]) => { const row = el('div'); row.append(el('dt', label), el('dd', value)); return row; }));
    if (stats.days !== this.statsDays) return;
    const details = [...pairs, ['Распознавание речи', measured(stats.stages.stt)], ['Генерация модели', measured(stats.stages.model_total)], ['Синтез речи', measured(stats.stages.tts_generation)], ['Последняя модель', stats.model ?? 'Нет данных']];
    node('dashboard-stat-details').replaceChildren(...details.map(([label, value]) => { const row = el('div', '', 'perf-card'); row.append(el('small', label), el('strong', value)); return row; }));
    const maximum = Math.max(1, ...stats.buckets.map(b => b.requests));
    node('dashboard-stat-chart').replaceChildren(...stats.buckets.map(bucket => { const column = el('div', '', 'chart-column'); const bar = el('div', '', 'chart-bar'); bar.style.height = `${bucket.requests / maximum * 130}px`; column.append(el('small', String(bucket.requests)), bar, el('span', bucket.label)); column.title = `${bucket.label}: запросов ${bucket.requests}; средний отклик ${measured(bucket.response_s)}`; return column; }));
    if (!stats.buckets.length) node('dashboard-stat-chart').append(el('p', 'Нет трасс за выбранный период.', 'empty-card'));
  }
  private click(event: MouseEvent): void {
    const target = (event.target as Element).closest<HTMLElement>('button'); if (!target) return;
    if (target.dataset.go) this.navigate(target.dataset.go);
    if (target.dataset.command) this.command(target.dataset.command);
    if (target.dataset.op) this.send({op: target.dataset.op, ...(target.dataset.op.startsWith('calendar') ? {date: this.selected || this.data?.date} : {})});
    if (target.dataset.month) { const next = new Date(Date.UTC(this.year, this.month + Number(target.dataset.month), 1)); this.year = next.getUTCFullYear(); this.month = next.getUTCMonth(); this.renderMonth(); }
    const action = target.dataset.action;
    if (action === 'new-chat') this.send({op: 'new_conversation'});
    if (action === 'ambient') this.setAmbient(true);
    if (action === 'ambient-exit') this.setAmbient(false);
    if (action === 'add-note' || action === 'add-goal' || action === 'add-task') this.edit(action === 'add-note' ? 'Новая заметка' : action === 'add-task' ? 'Новая задача' : 'Цель на неделю', '', true, content => this.send({op: 'item_add', kind: action === 'add-note' ? 'note' : action === 'add-task' ? 'task' : 'goal', content}));
    if (target.dataset.conversationAction) {
      const id = Number(target.dataset.id);
      if (target.dataset.conversationAction === 'rename') this.edit('Название разговора', target.dataset.title ?? '', false, title => this.send({op: 'conversation_rename', id, title}));
      else if (target.dataset.conversationAction === 'delete') this.confirm('Переместить разговор в корзину?', 'Переписку можно будет восстановить в разделе «История разговоров».', () => this.send({op: 'conversation_delete', id}));
      else this.send({op: `conversation_${target.dataset.conversationAction}`, id});
    }
  }
  private edit(title: string, value: string, long: boolean, save: (value: string) => void, limit = long ? 3000 : 80): void {
    this.editSave = save; node('edit-heading').textContent = title; node('edit-description').textContent = ''; node('edit-value-label').hidden = long; node('edit-long-label').hidden = !long;
    (node('edit-value') as HTMLInputElement).value = value; (node('edit-long') as HTMLTextAreaElement).value = value; (node('edit-long') as HTMLTextAreaElement).maxLength = limit; node('edit-submit').textContent = 'Сохранить'; (node('edit-dialog') as HTMLDialogElement).showModal(); (node(long ? 'edit-long' : 'edit-value') as HTMLInputElement).focus();
  }
  private confirm(title: string, description: string, save: () => void): void {
    this.editSave = save; node('edit-heading').textContent = title; node('edit-description').textContent = description; node('edit-value-label').hidden = true; node('edit-long-label').hidden = true; node('edit-submit').textContent = 'Подтвердить'; (node('edit-dialog') as HTMLDialogElement).showModal();
  }
  private readSettings(): AmbientSettings { return {ambient_enabled: (node('ambient-enabled') as HTMLInputElement).checked, ambient_minutes: Number((node('ambient-minutes') as HTMLSelectElement).value), ambient_display: (node('ambient-display') as HTMLSelectElement).value as 'primary' | 'external', ambient_fullscreen: (node('ambient-fullscreen') as HTMLInputElement).checked, reduce_on_battery: (node('ambient-battery') as HTMLInputElement).checked}; }
  private motion(): void { document.querySelector<HTMLElement>('.hud')!.classList.toggle('reduce-motion', !!this.data?.settings.reduce_on_battery && this.battery); }
  private async pollAmbient(): Promise<void> {
    if (!this.data || this.nativeBusy) return;
    this.nativeBusy = true;
    let state: NativeState | null = null;
    try {
      if (native()?.desktop_state) state = await native()!.desktop_state!();
      const idle = state?.idle_s ?? (Date.now() - this.lastInput) / 1000;
      if (this.ambient && this.lastSystemIdle !== null && idle + 1 < this.lastSystemIdle) this.setAmbient(false);
      this.lastSystemIdle = idle;
      if (!this.ambient && shouldAmbient(this.data.settings, idle, !document.hidden, this.busy, this.online, state?.minimized)) this.setAmbient(true);
      if (!this.ambient && state?.ambient_active && !state.minimized) await native()?.ambient?.(false, this.data.settings.ambient_display, this.data.settings.ambient_fullscreen);
      node('ambient-note').textContent = state ? `Бездействие Mac. Доступных дисплеев: ${state.screens}.` : 'В браузере учитывается бездействие этой вкладки. Выбор дисплея работает в приложении Чушпана.';
    } catch { node('ambient-note').textContent = 'Не удалось прочитать бездействие Mac. Автоматический Ambient доступен по активности вкладки.'; }
    finally { this.nativeBusy = false; }
  }
  private setAmbient(active: boolean): void {
    if (active === this.ambient || !this.data) return;
    const hud = document.querySelector<HTMLElement>('.hud')!;
    if (active) { this.previousView = hud.dataset.view ?? 'home'; this.navigate('home'); this.lastSystemIdle = null; }
    this.ambient = active; hud.dataset.ambient = String(active);
    if (!active) this.navigate(this.previousView);
    const response = native()?.ambient?.(active, this.data.settings.ambient_display, this.data.settings.ambient_fullscreen);
    if (response) void response.then(result => { if (!result.ok && result.reason === 'external_unavailable') node('ambient-note').textContent = 'Внешний дисплей не подключён. Комната показана в текущем окне.'; }).catch(() => { node('ambient-note').textContent = 'Настройки окна не применены. Ambient остаётся в текущем окне.'; });
  }
}

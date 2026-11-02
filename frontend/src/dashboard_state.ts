export interface AmbientSettings {
  ambient_enabled: boolean; ambient_minutes: number; ambient_display: 'primary' | 'external';
  ambient_fullscreen: boolean; reduce_on_battery: boolean;
}
export interface CalendarItem {
  id: string; title: string; start: string; end: string; all_day: boolean; calendar: string; color: string;
}
export interface CalendarCard { status: string; date: string; events: CalendarItem[]; timezone?: string; message?: string; updated_at?: string; }
export interface UserItem {id: number; kind: 'note' | 'task' | 'goal'; content: string; completed: number;}
export interface MemoryItem {id: number; content: string; category: string; status: string; revision: number; source: string;}
export interface ConversationItem {id: number; started_at: string; messages: number; title?: string; pinned?: number; preview?: string; deleted_at?: string | null;}
export interface DashboardData {
  type: 'dashboard'; ready: boolean; timezone: string; date: string; settings: AmbientSettings;
  location: {city?: string; zone: string; status: string; source?: string; updated_at?: string; message?: string;};
  weather: {status: string; updated_at?: string; message?: string; data?: {
    place: string; now: {temperature: number | null; feels_like: number | null; conditions: string | null; humidity_percent: number | null; wind_kmh: number | null;};
    days: {day: string; weekday: string; conditions: string; min: number | null; max: number | null; rain_chance_percent: number | null;}[];
  }};
  calendar: CalendarCard; items: UserItem[]; memories: MemoryItem[];
  confirmation: {summary: string; fingerprint: string; code: string;} | null;
  integrations: {led: boolean; night: boolean; calendar_enabled: boolean; location_enabled: boolean;};
  conversation_id: number;
}
export interface DashboardStats {
  type: 'dashboard_stats'; days: number; conversations: number; messages: number; response_s: number | null;
  stages: Record<string, number | null>; commands: number; errors: number; model: string | null;
  buckets: {label: string; requests: number; response_s: number | null;}[];
}
export type DashboardEvent = DashboardData | DashboardStats | ({type: 'dashboard_calendar'} & CalendarCard);

export function dayKey(date: Date, timezone: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', {timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(date);
  const part = (name: string) => parts.find(p => p.type === name)!.value;
  return `${part('year')}-${part('month')}-${part('day')}`;
}
export function monthDays(year: number, month: number): {date: string; day: number; current: boolean;}[] {
  const first = new Date(Date.UTC(year, month, 1));
  const offset = (first.getUTCDay() + 6) % 7;
  const count = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  return Array.from({length: Math.ceil((offset + count) / 7) * 7}, (_, index) => {
    const date = new Date(Date.UTC(year, month, index - offset + 1));
    return {date: date.toISOString().slice(0, 10), day: date.getUTCDate(), current: date.getUTCMonth() === month};
  });
}
export function eventTime(event: CalendarItem, timezone: string): string {
  if (event.all_day) return 'Весь день';
  const format = (date: string) => new Date(date).toLocaleTimeString('ru-RU', {timeZone: timezone, hour: '2-digit', minute: '2-digit'});
  return `${format(event.start)} — ${format(event.end)}`;
}
export function shouldAmbient(settings: AmbientSettings, idle: number | null, visible: boolean, busy: boolean, connected: boolean, minimized = false): boolean {
  return settings.ambient_enabled && idle !== null && idle >= settings.ambient_minutes * 60 && visible && !busy && connected && !minimized;
}
export function safeColor(value: string): string { return /^#[0-9a-f]{6}$/i.test(value) ? value : '#a990da'; }
export function measured(value: number | null | undefined): string { return value == null ? 'Нет данных' : `${value.toFixed(2)} с`; }

import type {VoiceSettingsEvent} from './voice_settings';
// Events sent by backend/hud (see backend/hud/bus.py and backend/voice.py).
import type { PerformanceEvent } from './performance';
import type { DashboardEvent, ConversationItem } from './dashboard_state';

export type AssistantState = "booting" | "sleeping" | "listening" | "thinking" | "speaking";

export interface Hello {
  type: "hello";
  name: string;
  language: string;
  model: string;
  owner: string | null;
  place: string | null;
  tools: string[];
  wake_word: boolean;
  wake_phrase?: string | null;
}

export interface StateEvent {
  type: "state";
  state: AssistantState;
}

export interface LevelEvent {
  type: "level";
  mic?: number;
  out?: number;
}

export interface TranscriptLine {
  timestamp?: string;
  role: "user" | "assistant";
  text: string;
  open: boolean;
}

export interface WeatherDay {
  day: string;
  weekday: string;
  conditions: string | null;
  min: number | null;
  max: number | null;
  rain_chance_percent: number | null;
}

export interface WeatherEvent {
  type: "weather";
  place: string | null;
  now: {
    conditions: string | null;
    temperature: number | null;
    feels_like: number | null;
    humidity_percent: number | null;
    wind_kmh: number | null;
  };
  daytime: boolean | null;
  sunrise: string | null;
  sunset: string | null;
  days: WeatherDay[];
}

export interface NewsEvent {
  type: "news";
  items: { title: string; source: string; hours_ago: number | null; link?: string }[];
}

export interface SystemEvent {
  type: "system";
  battery: { percent: number; status: string; on_power_adapter: boolean } | null;
  cpu_percent: number | null;
  memory_percent: number | null;
  memory_total_gb: number | null;
}

export interface Timer {
  id: number;
  label: string | null;
  seconds: number;
  ends_at_ms: number;
}

export interface StatsEvent {
  type: "stats";
  trace_id?: string;
  response_s?: number | null;
  tts_s?: number;
  model_used?: boolean;
  stages?: Record<string, number>;
  attempts?: {stage: string; seconds: number; outcome: string}[];
  stt_s: number | null;
  first_token_s: number | null;
  voice_s: number | null;
  tokens_per_second: number | null;
  tools: string[];
}

export type BootStatus = "pending" | "running" | "done" | "skipped" | "offline" | "failed";

export interface BootEvent {
  type: "boot";
  steps: { id: string; status: BootStatus }[];
  progress: number;
}

export interface FootprintEvent {
  type: "footprint";
  jarvis_mb: number | null;
  model_mb: number | null; // 0 = unloaded while Jarvis sleeps; null = Ollama unreachable
  model: string | null;
}

export interface AutostartEvent {
  type: "autostart";
  enabled: boolean;
  error?: string;
}

export interface ExportedEvent {
  type: "exported";
  path?: string;
  name?: string;
  error?: string;
}

type HudPayload =
  | import('./automations').AutomationsEvent
  | VoiceSettingsEvent
  | DashboardEvent
  | PerformanceEvent
  | {type: "music_settings"; current: string | null; saved: string | null; restart_required: boolean; error: string | null}
  | {type: "addressee"; state: "direct" | "continuation" | "waiting" | "task" | "task_running" | "ambiguous"}
  | {type: "dialogue_settings_result"; ok: boolean}
  | {type: "session"; active: boolean; listening: boolean; requires_wake: boolean; remaining_s: number; enabled: boolean; duration_s: number; nameless_interruptions: boolean; nameless_ready: boolean; interrupted: boolean; reason: string; addressee_strictness: "contextual" | "addressed"}
  | {type: "ai_status"; phase: string; provider: string | null; model: string | null; reason?: string | null; models: {model: string; provider: string; status: string; retry_in_s: number; successes: number; failures: number; latency_s: number | null; error: string | null}[]}
  | {type: "settings_info"; items: Record<string, string>}
  | {type: "activity"; phase: string}
  | {type: "notice"; message: string; error: boolean}
  | {type: "history"; selected: number; conversations: ConversationItem[]; messages: {role: string; content: string; created_at: string; request_id?: string; has_trace?: number}[]; query?: string; trash?: boolean; error?: string}
  | Hello
  | StateEvent
  | LevelEvent
  | WeatherEvent
  | NewsEvent
  | SystemEvent
  | StatsEvent
  | AutostartEvent
  | FootprintEvent
  | BootEvent
  | ExportedEvent
  | { type: "timers"; items: Timer[] }
  | { type: "transcript"; lines: TranscriptLine[] }
  | { type: "user"; text: string }
  | { type: "reply"; delta: string; duration_s?: number }
  | { type: "reply_end"; cut?: boolean }
  | { type: "tool"; name: string };

export type HudEvent = HudPayload & {request_id?: number};

import type { HudEvent } from './types';
import { seconds } from './workspace_state';
import {timeline} from './performance_state';

export type TimingStage = {id: number; stage: string; start_s: number; end_s: number | null;
  seconds: number; outcome: string; metadata: Record<string, string | number | boolean | null>};
export type RequestTrace = {request_id: string; created_at: string; kind: string; status: string;
  first_audio_s?: number | null; waiting_to_content_gap_s?: number | null; elapsed_s: number; total_s: number | null; first_content_s: number | null;
  marks: Record<string, number>; metadata: Record<string, string | number | boolean | null>;
  errors: string[]; stages: TimingStage[]};
export type TraceSummary = {count: number; measured: number; failed: number; cancelled: number;
  incomplete?: number; rejected?: number; awaiting_confirmation?: number; unmeasured?: number;
  mean_s: number | null; median_s: number | null; p95_s: number | null; max_s: number | null;
  over: Record<string, number | null>};
export type TraceRow = Pick<RequestTrace, 'request_id' | 'created_at' | 'kind' | 'status' | 'total_s' | 'first_content_s' | 'metadata'>;
type BenchmarkSummary = {summary: TraceSummary; categories: Record<string, TraceSummary>; configuration: {model: string; num_ctx: number}};
export type PerformanceEvent = {type: 'performance_live' | 'performance_detail'; trace: RequestTrace | null}
  | {type: 'performance_stats'; days: number; summary: TraceSummary; providers: Record<string, TraceSummary>; requests: TraceRow[]; active?: RequestTrace | null; activations?: RequestTrace[];
     kinds?: Record<string, TraceSummary>; completion?: Record<string, TraceSummary>;
     comparison?: Record<string, {before: TraceSummary; after: TraceSummary}>;
     benchmarks?: {before: BenchmarkSummary; after: BenchmarkSummary} | null}
  | {type: 'performance_error'; message: string};

const labels: Record<string, string> = {reply_queue: 'Ожидание очереди ответа', stt: 'Распознавание речи', local_routing: 'Маршрутизация',
  intent_detection: 'Определение намерения', scenario_search: 'Поиск сценария', action_planning: 'Планирование действий', scenario_execution: 'Выполнение сценария', scenario_action: 'Действие сценария', result_verification: 'Проверка результата', scenario_interpretation: 'Уточнение сценария моделью',
  led_normalization: 'Нормализация команды подсветки', led_intent: 'Намерение подсветки', led_selection: 'Выбор инструмента LED', led_authorization: 'Разрешение на подсветку', led_request: 'Запрос к ленте', ble_connect: 'Подключение BLE', ble_write: 'Запись BLE', led_delivery: 'Отправка команды LED', led_result: 'Результат подсветки', led_reply: 'Ответ о подсветке',
  context_memory: 'Память и контекст', model_retry: 'Повтор после проверки ответа', model_total: 'Поток ответа LLM', llm_attempt: 'Попытка запроса', llm_transport: 'Поток Ollama', model_thinking: 'Обдумывание модели',
  tool_execution: 'Инструмент', tts_queue: 'Очередь TTS', tts_model_ready: 'Подготовка голоса',
  tts_text_preparation: 'Подготовка текста и произношения', tts_generation: 'Синтез звука', tts_playback: 'Озвучка ответа', waiting_playback: 'Реплика ожидания',
  gemini_first_event: 'Gemini: сеть и ожидание первого события', gemini_stream_total: 'Gemini: весь поток',
  model_first_text: 'LLM: первое содержимое', model_first_chunk: 'LLM: первый результат',
  provider_first_chunk: 'Провайдер: первый результат', vad_wait: 'Определение конца речи',
  wake_detection: 'Распознавание обращения', local_execution: 'Локальная команда',
  playback_gap: 'Пауза между аудиофрагментами', response_total: 'Полный ответ', tts_first_audio: 'TTS: первый звук', playback_queue: 'Очередь вывода'};
const marksLabels: Record<string, string> = {vad_start: 'VAD: начало речи (до конца реплики)', vad_end: 'VAD: конец речи', first_audio: 'Первый звук', waiting_sentence_ready: 'Фраза ожидания подготовлена', waiting_audio_enqueued: 'Звук ожидания подготовлен', user_speech_end: 'Конец реплики', stt_completed: 'Речь распознана', first_llm_token: 'Первый токен модели', first_llm_text: 'Первый текст модели', first_content_text: 'Начало текстового ответа', first_sentence_ready: 'Первый смысловой фрагмент', tts_audio_enqueued: 'Звук подготовлен', first_content_audio: 'Первый содержательный аудиоблок', waiting_audio: 'Реплика ожидания'};
const reasons: Record<string, string> = {rate_limit: 'Лимит API', timeout: 'Тайм-аут', api_error: 'Ошибка API', reply_validation: 'Проверка ответа'};
const statuses: Record<string, string> = {activated:'Обращение подтверждено', running: 'В работе', completed: 'Завершён', failed: 'Ошибка',
  cancelled: 'Отменён', interrupted: 'Прерван при остановке приложения', unknown: 'Завершение не подтверждено', rejected: 'Не принят', awaiting_confirmation: 'Ожидает подтверждения'};
const node = (id: string) => document.getElementById(id)!;
function el(tag: string, value = '', cls = ''): HTMLElement {
  const n = document.createElement(tag); n.textContent = value; n.className = cls; return n;
}


export class PerformancePanel {
  private days = 1;
  private kind = 'voice';
  private online = false;
  private live: RequestTrace | null = null;
  private selected: RequestTrace | null = null;
  private received = 0;
  private refreshPending = false;
  private awaitingFresh = true;
  private latestId: string | null = null;
  constructor(private stats: (days: number) => void, private detail: (id: string) => void) {
    node('performance-period').addEventListener('change', e => {
      this.days = Number((e.target as HTMLSelectElement).value); this.refresh();
    });
    node('performance-refresh').addEventListener('click', () => this.refresh());
    node('performance-kind').addEventListener('change', e => {
      this.kind = (e.target as HTMLSelectElement).value; this.refresh();
    });
    setInterval(() => this.tick(), 200);
  }
  connection(online: boolean): void {
    this.online = online;
    node('performance-connection').textContent = online ? 'Подключено к Чушпану' : 'Нет соединения. Текущий этап не подтверждён.';
    (node('performance-refresh') as HTMLButtonElement).disabled = !online;
    if (online) {
      this.live = null; this.awaitingFresh = true;
      node('performance-live').textContent = 'Проверяю текущий этап у backend…';
      this.refresh();
      if (this.latestId) this.detail(this.latestId);
    }
  }
  refresh(): void { if (this.online) this.stats(this.days); }
  open(id: string): void {
    this.latestId = id;
    node('performance-detail').replaceChildren(el('p', 'Загружаю трассу…'));
    if (this.online) this.detail(id);
  }
  receive(event: HudEvent): void {
    if (event.type === 'settings_info') { this.refresh(); return; }
    if (event.type === 'performance_live') {
      if (!event.trace || this.awaitingFresh) return;
      this.live = event.trace; this.received = performance.now();
      if (this.selected?.request_id === event.trace.request_id) this.selected = event.trace;
      if (event.trace.status !== 'running' && !this.refreshPending) {
        this.refreshPending = true; setTimeout(() => { this.refreshPending = false; this.refresh(); }, 100);
      }
      if (this.selected?.request_id === event.trace.request_id) this.renderDetail(event.trace);
      this.tick();
    } else if (event.type === 'performance_detail') {
      if (event.trace && event.trace.request_id !== this.latestId) return;
      this.selected = event.trace;
      if (!this.selected) node('performance-detail').replaceChildren(el('p', 'Для этого сообщения трасса не сохранена.'));
      else {
        this.renderDetail(this.selected);
        if (matchMedia('(max-width:1100px)').matches) node('performance-detail').scrollIntoView({block: 'start'});
      }
    } else if (event.type === 'performance_error') {
      node('performance-connection').textContent = event.message;
    } else if (event.type === 'performance_stats' && event.days === this.days) {
      if ('active' in event) {
        this.awaitingFresh = false;
        if (event.active) { this.live = event.active; this.received = performance.now(); this.tick(); }
        else if (!this.live || this.live.status === 'running') {
          this.live = null; node('performance-live').textContent = 'Активных запросов нет.';
        }
      }
      const s = event.kinds?.[this.kind] ?? event.summary;
      node('performance-summary').replaceChildren(...[
        ['Средний отклик', seconds(s.mean_s)], ['Медиана', seconds(s.median_s)],
        ['p95', seconds(s.p95_s)], ['Самый долгий отклик', seconds(s.max_s)],
        ['Запросов', String(s.count)], ['Ошибки / отмены / незавершённые', `${s.failed} / ${s.cancelled} / ${s.incomplete ?? 0}`],
        ...[5, 10, 15].map(t => [`Дольше ${t} с`, s.over[String(t)] == null ? '—' : `${s.over[String(t)]!.toFixed(1)} %`]),
      ].map(([label, value]) => { const n = el('div', '', 'perf-card'); n.append(el('small', label), el('strong', value)); return n; }));
      node('performance-samples').textContent = `Отклик — до содержательного звука для голоса, до текста для чата. ${s.measured} измеренных завершённых ответов. Без измерения: ${s.unmeasured ?? 0}; отклонено: ${s.rejected ?? 0}; ждут подтверждения: ${s.awaiting_confirmation ?? 0}. Полное завершение ${seconds(event.completion?.[this.kind]?.median_s)} (p50), включая озвучку; отдельно от отклика. Ошибки и отмены не заменяются нулём.`;
      node('performance-providers').replaceChildren(...Object.entries(event.providers).map(([p, v]) =>
        el('p', `${p === 'gemini' ? 'Gemini' : p === 'ollama' ? 'Ollama' : 'Без LLM'} (все виды ввода): ${v.count} запросов · средний ${seconds(v.mean_s)} · p95 ${seconds(v.p95_s)}`)));
      const statsLine = (label: string, value: TraceSummary) => el('p', `${label}: p50 ${seconds(value.median_s)} · p95 ${seconds(value.p95_s)} · среднее ${seconds(value.mean_s)} · максимум ${seconds(value.max_s)} · ${value.measured}/${value.count} измерено; ошибки ${value.failed}, отмены ${value.cancelled}, незавершённые ${value.incomplete ?? 0}, отклонено ${value.rejected ?? 0}`);
      const comparison = node('performance-comparison');
      comparison.replaceChildren(el('h3', 'Запросы в приложении'), el('p', 'Период и вид ввода выбраны выше. До / после обновления тракта; состав запросов может различаться.', 'perf-note'));
      const cohorts = event.comparison?.[this.kind];
      if (cohorts) comparison.append(statsLine('До', cohorts.before), statsLine('После', cohorts.after));
      const bench = node('performance-benchmarks');
      bench.replaceChildren(el('h3', 'Контрольный локальный прогон'));
      if (event.benchmarks) {
        bench.append(el('p', 'Одинаковый набор запросов. Реальные LLM и Silero; инструменты заменены стендом. Измеряется готовность PCM, без микрофона и воспроизведения. Эти значения не входят в статистику слышимого ответа.', 'perf-note'),
          statsLine('До', event.benchmarks.before.summary), statsLine('После', event.benchmarks.after.summary));
        const categories: Record<string,string> = {led:'Подсветка',brightness:'Яркость',volume:'Громкость',app:'Приложение',compound:'Составная команда',scenario:'Сценарий',dialogue:'Диалог',explanation:'Объяснение',context:'Контекст',long_answer:'Длинный ответ',device_error:'Ошибка устройства'};
        for (const [key, after] of Object.entries(event.benchmarks.after.categories)) {
          const before = event.benchmarks.before.categories[key];
          bench.append(el('p', `${categories[key] ?? key}: p50 ${seconds(before?.median_s)} → ${seconds(after.median_s)} · p95 ${seconds(before?.p95_s)} → ${seconds(after.p95_s)} · измерено ${before?.measured ?? 0} → ${after.measured}`));
        }
        bench.append(el('p', `Модель: ${event.benchmarks.after.configuration.model} · контекст ${event.benchmarks.after.configuration.num_ctx}. Холодная загрузка учитывается в отчёте отдельно.`, 'perf-note'));
      } else bench.append(el('p', 'Пара сопоставимых контрольных прогонов пока не сохранена.', 'perf-note'));
      node('performance-requests').replaceChildren(...event.requests.filter(row => row.kind === this.kind).map(row => {
        const button = el('button', '', 'perf-request') as HTMLButtonElement;
        const slow = row.first_content_s != null && row.first_content_s > 5;
        button.append(el('span', `${new Date(row.created_at).toLocaleString()} · ${row.kind === 'voice' ? 'Голос' : 'Текст'}${row.metadata.input_mode === 'replay' ? ' · запись' : ''}`),
          el('span', String(row.metadata.model ?? 'Локальная команда')),
          el('span', `Отклик ${seconds(row.first_content_s)} · всего ${seconds(row.total_s)}`),
          el('small', `${statuses[row.status] ?? row.status}${slow ? ' · Медленный ответ' : ''}`));
        button.dataset.slow = String(slow); button.onclick = () => this.open(row.request_id); return button;
      }));
      node('performance-activations').replaceChildren(...(event.activations ?? []).map(trace => { const reason: Record<string,string> = {uncertain_stt:'Низкая уверенность распознавания',not_addressed:'Нет обращения',own_tts_echo:'Эхо озвучки',third_person_discussion:'Разговор о другом человеке',explicit_address:'Прямое обращение',recognition_error:'Ошибка распознавания'}; const control=el('button',`${new Date(trace.created_at).toLocaleTimeString('ru-RU')} · ${reason[String(trace.metadata.reason)] ?? statuses[trace.status] ?? trace.status}`, 'perf-request') as HTMLButtonElement; control.onclick=()=>this.open(trace.request_id); return control; }));
      if (!(event.activations ?? []).length) node('performance-activations').append(el('p','Проверок обращения за выбранный период пока нет.','empty-state'));
      if (!event.requests.length) node('performance-requests').append(el('p', 'Новых трасс за этот период пока нет. Старые ответы задним числом не измеряются.', 'empty-state'));
    }
  }
  private tick(): void {
    if (!this.live) return;
    const elapsed = this.live.elapsed_s + (this.online && this.live.status === 'running' ? (performance.now() - this.received) / 1000 : 0);
    const active = this.live.stages.filter(s => s.end_s === null);
    node('performance-live').textContent = this.live.status === 'running'
      ? `${this.online ? 'Сейчас' : 'Последнее состояние'}: ${active.map(s => `${labels[s.stage] ?? s.stage} ${seconds(elapsed - s.start_s)}`).join(' · ') || 'Начало запроса'} · прошло ${seconds(elapsed)}`
      : `Последний запрос: ${statuses[this.live.status] ?? this.live.status} · отклик ${seconds(this.live.first_content_s)} · всего ${seconds(this.live.total_s)}`;
    if (this.online && this.live.status === 'running' && this.selected?.request_id === this.live.request_id) this.renderDetail({...this.live, elapsed_s: elapsed});
  }
  private renderDetail(trace: RequestTrace): void {
    const box = node('performance-detail');
    const duration = Math.max(trace.elapsed_s, .001);
    const spans = trace.stages.filter(s => !/first_|response_total|_attempt_/.test(s.stage));
    const longest = [...spans].sort((a,b) => timeline(b, duration).seconds - timeline(a, duration).seconds)[0];
    const heading = el('div', '', 'perf-detail-heading');
    const model = trace.stages.find(s => s.stage === 'model_total');
    const ttft = model && trace.marks.first_llm_token != null ? Math.max(0, trace.marks.first_llm_token - model.start_s) : null;
    const waitingEnd = Math.max(-1, ...trace.stages.filter(s => s.stage === 'waiting_playback' && s.end_s != null && trace.first_content_s != null && s.end_s <= trace.first_content_s).map(s => s.end_s!));
    const waitingGap = trace.waiting_to_content_gap_s ?? (waitingEnd >= 0 && trace.first_content_s != null ? trace.first_content_s - waitingEnd : null);
    heading.append(el('h2', `Запрос ${trace.request_id.slice(0, 8)}`),
      el('p', `${statuses[trace.status] ?? trace.status} · отклик ${seconds(trace.first_content_s)} · всего ${seconds(trace.total_s)}`),
      el('p', `${trace.metadata.provider ?? 'Локально'} · ${trace.metadata.model ?? 'Без LLM'} · обдумывание: ${trace.metadata.thinking == null ? '—' : trace.metadata.thinking ? 'включено' : 'выключено'}`),
      el('p', `Маршрут: ${trace.metadata.route ?? 'Не измерен'} · тип: ${trace.metadata.request_class ?? 'Не измерен'} · причина переключения: ${reasons[String(trace.metadata.reason)] ?? trace.metadata.reason ?? '—'}`),
      el('p', `LLM: до первого токена ${seconds(ttft)} · весь цикл ${seconds(model ? timeline(model, duration).seconds : null)}`),
      el('p', `Первый звук: ${seconds(trace.first_audio_s ?? trace.marks.first_audio)} · содержательная речь: ${seconds(trace.first_content_s)} · пауза после ожидания: ${seconds(waitingGap)} · паузы между фрагментами: ${seconds(trace.stages.filter(s=>s.stage==='playback_gap').reduce((sum,s)=>sum+s.seconds,0))}`),
      el('p', `Токены: вход ${trace.metadata.prompt_eval_count ?? '—'}, выход ${trace.metadata.eval_count ?? '—'} · fallback: ${trace.metadata.fallback ? 'да' : 'нет'} · повторы: ${trace.metadata.retry ?? 0}`),
      el('p', `Контекст: ${trace.metadata.context_messages ?? '—'} сообщений · ${trace.metadata.context_chars ?? '—'} символов · оценка ${trace.metadata.context_estimated_tokens ?? '—'} токенов · генерация ${typeof trace.metadata.tokens_per_second === 'number' ? trace.metadata.tokens_per_second.toFixed(1) : '—'} токенов/с`),
      el('p', `Самый длительный этап всего ответа: ${longest ? labels[longest.stage] ?? longest.stage : '—'}. Строки используют общую шкалу: пересекающиеся интервалы не суммируются.`));
    if (trace.kind === 'activation') {
      heading.append(el('p', `Whisper: logprob ${trace.metadata.min_avg_logprob ?? '—'} · no_speech_prob ${trace.metadata.max_no_speech_prob ?? '—'} · compression_ratio ${trace.metadata.max_compression_ratio ?? '—'}. Значения движка, без выдуманной вероятности обращения.`));
    }
    if (trace.metadata.provider === 'ollama') {
      const reported = (key: string) => typeof trace.metadata[key] === 'number' ? Number(trace.metadata[key]) / 1e9 : null;
      heading.append(el('p', `По данным Ollama: загрузка модели ${seconds(reported('load_duration'))} · обработка контекста ${seconds(reported('prompt_eval_duration'))} · генерация ${seconds(reported('eval_duration'))}`));
    }
    box.replaceChildren(heading);
    if (trace.errors.length) box.append(el('p', `Ошибки: ${trace.errors.join(', ')}`, 'perf-error'));
    const marks = el('p', Object.entries(trace.marks).map(([key, value]) => `${marksLabels[key] ?? key}: ${seconds(value)}`).join(' · '), 'perf-marks');
    box.append(marks);
    const axis = el('div', `0 с — ${seconds(duration)}`, 'perf-axis'); box.append(axis);
    for (const stage of spans) {
      const value = timeline(stage, duration);
      const row = el('div', '', 'perf-stage');
      row.append(el('span', `${labels[stage.stage] ?? stage.stage}${stage.metadata.tool ? ` · ${stage.metadata.tool}` : ''}${stage.metadata.model ? ` · ${stage.metadata.model}` : ''}${stage.metadata.attempt ? ` · попытка ${stage.metadata.attempt}` : ''}`));
      const track = el('div', '', 'perf-track');
      const bar = el('i', '', 'perf-bar'); bar.style.left = `${value.left}%`; bar.style.width = `${value.width}%`; bar.dataset.outcome = stage.outcome; track.append(bar);
      row.append(track, el('small', `${seconds(value.seconds)} · ${stage.outcome}${stage.metadata.reason ? ` · ${reasons[String(stage.metadata.reason)] ?? stage.metadata.reason}` : ''}${stage.metadata.http_status ? ` (${stage.metadata.http_status})` : ''}`)); box.append(row);
    }
  }
}

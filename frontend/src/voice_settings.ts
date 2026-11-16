import type { HudEvent } from './types';

export interface VoiceControls {
  sensitivity: 'conservative' | 'balanced' | 'sensitive';
  mode: 'name_only' | 'dialogue' | 'continuous'; continuation_s: number;
  allow_barge_in: boolean; noise_suppression: boolean;
  vad_threshold?: number|null; min_speech_ms?: number|null;
  background_filter?: 'balanced'|'strict';
}
export type VoiceSettingsEvent = ({type: 'voice_settings'} & VoiceControls & {
  activation_enabled: boolean; wake_source: string; wake_threshold: number;
  min_avg_logprob: number; max_no_speech_prob: number; noise_available: boolean;
  noise_active: boolean; microphone: string; state: string; session_phase:string;
}) | {type: 'voice_settings_result'; ok: boolean; message: string};

const node = (id: string) => document.getElementById(id)!;
function selectDuration(id:string,value:number):void {
  const select=node(id) as HTMLSelectElement;
  const stringValue=String(value);
  const preset=[...select.options].some(option=>option.value===stringValue);
  select.value=preset?stringValue:'custom';
  (node('voice-duration-custom') as HTMLInputElement).value=stringValue;
  node('voice-duration-custom-wrap').hidden=preset;
}
export class VoiceSettingsPanel {
  private online = false;
  private ready = false;
  constructor(private send: (value: VoiceControls) => void) {
    node('voice-duration').addEventListener('change',()=>{node('voice-duration-custom-wrap').hidden=(node('voice-duration') as HTMLSelectElement).value!=='custom';});
    node('voice-settings').addEventListener('submit', event => {
      event.preventDefault(); if (!this.online || !this.ready) return;
      const value: VoiceControls = {
        sensitivity: (node('voice-sensitivity') as HTMLSelectElement).value as VoiceControls['sensitivity'],
        mode: (node('voice-mode') as HTMLSelectElement).value as VoiceControls['mode'],
        continuation_s: (node('voice-duration') as HTMLSelectElement).value==='custom'
          ? Number((node('voice-duration-custom') as HTMLInputElement).value)
          : Number((node('voice-duration') as HTMLSelectElement).value),
        allow_barge_in: (node('voice-barge') as HTMLInputElement).checked,
        noise_suppression: (node('voice-noise') as HTMLInputElement).checked,
        vad_threshold:Number((node('voice-vad') as HTMLInputElement).value),
        min_speech_ms:Number((node('voice-min-speech') as HTMLInputElement).value),
        background_filter:(node('voice-background') as HTMLSelectElement).value as VoiceControls['background_filter'],
      };
      if (!(node('voice-settings') as HTMLFormElement).reportValidity()) return;
      node('voice-feedback').textContent = 'Применяю настройки…'; this.send(value);
    });
  }
  connection(online: boolean): void {
    this.online = online;
    node('voice-settings').querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLButtonElement>('input,select,button').forEach(input => input.disabled = !online || !this.ready);
    if (!online) { node('microphone-status').textContent = 'Нет соединения с микрофоном Чушпана'; node('voice-feedback').textContent = 'Применение изменений не подтверждено.'; }
  }
  receive(event: HudEvent): void {
    if (event.type === 'voice_settings_result') node('voice-feedback').textContent = event.message;
    if (event.type !== 'voice_settings') return;
    this.ready = true; this.connection(this.online);
    if (!node('voice-settings').contains(document.activeElement)) {
      (node('voice-sensitivity') as HTMLSelectElement).value = event.sensitivity;
      (node('voice-mode') as HTMLSelectElement).value = event.mode;
      selectDuration('voice-duration',event.continuation_s);
      (node('voice-barge') as HTMLInputElement).checked = event.allow_barge_in;
      (node('voice-noise') as HTMLInputElement).checked = event.noise_available && event.noise_suppression;
      (node('voice-vad') as HTMLInputElement).value=String(event.vad_threshold??0.5);
      (node('voice-min-speech') as HTMLInputElement).value=String(event.min_speech_ms??300);
      (node('voice-background') as HTMLSelectElement).value=event.background_filter??'balanced';
    }
    (node('voice-noise') as HTMLInputElement).disabled = !this.online || !event.noise_available;
    node('voice-session-limit').textContent = 'Таймер начинается после ответа и обновляется после каждой принятой реплики.';
    node('voice-thresholds').textContent = event.wake_source === 'whisper'
      ? `Whisper: средний logprob ≥ ${event.min_avg_logprob}; no_speech_prob ≤ ${event.max_no_speech_prob}. Это метрики декодера, не вероятность обращения.`
      : `Порог ONNX detector: ${event.wake_threshold}.`;
    const phases:Record<string,string>={idle:'Ожидает активации',wake_detected:'Слушает',listening:'Слушает',processing:'Думает',speaking:'Говорит',follow_up_listening:'Ожидает продолжения'};
    node('microphone-status').textContent = event.microphone === 'starting' ? 'Подготовка распознавания и голоса · микрофон ещё выключен'
      : event.microphone === 'stopped' ? 'Микрофон не запущен'
      : event.mode === 'continuous' ? `Постоянный диалог включён · ${phases[event.session_phase]??'Слушает'} · нажми кнопку микрофона, чтобы немедленно выключить`
      : event.session_phase === 'idle' ? 'Ожидает активации фразой «Эй, Чушпан»'
      : phases[event.session_phase]??(event.state === 'speaking' ? 'Говорит' : 'Слушает');
    node('voice-noise-status').textContent = !event.noise_available ? 'Модуль шумоподавления недоступен.' : event.noise_active ? 'Локальное шумоподавление WebRTC работает.' : 'Шумоподавление выключено.';
  }
}

import type { HudEvent } from './types';

export interface VoiceControls {
  sensitivity: 'conservative' | 'balanced' | 'sensitive';
  mode: 'name_only' | 'dialogue'; continuation_s: number;
  allow_barge_in: boolean; noise_suppression: boolean;
}
export type VoiceSettingsEvent = ({type: 'voice_settings'} & VoiceControls & {
  activation_enabled: boolean; wake_source: string; wake_threshold: number;
  min_avg_logprob: number; max_no_speech_prob: number; noise_available: boolean;
  noise_active: boolean; microphone: string; state: string; max_session_s: number;
}) | {type: 'voice_settings_result'; ok: boolean; message: string};

const node = (id: string) => document.getElementById(id)!;
export class VoiceSettingsPanel {
  private online = false;
  private ready = false;
  constructor(private send: (value: VoiceControls) => void) {
    node('voice-settings').addEventListener('submit', event => {
      event.preventDefault(); if (!this.online || !this.ready) return;
      const value: VoiceControls = {
        sensitivity: (node('voice-sensitivity') as HTMLSelectElement).value as VoiceControls['sensitivity'],
        mode: (node('voice-mode') as HTMLSelectElement).value as VoiceControls['mode'],
        continuation_s: Number((node('voice-duration') as HTMLInputElement).value),
        allow_barge_in: (node('voice-barge') as HTMLInputElement).checked,
        noise_suppression: (node('voice-noise') as HTMLInputElement).checked,
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
      (node('voice-duration') as HTMLInputElement).value = String(event.continuation_s);
      (node('voice-barge') as HTMLInputElement).checked = event.allow_barge_in;
      (node('voice-noise') as HTMLInputElement).checked = event.noise_available && event.noise_suppression;
    }
    (node('voice-noise') as HTMLInputElement).disabled = !this.online || !event.noise_available;
    node('voice-session-limit').textContent = `Предел одной сессии: ${event.max_session_s} с.`;
    node('voice-thresholds').textContent = event.wake_source === 'whisper'
      ? `Whisper: средний logprob ≥ ${event.min_avg_logprob}; no_speech_prob ≤ ${event.max_no_speech_prob}. Это метрики декодера, не вероятность обращения.`
      : `Порог ONNX detector: ${event.wake_threshold}.`;
    node('microphone-status').textContent = event.microphone === 'stopped' ? 'Микрофон не запущен'
      : !event.activation_enabled ? 'Внимание: включено постоянное прослушивание без обращения'
      : event.state === 'sleeping' ? 'Микрофон ждёт обращения «Эй, Чушпан»'
      : event.state === 'speaking' ? 'Идёт озвучка · проверяются только разрешённые перебивания'
      : 'Микрофон активен · окно разговора ограничено по времени';
    node('voice-noise-status').textContent = !event.noise_available ? 'Модуль шумоподавления недоступен.' : event.noise_active ? 'Локальное шумоподавление WebRTC работает.' : 'Шумоподавление выключено.';
  }
}

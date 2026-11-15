import type { Mode } from './core';

/** A small SVG scene: no animation loop, GPU canvas, recording or model inference. */
export class Character {
  private lastLevel = 0;
  constructor(private readonly host: HTMLElement, tap: () => void) {
    host.innerHTML = `<svg viewBox="0 0 320 320" role="img" aria-label="Персонаж Чушпан"><defs>
      <linearGradient id="cat-fur" x1=".18" y1="0" x2=".82" y2="1"><stop stop-color="#e2e0e6"/><stop offset=".58" stop-color="#c1bec8"/><stop offset="1" stop-color="#85818e"/></linearGradient>
      <linearGradient id="cat-shadow" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#aaa5b2"/><stop offset="1" stop-color="#716d79"/></linearGradient>
      <linearGradient id="cat-crown-fill" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#d8b8ec"/><stop offset="1" stop-color="#8f67ae"/></linearGradient>
    </defs><ellipse cx="160" cy="298" rx="103" ry="13" fill="#100e19" opacity=".34"/>
    <g class="cat-body">
      <path class="cat-tail" d="M102 245C68 250 50 226 60 202c6-15 24-20 35-9 7 7 5 17-1 21-4 3-9 2-12-1" fill="none" stroke="url(#cat-shadow)" stroke-width="17" stroke-linecap="round"/>
      <path d="M105 174c16-16 37-22 57-20 24-3 48 7 63 24 22 25 24 75 11 103-7 16-32 25-73 25s-66-9-73-25c-13-28-7-82 15-107Z" fill="url(#cat-fur)" stroke="#4c4654" stroke-width="3.5" stroke-linejoin="round"/>
      <path d="M116 224c4 22 17 35 31 35 9 0 15-7 15-17 0-11-8-20-20-24-11-4-21-2-26 6Zm88 0c-4 22-17 35-31 35-9 0-15-7-15-17 0-11 8-20 20-24 11-4 21-2 26 6Z" fill="#dedce2" opacity=".9"/>
      <path d="M77 112 77 48c0-8 8-12 14-6l44 35c17-5 35-5 52 0l44-35c6-6 14-2 14 6l0 64c7 16 10 33 5 49-10 32-42 48-95 48s-85-16-95-48c-5-16-2-33 5-49Z" fill="url(#cat-fur)" stroke="#4c4654" stroke-width="3.5" stroke-linejoin="round"/>
      <path d="m89 51 31 25c-10 5-19 12-25 21L89 51Zm142 0-31 25c10 5 19 12 25 21l6-46Z" fill="#ba8c9e" opacity=".72"/>
      <path d="M100 116q18-12 38 0m44 0q20-12 38 0" fill="none" stroke="#68606c" stroke-width="4" stroke-linecap="round"/>
      <g class="cat-eyes"><ellipse cx="119" cy="134" rx="15" ry="17" fill="#b99b61"/><ellipse cx="201" cy="134" rx="15" ry="17" fill="#b99b61"/><ellipse cx="120" cy="135" rx="4" ry="12" fill="#302a35"/><ellipse cx="200" cy="135" rx="4" ry="12" fill="#302a35"/><circle cx="116" cy="129" r="3" fill="#fff"/><circle cx="196" cy="129" r="3" fill="#fff"/></g>
      <g class="cat-closed-eyes" fill="none" stroke="#544a58" stroke-width="4" stroke-linecap="round"><path d="M103 136q16 12 32 0m50 0q16 12 32 0"/></g>
      <ellipse cx="103" cy="153" rx="13" ry="7" fill="#cd98a9" opacity=".45"/><ellipse cx="217" cy="153" rx="13" ry="7" fill="#cd98a9" opacity=".45"/>
      <path d="m156 151 8 0-4 6Z" fill="#916c7b"/><path class="cat-mouth" d="M160 157v5m0 0q-8 8-14 1m14-1q8 8 14 1" fill="none" stroke="#53434e" stroke-width="2.7" stroke-linecap="round"/>
      <ellipse class="cat-talking" cx="160" cy="166" rx="6" ry="8" fill="#765563"/>
      <path d="M140 279c1-12 8-19 20-19s19 7 20 19" fill="none" stroke="#89838f" stroke-width="3" stroke-linecap="round"/>
    </g><g class="cat-crown"><path d="m128 53-5-22 16 11 21-25 21 25 16-11-5 22Z" fill="url(#cat-crown-fill)" stroke="#71567e" stroke-width="2.5" stroke-linejoin="round"/><path d="m134 50 53 0" stroke="#f0dff5" stroke-width="2" stroke-linecap="round"/><circle cx="160" cy="43" r="3" fill="#f6e8c2"/></g>
    <g class="cat-sleep" fill="#dec9eb" font-family="system-ui" font-weight="600"><text x="246" y="79" font-size="18">z</text><text x="267" y="54" font-size="23">z</text></g>
    <g class="cat-success" fill="#d8c0eb"><path d="m259 115 3-9 3 9 9 3-9 3-3 9-3-9-9-3Z"/><path d="m54 98 2-6 2 6 6 2-6 2-2 6-2-6-6-2Z"/></g></svg>`;
    host.addEventListener('click', tap);
    host.addEventListener('animationend', event => { if (['cat-cheer', 'cat-error'].includes((event as AnimationEvent).animationName)) delete host.dataset.result; });
  }
  setMode(mode: Mode): void { this.host.dataset.mode = mode; }
  feed(value: number): void {
    if (performance.now() - this.lastLevel < 90) return;
    this.lastLevel = performance.now();
    this.host.style.setProperty('--speech-level', String(Math.min(1, Math.max(0, value))));
  }
  success(): void { this.host.dataset.result = 'success'; }
  error(): void { this.host.dataset.result = 'error'; }
  start(): void {} // same HUD surface as Core; no RAF or timers needed
}

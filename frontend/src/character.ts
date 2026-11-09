import type { Mode } from './core';

/** A small SVG scene: no animation loop, GPU canvas, recording or model inference. */
export class Character {
  private lastLevel = 0;
  constructor(private readonly host: HTMLElement, tap: () => void) {
    host.innerHTML = `<svg viewBox="0 0 290 285" role="img" aria-label="Персонаж Чушпан"><defs>
      <linearGradient id="cat-fur" x2="0.8" y2="1"><stop stop-color="#d9d7e1"/><stop offset="1" stop-color="#85808f"/></linearGradient>
      <linearGradient id="cat-quilt" x2="1" y2="1"><stop stop-color="#57506b"/><stop offset="1" stop-color="#282334"/></linearGradient>
      <linearGradient id="cat-crown-fill" x2="0" y2="1"><stop stop-color="#ff9dff"/><stop offset=".48" stop-color="#e93cff"/><stop offset="1" stop-color="#a31cce"/></linearGradient>
      <filter id="cat-crown-glow" x="-50%" y="-70%" width="200%" height="240%"><feGaussianBlur stdDeviation="3" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    </defs><ellipse cx="147" cy="265" rx="113" ry="13" fill="#100e19" opacity=".35"/>
    <g class="cat-body"><path d="M66 181C58 141 49 103 70 64L87 13Q91 2 100 12L145 51Q167 44 193 52L238 34Q250 29 249 43L247 102C261 132 258 177 238 200Z" fill="url(#cat-fur)" stroke="#261c30" stroke-width="4" stroke-linejoin="round"/>
    <path d="M83 30L98 20 123 51 99 60Z M219 58L239 45 238 78Z" fill="#bb829b" opacity=".7"/>
    <path d="M93 114Q111 105 127 110M181 105Q203 105 215 114" fill="none" stroke="#48303e" stroke-width="4" stroke-linecap="round"/>
    <g class="cat-eyes"><path d="M94 121Q111 140 129 118Z" fill="#dfc052"/><path d="M178 117Q195 138 214 120Z" fill="#dfc052"/><path d="M109 120v10M194 119v10" stroke="#352633" stroke-width="5"/></g>
    <g class="cat-closed-eyes" fill="none" stroke="#493040" stroke-width="4" stroke-linecap="round"><path d="M94 124Q110 132 129 122M179 122Q196 131 214 123"/></g>
    <ellipse cx="92" cy="141" rx="15" ry="7" fill="#d08ba0" opacity=".55"/><ellipse cx="218" cy="140" rx="15" ry="7" fill="#d08ba0" opacity=".55"/>
    <path d="M150 139l7 1-4 5Z" fill="#805165"/><path class="cat-mouth" d="M144 151q8 7 13 0q6 7 13-2" fill="none" stroke="#493040" stroke-width="3" stroke-linecap="round"/>
    <ellipse class="cat-talking" cx="156" cy="156" rx="6" ry="5" fill="#714452"/>
    <path d="M74 165Q127 181 246 168L255 240Q151 282 50 242Z" fill="url(#cat-quilt)" stroke="#292133" stroke-width="4"/>
    <path d="M62 188Q140 214 248 189M59 217Q134 238 251 217M91 177l-7 71M140 185l-2 72M190 181l9 71" fill="none" stroke="#85738e" opacity=".2" stroke-width="3"/>
    <path d="M51 243Q111 258 167 244L155 270Q96 281 44 260Z" fill="#3e354d" stroke="#282131" stroke-width="3"/>
    </g><g class="cat-crown" filter="url(#cat-crown-glow)"><path d="m111 48-5-26 19 13 19-28 20 28 20-14-6 27Z" fill="url(#cat-crown-fill)" stroke="#ffd2ff" stroke-width="3" stroke-linejoin="round"/><path d="m117 44 55 0" stroke="#fff0ff" stroke-width="2" stroke-linecap="round"/><circle cx="144" cy="40" r="3" fill="#fff3a8"/></g>
    <g class="cat-sleep" fill="#d8c0ef" font-family="system-ui" font-weight="600"><text x="240" y="47" font-size="18">z</text><text x="261" y="23" font-size="24">z</text></g>
    <g class="cat-success" fill="#d3b1ff"><path d="M252 95l3-10 3 10 10 3-10 3-3 10-3-10-10-3Z"/><path d="M35 80l2-7 2 7 7 2-7 2-2 7-2-7-7-2Z"/></g></svg>`;
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

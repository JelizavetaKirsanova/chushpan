type ThemeMode = 'system' | 'light' | 'dark';
const root = document.documentElement;
const control = document.getElementById('theme-mode') as HTMLSelectElement | null;
const media = window.matchMedia('(prefers-color-scheme: light)');
function storedMode(): ThemeMode {
  try {
    const value = localStorage.getItem('chushpan:theme');
    return value === 'light' || value === 'dark' || value === 'system' ? value : 'system';
  } catch { return 'system'; }
}
let mode = storedMode();
function apply(): void {
  root.dataset.themeMode = mode;
  root.dataset.theme = mode === 'system' ? (media.matches ? 'light' : 'dark') : mode;
  root.style.colorScheme = root.dataset.theme;
}
if (control) {
  control.value = mode;
  control.addEventListener('change', () => {
    mode = control.value as ThemeMode;
    try { localStorage.setItem('chushpan:theme', mode); } catch { /* Theme still applies for this session. */ }
    apply();
  });
}
media.addEventListener('change', () => { if (mode === 'system') apply(); });
apply();

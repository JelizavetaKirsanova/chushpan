import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../backend/media/music_tab.js', import.meta.url), 'utf8');

function fixture(options: {origin?: string; login?: boolean; reject?: boolean} = {}) {
  let plays = 0, pauses = 0, clicks = 0;
  const node = (textContent = '', attrs: Record<string, string> = {}) => ({
    textContent, disabled: false, getClientRects: () => [1],
    getAttribute: (name: string) => attrs[name] || null, click: () => { clicks++; },
  });
  const media = {paused: true, ended: false, readyState: 4, currentTime: 12, volume: .3,
    play: () => { plays++; return options.reject ? Promise.reject({name: 'NotAllowedError'}) : Promise.resolve(); },
    pause: () => { pauses++; },
  };
  const title = node('Song'), artist = node('Artist');
  const button = node('', {'aria-label': 'Воспроизвести Нервы'});
  const bar = {querySelector: (selector: string) => selector === '.title' ? title : selector === '.byline, .subtitle' ? artist : null,
    querySelectorAll: () => [], contains: () => false};
  const document = {
    documentElement: {dataset: {}},
    querySelector: (selector: string) => selector === 'ytmusic-player-bar' ? bar : selector === 'video, audio' ? media : null,
    querySelectorAll: (selector: string) => selector.startsWith('a[href*=') && options.login ? [node('Войти')]
      : selector === 'button, [role=button]' ? [button] : [],
  };
  const context = vm.createContext({document, location: {origin: options.origin || 'https://music.youtube.com', href: 'https://music.youtube.com/'}, URL});
  vm.runInContext(source, context);
  const call = (request: object) => {
    context.request = request;
    return JSON.parse(JSON.stringify(vm.runInContext('chushpanMusic(request)', context)));
  };
  return {call, counts: () => ({plays, pauses, clicks}), media};
}

test('inspect reports media evidence without changing playback', () => {
  const f = fixture(), state = f.call({op: 'inspect'});
  assert.equal(state.state, 'paused'); assert.equal(state.position, 12);
  assert.equal(state.track, 'Song'); assert.equal(state.artist, 'Artist');
  assert.deepEqual(f.counts(), {plays: 0, pauses: 0, clicks: 0});
});

test('wrong origin and logged-out state cannot start music', () => {
  const wrong = fixture({origin: 'https://youtube.com'});
  assert.equal(wrong.call({op: 'resume'}).error, 'tab_changed');
  const login = fixture({login: true});
  assert.equal(login.call({op: 'resume'}).error, 'authorization_required');
  assert.equal(login.counts().plays, 0);
});

test('autoplay promise rejection becomes a diagnostic reason', async () => {
  const f = fixture({reject: true});
  f.call({op: 'resume'}); await Promise.resolve();
  assert.equal(f.call({op: 'inspect'}).play_error, 'autoplay_blocked');
});

test('public player controls and Russian page play labels work', () => {
  const f = fixture();
  f.call({op: 'pause'}); f.call({op: 'volume', level: 40}); f.call({op: 'page_play'});
  assert.equal(f.media.volume, .4);
  assert.deepEqual(f.counts(), {plays: 0, pauses: 1, clicks: 1});
  assert.equal(f.call({op: 'next'}).error, 'control_missing');
});

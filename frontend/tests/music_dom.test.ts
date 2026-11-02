import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../backend/media/music_tab.js', import.meta.url), 'utf8');

function fixture(options: {origin?: string; login?: boolean; reject?: boolean; label?: string;
  playlistTitle?: string; pagePlaylist?: string} = {}) {
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
  const button = node('', {'aria-label': options.label || 'Воспроизвести Нервы'});
  const bar = {querySelector: (selector: string) => selector === '.title' ? title : selector === '.byline, .subtitle' ? artist : null,
    querySelectorAll: () => [], contains: () => false};
  const document = {
    documentElement: {dataset: {}},
    querySelector: (selector: string) => selector === 'ytmusic-player-bar' ? bar : selector === 'video, audio' ? media
      : selector === 'ytmusic-queue-header-renderer .subtitle' ? node(options.playlistTitle) : null,
    querySelectorAll: (selector: string) => selector.startsWith('a[href*=') && options.login ? [node('Войти')]
      : selector === 'button, [role=button]' ? [button] : [],
  };
  const context = vm.createContext({document, location: {origin: options.origin || 'https://music.youtube.com',
    pathname: options.pagePlaylist ? '/playlist' : '/',
    href: 'https://music.youtube.com/' + (options.pagePlaylist ? `playlist?list=${options.pagePlaylist}` : '')}, URL});
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

test('queue source and open page are separate evidence, never an invented player playlist ID', () => {
  const f = fixture({playlistTitle: 'Favourite Songs', pagePlaylist: 'actual-id'});
  const state = f.call({op: 'inspect'});
  assert.equal(state.playlist_title, 'Favourite Songs');
  assert.equal(state.page_playlist_id, 'actual-id');
  assert.equal(state.playlist_id, null);
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

test('library renderer yields only the actual playlist link without starting playback', () => {
  const anchor = {href: 'https://music.youtube.com/playlist?list=actual-id', textContent: 'Favourite Songs'};
  const thumbnail = {href: anchor.href, textContent: '   '};
  const row = {getClientRects: () => [1], getAttribute: () => null,
    querySelectorAll: () => [thumbnail, anchor], querySelector: () => null};
  const document = {
    querySelector: () => null,
    querySelectorAll: (selector: string) => selector.includes('ytmusic-responsive-list-item-renderer') ? [row] : [],
  };
  const context = vm.createContext({document, location: {origin: 'https://music.youtube.com', href: 'https://music.youtube.com/library/playlists'}, URL});
  vm.runInContext(source, context);
  const result = JSON.parse(JSON.stringify(vm.runInContext('chushpanMusic({op:"items",kind:"playlist"})', context)));
  assert.equal(result.items.length, 1);
  assert.equal(result.items[0].title, 'Favourite Songs');
  assert.equal(result.items[0].href, anchor.href);
});

test('playlist title before Russian play action is matched to the requested playlist', () => {
  const f = fixture({label: 'Favourite Songs\u00a0– воспроизвести'});
  assert.equal(f.call({op: 'page_play', title: 'Other Playlist'}).error, 'play_button_missing');
  assert.equal(f.counts().clicks, 0);
  assert.equal(f.call({op: 'page_play', title: 'Favourite Songs'}).requested, true);
  assert.equal(f.counts().clicks, 1);
});

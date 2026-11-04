/* Public HTMLMediaElement API only. No cross-origin iframe access or protection bypass. */
(function (request) {
  if (location.href !== request.url) return JSON.stringify({error: 'page_changed'});
  if (request.action === 'detach') {
    window.__chushpanCinema?.dispose(); delete window.__chushpanCinema;
    return JSON.stringify({detached:true});
  }
  const documents = [document];
  for (let index = 0; index < documents.length && index < 20; index++) {
    for (const frame of documents[index].querySelectorAll('iframe')) {
      try { if (frame.contentDocument) documents.push(frame.contentDocument); } catch (_) { /* same origin only */ }
    }
  }
  const videos = documents.flatMap(doc => [...doc.querySelectorAll('video')]);
  const eligible = videos.filter(video => video.readyState >= 1 && Number.isFinite(video.duration) && video.duration > 0);
  if (eligible.length !== 1) return JSON.stringify({error: eligible.length ? 'multiple_players' : 'no_player'});
  const video = eligible[0];
  if (video.mediaKeys) return JSON.stringify({error: 'protected_player'});
  const snapshot = () => ({position: video.currentTime, duration: video.duration, observed_at: Date.now(),
    paused: video.paused, ended: video.ended, ready: video.readyState,
    remote: video.webkitCurrentPlaybackTargetIsWireless === true || video.remote?.state === 'connected',
    airplay: typeof video.webkitShowPlaybackTargetPicker === 'function' && !video.disableRemotePlayback,
    error: video.error ? 'media_error' : undefined});
  const old = window.__chushpanCinema;
  if (request.session && (old?.session !== request.session || old?.video !== video)) {
    old?.dispose();
    const tracker = {sequence: old?.session === request.session ? old.sequence : 0};
    let last = 0;
    const listeners = [];
    const report = event => {
      if (event === 'timeupdate' && Date.now() - last < request.poll * 1000) return;
      last = Date.now();
      const value = {...snapshot(), event: ({timeupdate:'periodic', seeked:'seek', pagehide:'close'})[event] || event, sequence: ++tracker.sequence};
      if (!request.callback) return;
      // A capability limited to this episode. No cookies, user history or page content.
      try { navigator.sendBeacon(request.callback, JSON.stringify(value)); } catch (_) { /* polling remains available */ }
    };
    for (const event of ['timeupdate', 'pause', 'ended', 'seeked', 'playing']) {
      const listener = () => report(event); video.addEventListener(event, listener);
      listeners.push(() => video.removeEventListener(event, listener));
    }
    const close = () => report('pagehide');
    window.addEventListener('pagehide', close);
    window.__chushpanCinema = Object.assign(tracker, {session: request.session, video,
      dispose() { listeners.forEach(remove => remove()); window.removeEventListener('pagehide', close); }});
  }
  if (request.action === 'pause') video.pause();
  else if (request.action === 'play') {
    video.play().catch(() => {}); // Verification is a later independent observation.
  } else if (request.action === 'seek') {
    if (!Number.isFinite(request.position) || request.position < 0) return JSON.stringify({error:'invalid_position'});
    video.currentTime = Math.min(request.position, Math.max(0, video.duration - 0.1));
  }
  return JSON.stringify(snapshot());
})

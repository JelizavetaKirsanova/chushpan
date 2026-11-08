/* Public HTMLMediaElement API only. No cross-origin iframe access or protection bypass. */
(function (request) {
  if (location.href !== request.url) return JSON.stringify({error: 'page_changed'});
  if (request.action === 'detach') {
    window.__chushpanCinema?.dispose(); delete window.__chushpanCinema;
    return JSON.stringify({detached:true});
  }
  const documents = [document], roots = [document]; const seen = new Set(roots); let blockedFrame = false;
  // Public open shadow roots are used by real web-component players, including
  // Internet Archive. Closed roots and foreign iframe documents remain inaccessible.
  for (let index = 0; index < roots.length && index < 100; index++) {
    for (const element of roots[index].querySelectorAll('*')) {
      if(element.shadowRoot&&!seen.has(element.shadowRoot)){seen.add(element.shadowRoot);roots.push(element.shadowRoot);}
      if(element.tagName==='IFRAME'){
        try { const doc=element.contentDocument;if(doc&&!seen.has(doc)){seen.add(doc);roots.push(doc);documents.push(doc);}else if(!doc)blockedFrame=true; } catch (_) { blockedFrame=true; }
      }
    }
  }
  const videos = roots.slice(0,100).flatMap(root => [...root.querySelectorAll('video')]);
  if(request.action==='prepare'){
    const visible=videos.filter(v=>{const s=v.ownerDocument.defaultView.getComputedStyle(v);return s.display!=='none'&&s.visibility!=='hidden'&&v.getBoundingClientRect().width>0;});
    if(visible.length===1&&visible[0].readyState===0&&!visible[0].autoplay&&!visible[0].__chushpanMetadataRequested){
      const v=visible[0];v.__chushpanMetadataRequested=true;v.preload='metadata';v.load();
    }
    return JSON.stringify({preparing:visible.length===1,playback_confirmed:false});
  }
  let eligible = videos.filter(video => {const style=video.ownerDocument.defaultView.getComputedStyle(video);return style.display!=='none'&&style.visibility!=='hidden'&&video.getBoundingClientRect().width>0&&video.readyState>=1&&Number.isFinite(video.duration)&&video.duration>0;});
  // A single already playing element is unambiguous; its identity is still validated.
  const active = eligible.filter(video=>!video.paused&&!video.ended);
  if(eligible.length>1&&active.length===1)eligible=active;
  if (eligible.length !== 1) return JSON.stringify({error: eligible.length ? 'multiple_players' : videos.length===1&&videos[0].error?'media_error':videos.length?'player_loading':blockedFrame?'cross_origin_player':'no_player'});
  const video = eligible[0];
  if (video.mediaKeys) return JSON.stringify({error: 'protected_player'});
  if(!video.__chushpanMediaId)video.__chushpanMediaId=globalThis.crypto?.randomUUID?.()||String(window.__chushpanMediaCounter=(window.__chushpanMediaCounter||0)+1);
  const binding=()=>JSON.stringify([video.__chushpanMediaId,video.currentSrc,video.src,video.ownerDocument.URL,video.duration]);
  if(request.binding&&request.binding!==binding())return JSON.stringify({error:'player_changed'});
  const evidence=()=>({live:true,page_url:location.href,frame_url:video.ownerDocument.URL,
    page_title:document.title,video_title:video.getAttribute('title')||video.getAttribute('aria-label')||'',
    media_url:video.currentSrc||video.src,duration:video.duration,
    structured:documents.flatMap(doc=>[...doc.querySelectorAll('script[type="application/ld+json"]')].slice(0,8).flatMap(node=>{try{return [JSON.parse(node.textContent.slice(0,12000))];}catch{return [];}})).slice(0,16)});
  const fullscreen = () => {
    if(video.webkitDisplayingFullscreen===true||video.getRootNode().fullscreenElement===video)return true;
    const doc=video.ownerDocument, full=doc.fullscreenElement;
    if(!full||full===doc.body||full===doc.documentElement)return false;
    for(let node=video;node;node=node.parentNode||node.getRootNode?.().host)if(node===full){
      const screen=full.getBoundingClientRect(), media=video.getBoundingClientRect();
      return screen.width>0&&screen.height>0&&media.width>=screen.width*.8&&media.height>=screen.height*.8;
    }
    return false;
  };
  const tracks=()=>Array.from(video.audioTracks||[]);
  const normalize=value=>String(value||'').trim().toLocaleLowerCase();
  const desiredVoice=normalize(request.voice);
  const voiceConfirmed=()=>!!desiredVoice&&tracks().some(track=>track.enabled&&[track.label,track.language].some(value=>normalize(value)===desiredVoice));
  const snapshot = () => ({position: video.currentTime, duration: video.duration, observed_at: Date.now(),
    paused: video.paused, muted:video.muted, seeking:video.seeking, ended: video.ended, ready: video.readyState,
    remote: video.webkitCurrentPlaybackTargetIsWireless === true || video.remote?.state === 'connected',
    airplay: typeof video.webkitShowPlaybackTargetPicker === 'function' && !video.disableRemotePlayback,
    fullscreen:fullscreen(), fullscreen_pending:window.__chushpanCinema?.fullscreenPending===true, voice_confirmed:voiceConfirmed(), play_error:window.__chushpanCinema?.playError, fullscreen_error:window.__chushpanCinema?.fullscreenError,
    binding:binding(),
    capabilities:{play:true,pause:true,position:true,seek:video.seekable.length>0,
      fullscreen:fullscreen(),fullscreen_available:typeof (video.requestFullscreen||video.webkitEnterFullscreen||video.webkitEnterFullScreen)==='function',
      audio_tracks:tracks().map(track=>track.label||track.language).filter(Boolean)},
    error: video.mediaKeys ? 'protected_player' : video.error ? 'media_error' : undefined});
  const old = window.__chushpanCinema;
  if ((request.session || request.action!=='status') && (old?.session !== request.session || old?.video !== video)) {
    old?.dispose();
    const tracker = {sequence: old && old.session === request.session ? old.sequence : 0};
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
      playError:null, fullscreenError:null, fullscreenPending:false, overlay:null, gestureListeners:[],
      dispose() { listeners.forEach(remove => remove());this.gestureListeners.forEach(remove=>remove()); window.removeEventListener('pagehide', close);this.overlay?.remove(); }});
  }
  const tracker=window.__chushpanCinema;
  const failed = error => error?.name==='NotAllowedError'?'gesture_required':error?.name==='NotSupportedError'?'unsupported':'blocked';
  const enterFullscreen = () => {
    if(fullscreen())return;
    const method=video.requestFullscreen||video.webkitEnterFullscreen||video.webkitEnterFullScreen;
    if(!method){if(tracker)tracker.fullscreenError='unsupported';return;}
    if(tracker?.fullscreenPending)return;
    try {
      const result=method.call(video);
      if(result?.then){
        if(tracker)tracker.fullscreenPending=true;
        result.then(()=>{if(tracker)tracker.fullscreenPending=false;},error=>{if(tracker){tracker.fullscreenPending=false;tracker.fullscreenError=failed(error);}});
      }
    }
    catch(error){if(tracker)tracker.fullscreenError=failed(error);}
  };
  const play = () => {
    try { video.play()?.catch(error=>{if(tracker)tracker.playError=failed(error);}); }
    catch(error){if(tracker)tracker.playError=failed(error);}
  };
  const offerGesture = () => {
    if(!tracker||tracker.overlay)return;
    const button=video.ownerDocument.createElement('button');button.type='button';button.textContent='Чушпан: запустить видео на весь экран';
    button.setAttribute('aria-label',button.textContent);button.dataset.chushpanCinema='play-fullscreen';
    Object.assign(button.style,{position:'fixed',bottom:'24px',left:'50%',transform:'translateX(-50%)',zIndex:'2147483647',padding:'14px 22px',borderRadius:'14px',border:'1px solid #c29be5',background:'#5d3978',color:'white',font:'15px -apple-system,sans-serif',cursor:'pointer'});
    const offeredBinding=binding();
    button.addEventListener('click',()=>{if(!video.isConnected||binding()!==offeredBinding){button.textContent='Плеер изменился — повтори выбор в Чушпане';return;}tracker.playError=null;tracker.fullscreenError=null;play();enterFullscreen();});
    const hide=()=>{if(fullscreen()&&!video.paused){button.remove();tracker.overlay=null;}};
    for(const [target,event]of [[video,'playing'],[video,'webkitbeginfullscreen'],[video.ownerDocument,'fullscreenchange']]){
      target.addEventListener(event,hide);tracker.gestureListeners.push(()=>target.removeEventListener(event,hide));
    }
    video.ownerDocument.body.append(button);tracker.overlay=button;
  };
  if (request.action === 'mute'&&[0,1].includes(request.position))video.muted=request.position===1;
  else if (request.action === 'pause') video.pause();
  else if (request.action === 'play') {
    if(tracker)tracker.playError=null;play();
  } else if(request.action==='fullscreen') {
    if(tracker)tracker.fullscreenError=null;enterFullscreen();
  } else if(request.action==='offer_gesture') {
    offerGesture();
  } else if(request.action==='voice'&&desiredVoice) {
    const candidates=tracks().filter(track=>[track.label,track.language].some(value=>normalize(value)===desiredVoice));
    if(candidates.length===1)for(const track of tracks())track.enabled=track===candidates[0];
  } else if (request.action === 'seek') {
    if (!Number.isFinite(request.position) || request.position < 0) return JSON.stringify({error:'invalid_position'});
    video.currentTime = Math.min(request.position, Math.max(0, video.duration - 0.1));
  }
  return JSON.stringify({...snapshot(),...(request.action==='status'?{media_evidence:evidence()}: {})});
})

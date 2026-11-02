/* Fixed public-DOM operations only. No cookies, tokens, private player APIs or fetch. */
function chushpanMusic(request) {
  if (location.origin !== "https://music.youtube.com") return {error: "tab_changed"};
  const text = node => (node?.textContent || "").trim().replace(/\s+/g, " ");
  const visible = node => !!(node && node.getClientRects().length && !node.disabled && node.getAttribute("aria-disabled") !== "true");
  const bar = document.querySelector("ytmusic-player-bar");
  const media = document.querySelector("video, audio");
  const loggedOut = [...document.querySelectorAll('a[href*="accounts.google.com"]')]
    .some(node => visible(node) && /sign in|войти|logi sisse/i.test(text(node)));
  function state() {
    return {state: !media ? "unknown" : media.ended ? "stopped" : media.paused ? "paused" : "playing",
      track: text(bar?.querySelector(".title")), artist: text(bar?.querySelector(".byline, .subtitle")),
      position: media?.currentTime || 0, ready: !!media && media.readyState >= 2,
      volume: media ? Math.round(media.volume * 100) : null,
      muted: !!media?.muted,
      ad: !!document.querySelector(".ad-showing"), logged_out: loggedOut,
      signed_in: !!document.querySelector('ytmusic-settings-button img, button[aria-label="Account menu"], button[aria-label="Меню аккаунта"]'),
      playlist_id: (() => {const a=bar?.querySelector('a[href*="list="]'); return a ? new URL(a.href, location.href).searchParams.get("list") : null;})(),
      // The current UI can expose only an album link in the player bar. Keep the
      // visible queue source and page ID separate: an open page alone isn't proof.
      playlist_title: text(document.querySelector('ytmusic-queue-header-renderer .subtitle')),
      page_playlist_id: location.pathname === "/playlist" ? new URL(location.href).searchParams.get("list") : null,
      play_error: document.documentElement.dataset.chushpanMusicPlayError || null};
  }
  if (request.op === "inspect") return state();
  if (loggedOut) return {error: "authorization_required"};
  if (request.op === "items") {
    const results = [];
    for (const row of document.querySelectorAll("ytmusic-responsive-list-item, ytmusic-responsive-list-item-renderer, ytmusic-two-row-item-renderer")) {
      if (!visible(row)) continue;
      const links = [...row.querySelectorAll("a[href]")];
      const wanted = request.kind || "song";
      const anchor = links.find(node => {
        // The thumbnail link precedes the title and can have the same href but no text.
        if (!text(node)) return false;
        const u = new URL(node.href, location.href);
        if (u.origin !== location.origin) return false;
        if (wanted === "song") return u.pathname === "/watch" && u.searchParams.has("v");
        if (wanted === "playlist") return u.pathname === "/playlist" && u.searchParams.has("list");
        if (wanted === "artist") return /^(?:\/channel\/|\/browse\/UC)/.test(u.pathname);
        return wanted === "album" && u.pathname.startsWith("/browse/") && /album|альбом/i.test(text(row));
      });
      if (anchor && text(anchor)) results.push({title: text(anchor), href: anchor.href,
        artist: text(row.querySelector(".secondary-flex-columns, .subtitle")), kind: wanted});
      if (results.length >= 20) break;
    }
    return {items: results};
  }
  if (request.op === "page_play") {
    const normalize = value => value.normalize("NFKC").trim().replace(/[–—]/g, "-").replace(/\s+/g, " ").toLowerCase();
    const title = normalize(request.title || "");
    const actions = ["play", "play all", "воспроизвести", "воспроизвести всё", "esita", "микс", "mix", "play radio"];
    const buttons = [...document.querySelectorAll("button, [role=button]")].filter(node => {
      if (!visible(node) || bar?.contains(node)) return false;
      const label = normalize(node.getAttribute("aria-label") || text(node));
      if (!title) return /^(play|play all|воспроизвести|воспроизвести всё|esita|микс|mix|play radio)(?=\s|$)/i.test(label);
      return actions.some(action => label === action || label === `${action} ${title}` || label === `${title} - ${action}`);
    });
    if (buttons.length !== 1) return {error: "play_button_missing"};
    buttons[0].click(); return {requested: true};
  }
  if (request.op === "next" || request.op === "previous") {
    const names = request.op === "next" ? /^(next|next song|далее|следующий трек|järgmine)$/i : /^(previous|previous song|назад|предыдущий трек|eelmine)$/i;
    const button = [...(bar?.querySelectorAll("button, [role=button]") || [])]
      .find(node => visible(node) && names.test(node.getAttribute("aria-label") || text(node)));
    if (!button) return {error: "control_missing"};
    button.click(); return {requested: true};
  }
  if (!media) return {error: "no_media"};
  if (request.op === "resume") {
    delete document.documentElement.dataset.chushpanMusicPlayError;
    try { media.play().catch(error => {
      document.documentElement.dataset.chushpanMusicPlayError = error.name === "NotAllowedError" ? "autoplay_blocked" : "playback_blocked";
    }); } catch (error) {
      return {error: error.name === "NotAllowedError" ? "autoplay_blocked" : "playback_blocked"};
    }
  } else if (request.op === "pause") media.pause();
  else if (request.op === "volume") media.volume = request.level / 100;
  else if (request.op === "seek") media.currentTime = request.relative ? media.currentTime + request.seconds : request.seconds;
  else return {error: "unsupported"};
  return state();
}

import type {CinemaTitle} from './cinema';

const paths = {
  bookmark: '<path d="M6 4h12v17l-6-4-6 4z"/>',
  play: '<circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4z"/>',
  watched: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  favorite: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8z"/>',
};

/** Sibling of the poster button: no nested interactive elements or detail clicks. */
export function posterActions(
  current: () => CinemaTitle | undefined,
  change: (values: object) => void,
  watch: () => void,
  online: boolean,
): HTMLElement {
  const panel = document.createElement('div');
  panel.className = 'cinema-poster-actions';
  panel.setAttribute('role', 'group');
  panel.setAttribute('aria-label', 'Действия с фильмом');
  panel.onclick = event => event.stopPropagation();
  for (const key of ['bookmark', 'play', 'watched', 'favorite'] as const) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `cinema-quick-${key}${key === 'favorite' ? ' cinema-favorite' : ''}`;
    button.dataset.posterAction = key;
    button.disabled = !online;
    button.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[key]}</svg>`;
    button.onclick = event => {
      event.stopPropagation();
      const item = current();
      if (key === 'play') watch();
      else if (key === 'bookmark') change({watchlist: !(!!item && (item.watchlist == null ? item.status === 'planned' : !!item.watchlist))});
      else if (key === 'watched') change({watched: item?.status !== 'watched'});
      else change({favorite: !item?.favorite});
    };
    panel.append(button);
  }
  syncPosterActions(panel, current());
  return panel;
}

export function syncPosterActions(panel: HTMLElement, item?: CinemaTitle): void {
  const planned = !!item && (item.watchlist == null ? item.status === 'planned' : !!item.watchlist);
  const watched = item?.status === 'watched';
  const favorite = !!item?.favorite;
  const labels = {
    bookmark: planned ? 'Убрать из «Хочу посмотреть»' : 'Хочу посмотреть',
    play: item?.current?.position ? 'Продолжить просмотр' : 'Смотреть',
    watched: watched ? 'Снять отметку «Просмотрено»' : 'Просмотрено',
    favorite: favorite ? 'Убрать из избранного' : 'В избранное',
  };
  for (const button of panel.querySelectorAll<HTMLButtonElement>('button')) {
    const key = button.dataset.posterAction as keyof typeof labels;
    button.title = labels[key];
    button.setAttribute('aria-label', labels[key]);
    if (key !== 'play') button.setAttribute('aria-pressed', String(key === 'bookmark' ? planned : key === 'watched' ? watched : favorite));
  }
}

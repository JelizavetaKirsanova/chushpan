export type LocalMediaKind = 'music' | 'video' | 'image' | 'document';

export interface LocalMediaItem {
  id: string;
  name: string;
  kind: LocalMediaKind;
  mime: string;
  size: number;
  addedAt: string;
  favorite: boolean;
  playlistIds: string[];
  position: number;
  duration: number;
}

interface StoredMediaItem extends LocalMediaItem {
  blob: Blob;
}

const DB_NAME = 'chushpan-local-media';
const STORE_NAME = 'files';
const MEDIA_KIND: Record<LocalMediaKind, string> = {
  music: 'Музыка', video: 'Видео', image: 'Изображения', document: 'Документы',
};

export function classifyMedia(name: string, mime = ''): LocalMediaKind | null {
  const ext = name.toLocaleLowerCase().split('.').pop() ?? '';
  if (mime.startsWith('audio/') || /^(mp3|m4a|aac|wav|aiff|flac|ogg|opus)$/.test(ext)) return 'music';
  if (mime.startsWith('video/') || /^(mp4|m4v|mov|webm|mkv|avi)$/.test(ext)) return 'video';
  if (mime.startsWith('image/') || /^(png|jpe?g|gif|webp|heic|heif|svg|tiff?)$/.test(ext)) return 'image';
  if (mime === 'application/pdf' || /^(pdf|txt|md|rtf|docx?|pages|odt|xlsx?|pptx?)$/.test(ext)) return 'document';
  return null;
}

export class LocalMediaStore {
  private database: Promise<IDBDatabase> | null = null;

  supported(): boolean {
    return typeof indexedDB !== 'undefined';
  }

  async list(): Promise<LocalMediaItem[]> {
    const rows = await this.allStored();
    return rows.map(({blob: _blob, ...item}) => item).sort((a, b) => b.addedAt.localeCompare(a.addedAt));
  }

  async import(files: FileList | File[]): Promise<LocalMediaItem[]> {
    const accepted = Array.from(files).filter(file => classifyMedia(file.name, file.type));
    if (!accepted.length) throw new Error('Выбранные файлы имеют неподдерживаемый формат.');
    const estimate = await navigator.storage?.estimate?.().catch(() => undefined);
    const free = estimate?.quota && estimate.usage !== undefined ? estimate.quota - estimate.usage : null;
    const bytes = accepted.reduce((sum, file) => sum + file.size, 0);
    if (free !== null && bytes > free * 0.9) {
      throw new Error('Для импорта не хватает места в локальном хранилище браузера. Освободи место и повтори.');
    }
    const db = await this.open();
    const tx = db.transaction(STORE_NAME, 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    const added: LocalMediaItem[] = [];
    for (const file of accepted) {
      const kind = classifyMedia(file.name, file.type);
      if (!kind) continue;
      const item: StoredMediaItem = {
        id: globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        name: file.name,
        kind,
        mime: file.type || this.mimeFromName(file.name),
        size: file.size,
        addedAt: new Date().toISOString(),
        favorite: false,
        playlistIds: [],
        position: 0,
        duration: 0,
        blob: file,
      };
      store.add(item);
      const {blob: _blob, ...metadata} = item;
      added.push(metadata);
    }
    await this.complete(tx);
    if (navigator.storage?.persist) void navigator.storage.persist().catch(() => false);
    return added;
  }

  async file(id: string): Promise<Blob | null> {
    const item = await this.getStored(id);
    return item?.blob ?? null;
  }

  async update(id: string, values: Partial<Pick<LocalMediaItem, 'favorite' | 'playlistIds' | 'position' | 'duration'>>): Promise<void> {
    const item = await this.getStored(id);
    if (!item) throw new Error('Файл больше не найден в медиатеке.');
    const db = await this.open();
    const tx = db.transaction(STORE_NAME, 'readwrite');
    tx.objectStore(STORE_NAME).put({...item, ...values});
    await this.complete(tx);
  }

  async remove(id: string): Promise<void> {
    const db = await this.open();
    const tx = db.transaction(STORE_NAME, 'readwrite');
    tx.objectStore(STORE_NAME).delete(id);
    await this.complete(tx);
  }

  async removePlaylist(id: string): Promise<void> {
    const items = await this.allStored();
    const db = await this.open();
    const tx = db.transaction(STORE_NAME, 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    for (const item of items) {
      if (item.playlistIds.includes(id)) store.put({...item, playlistIds: item.playlistIds.filter(value => value !== id)});
    }
    await this.complete(tx);
  }

  private async allStored(): Promise<StoredMediaItem[]> {
    const db = await this.open();
    return this.request<StoredMediaItem[]>(db.transaction(STORE_NAME).objectStore(STORE_NAME).getAll());
  }

  private async getStored(id: string): Promise<StoredMediaItem | null> {
    const db = await this.open();
    return (await this.request<StoredMediaItem | undefined>(db.transaction(STORE_NAME).objectStore(STORE_NAME).get(id))) ?? null;
  }

  private open(): Promise<IDBDatabase> {
    if (this.database) return this.database;
    this.database = new Promise((resolve, reject) => {
      if (!this.supported()) { reject(new Error('Локальное хранилище недоступно в этом окне Чушпана.')); return; }
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => request.result.createObjectStore(STORE_NAME, {keyPath: 'id'});
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(new Error('Не удалось открыть локальную медиатеку.'));
      request.onblocked = () => reject(new Error('Закрой другие окна Чушпана и повтори импорт.'));
    }).catch(error => { this.database = null; throw error; });
    return this.database;
  }

  private request<T>(request: IDBRequest<T>): Promise<T> {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(new Error('Не удалось прочитать локальную медиатеку.'));
    });
  }

  private complete(tx: IDBTransaction): Promise<void> {
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(new Error('Не удалось сохранить выбранный файл.'));
      tx.onabort = () => reject(new Error('Сохранение файла отменено хранилищем.'));
    });
  }

  private mimeFromName(name: string): string {
    const ext = name.toLocaleLowerCase().split('.').pop() ?? '';
    const known: Record<string, string> = {mp3:'audio/mpeg',m4a:'audio/mp4',wav:'audio/wav',flac:'audio/flac',ogg:'audio/ogg',mp4:'video/mp4',m4v:'video/mp4',mov:'video/quicktime',webm:'video/webm',pdf:'application/pdf',txt:'text/plain',md:'text/markdown'};
    return known[ext] ?? '';
  }
}

export const localMediaLabels = MEDIA_KIND;

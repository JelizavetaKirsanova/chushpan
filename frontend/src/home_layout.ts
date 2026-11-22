export const homeWidgets = [
  {id: 'shortcuts', label: 'Быстрые действия', size: 'wide'},
  {id: 'resume', label: 'Продолжить просмотр', size: 'wide'},
  {id: 'events', label: 'Ближайшие события', size: 'wide'},
  {id: 'conversations', label: 'Недавние разговоры', size: 'wide'},
  {id: 'modules', label: 'Мои разделы', size: 'wide'},
  {id: 'location', label: 'Местоположение', size: 'compact'},
  {id: 'weather', label: 'Погода', size: 'compact'},
  {id: 'calendar', label: 'Календарь', size: 'compact'},
  {id: 'goals', label: 'Фокус недели', size: 'wide'},
  {id: 'favorite-scenarios', label: 'Избранные сценарии', size: 'wide'},
  {id: 'favorite-devices', label: 'Избранные устройства', size: 'wide'},
] as const;

export type HomeWidgetId = (typeof homeWidgets)[number]['id'];
export type HomeWidgetSize = 'compact' | 'wide';
export const homeActions = [
  {id: 'mac-control', label: 'Управление Mac'},
  {id: 'my-devices', label: 'Мои устройства'},
  {id: 'lighting', label: 'Освещение'},
  {id: 'automations', label: 'Сценарии'},
  {id: 'cinema', label: 'Медиатека'},
  {id: 'devices', label: 'Устройства'},
  {id: 'calendar', label: 'Календарь'},
  {id: 'memory', label: 'Память'},
] as const;

export type HomeActionId = (typeof homeActions)[number]['id'];
export const defaultHomeWidgets: HomeWidgetId[] = ['shortcuts', 'resume', 'conversations', 'events', 'weather', 'goals'];
export const defaultHomeActions: HomeActionId[] = [];

export interface HomeLayout {
  widgets: HomeWidgetId[];
  sizes: Partial<Record<HomeWidgetId, HomeWidgetSize>>;
  actions: HomeActionId[];
}

export function parseHomeLayout(value: string | null): HomeLayout {
  if (!value) return {widgets: [...defaultHomeWidgets], sizes: {}, actions: [...defaultHomeActions]};
  try {
    const saved = JSON.parse(value) as Partial<{widgets: unknown[]; sizes: Record<string, unknown>; actions: unknown[]}>;
    const knownWidgets = new Set<string>(homeWidgets.map(item => item.id));
    const knownActions = new Set<string>(homeActions.map(item => item.id));
    const widgets = Array.isArray(saved.widgets)
      ? unique(saved.widgets.filter((item): item is HomeWidgetId => typeof item === 'string' && knownWidgets.has(item)))
      : [...defaultHomeWidgets];
    const actions = Array.isArray(saved.actions)
      ? unique(saved.actions.filter((item): item is HomeActionId => typeof item === 'string' && knownActions.has(item)))
      : [...defaultHomeActions];
    const sizes: HomeLayout['sizes'] = {};
    if (saved.sizes && typeof saved.sizes === 'object') {
      for (const widget of homeWidgets) {
        const size = saved.sizes[widget.id];
        if (size === 'compact' || size === 'wide') sizes[widget.id] = size;
      }
    }
    return {widgets, sizes, actions};
  } catch {
    return {widgets: [...defaultHomeWidgets], sizes: {}, actions: [...defaultHomeActions]};
  }
}

export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (from < 0 || from >= items.length || to < 0 || to >= items.length || from === to) return [...items];
  const next = [...items];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item!);
  return next;
}

function unique<T extends string>(items: T[]): T[] {
  return [...new Set(items)];
}

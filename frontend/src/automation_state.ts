/** Shared data-only editor operations; snapshots and location variants stay independent. */
export type Json = string | number | boolean | null | {[key: string]: Json} | Json[];
export interface Condition {source: 'variable' | 'available' | 'time' | 'location'; key: string; operator: 'eq' | 'ne' | 'gt' | 'lt'; value: Json}
export interface Block {kind: 'action' | 'delay' | 'sequence' | 'parallel' | 'if'; capability: string; parameters: Record<string, Json>; seconds: number; condition: Condition | null; children: Block[]; otherwise: Block[]; on_error: 'stop' | 'continue'; enabled: boolean; required: boolean}
export interface Variant {id: string; name: string; place_id: string; enabled: boolean; actions: Block[]; conditions: Condition[]}
export interface Place {id: string; revision: number; name: string; cities: string[]; wifi_networks?: {ssid:string;bssid:string}[]; zones?: {latitude:number;longitude:number;radius_km:number}[]; signals?: {kind:string;key:string;name:string}[]}
export interface Scenario {id: string; revision: number; name: string; description: string; icon: string; color: string; tags: string[]; favorite: boolean; created_at?: string; updated_at?: string; enabled: boolean; mode: 'exact' | 'smart'; phrases: string[]; actions: Block[]; variants: Variant[]; conditions: Condition[]; triggers: {kind: 'voice' | 'manual' | 'schedule' | 'condition'; at: string; weekdays: number[]}[]; variables: {name: string; kind: 'number' | 'string' | 'boolean'; default: Json}[]; confirmation: 'auto' | 'always'; schema_version: 2; version_policy: 'fallback'; general_enabled: true; inter_block_delay_s: number; show_status: boolean; error_policy: 'block' | 'stop' | 'continue'; timeout_s: number | null}
export const clone = <T>(value: T): T => structuredClone(value);
export const newId = (): string => crypto.randomUUID().replaceAll('-', '');
export function newBlock(kind: Block['kind'] = 'action', capability = ''): Block {
  return {kind, capability, parameters: {}, seconds: 1, condition: kind === 'if' ? {source: 'time', key: 'hour', operator: 'gt', value: 21} : null, children: [], otherwise: [], on_error: 'stop', enabled: true, required: true};
}
export function newScenario(): Scenario {
  return {id: '', revision: 0, name: 'Новый сценарий', description: '', icon: '✦', color: '#9B4BD3', tags: [], favorite: false, enabled: true, mode: 'smart', phrases: [], actions: [], variants: [], conditions: [], triggers: [{kind: 'voice', at: '22:00', weekdays: [0,1,2,3,4,5,6]}, {kind: 'manual', at: '22:00', weekdays: [0,1,2,3,4,5,6]}], variables: [], confirmation: 'auto', schema_version: 2, version_policy: 'fallback', general_enabled: true, inter_block_delay_s: 0, show_status: true, error_policy: 'block', timeout_s: 3600};
}

/** Guard the renderer before replacing a draft; semantic validation remains on the backend. */
export function parseScenarioImport(text: string): Scenario {
  const fail = (): never => {throw new Error('Некорректная структура JSON сценария. Проверь файл экспорта.');};
  const object = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : fail();
  const array = (value: unknown): unknown[] => Array.isArray(value) ? value : fail();
  const shape = (value: unknown, template: object): void => {
    const record = object(value);
    for (const [key, expected] of Object.entries(template)) {
      if (expected === null) continue;
      if (Array.isArray(expected)) array(record[key]);
      else if (typeof record[key] !== typeof expected || record[key] === null) fail();
    }
  };
  const condition = (value: unknown): void => {
    shape(value, {source: '', key: '', operator: ''});
    const record = object(value);
    if (!['variable', 'available', 'time', 'location'].includes(String(record.source)) || !['eq', 'ne', 'gt', 'lt'].includes(String(record.operator)) || !('value' in record)) fail();
  };
  let total = 0;
  const blocks = (value: unknown, depth = 0): void => {
    const items = array(value); total += items.length;
    if (depth > 12 || total > 500) fail();
    for (const item of items) {
      shape(item, newBlock());
      const block = object(item);
      if (!['action', 'delay', 'sequence', 'parallel', 'if'].includes(String(block.kind))) fail();
      object(block.parameters);
      if (block.kind === 'if') condition(block.condition);
      blocks(block.children, depth + 1); blocks(block.otherwise, depth + 1);
    }
  };
  const parsed = object(JSON.parse(text));
  if (parsed.format !== 'chushpan-scenario' || parsed.schema_version !== 2) fail();
  const imported = {...newScenario(), ...object(parsed.scenario), id: '', revision: 0};
  shape(imported, newScenario());
  if (imported.schema_version !== 2) fail();
  for (const key of ['tags', 'phrases'] as const) if (array(imported[key]).some(value => typeof value !== 'string')) fail();
  array(imported.conditions).forEach(condition);
  blocks(imported.actions);
  for (const item of array(imported.variants)) {
    shape(item, {id: '', name: '', place_id: '', enabled: true, actions: [], conditions: []});
    const variant = object(item); total = 0; blocks(variant.actions); array(variant.conditions).forEach(condition);
  }
  for (const item of array(imported.variables)) {
    shape(item, {name: '', kind: ''});
    const variable = object(item);
    if (!['number', 'string', 'boolean'].includes(String(variable.kind)) || typeof variable.default !== variable.kind) fail();
  }
  for (const item of array(imported.triggers)) {
    shape(item, {kind: '', at: '', weekdays: []});
    const trigger = object(item);
    if (!['voice', 'manual', 'schedule', 'condition'].includes(String(trigger.kind)) || array(trigger.weekdays).some(day => !Number.isInteger(day) || Number(day) < 0 || Number(day) > 6)) fail();
  }
  delete imported.created_at; delete imported.updated_at;
  return imported;
}
export function addVariant(scenario: Scenario, place: Place, source?: Variant): Variant {
  if (scenario.variants.some(v => v.place_id === place.id)) throw new Error('Для этого места уже есть версия.');
  const variant: Variant = source ? {...clone(source), id: newId(), place_id: place.id, name: place.name} : {id: newId(), name: place.name, place_id: place.id, enabled: true, actions: [], conditions: []};
  scenario.variants.push(variant); return variant;
}
export function moveBlock(blocks: Block[], index: number, offset: number): void {
  const target = index + offset;
  if (index < 0 || index >= blocks.length || target < 0 || target >= blocks.length) return;
  [blocks[index], blocks[target]] = [blocks[target], blocks[index]];
}
export function searchCommands(scenarios: Scenario[], query: string): Scenario[] {
  const value = query.trim().toLocaleLowerCase('ru');
  return scenarios.filter(s => [s.name, s.description, ...s.tags, ...s.phrases, ...s.variants.map(v => v.name)].some(text => text.toLocaleLowerCase('ru').includes(value)));
}

/** A drop moves the item, preserving the relative order of every other block. */
export function reorderBlock(blocks: Block[], from: number, to: number): boolean {
  if (from === to || from < 0 || to < 0 || from >= blocks.length || to >= blocks.length) return false;
  blocks.splice(to, 0, blocks.splice(from, 1)[0]); return true;
}
export function countBlocks(blocks: Block[]): number {
  return blocks.reduce((count, block) => count + 1 + countBlocks(block.children) + countBlocks(block.otherwise), 0);
}
export function normalizePhrase(value: string): string {
  return value.toLocaleLowerCase('ru').replaceAll('ё', 'е').replace(/[^\p{L}\p{N}_\s]/gu, ' ').replace(/(?:^|\s)(?:пожалуйста|ну|давай|ка)(?=\s|$)/gu, ' ').trim().replace(/\s+/g, ' ');
}

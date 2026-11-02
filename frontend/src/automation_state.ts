/** Shared data-only editor operations; snapshots and location variants stay independent. */
export type Json = string | number | boolean | null | {[key: string]: Json} | Json[];
export interface Condition {source: 'variable' | 'available' | 'time'; key: string; operator: 'eq' | 'ne' | 'gt' | 'lt'; value: Json}
export interface Block {kind: 'action' | 'delay' | 'sequence' | 'parallel' | 'if'; capability: string; parameters: Record<string, Json>; seconds: number; condition: Condition | null; children: Block[]; otherwise: Block[]; on_error: 'stop' | 'continue'; enabled: boolean; required: boolean}
export interface Variant {id: string; name: string; place_id: string; enabled: boolean; actions: Block[]; conditions: Condition[]}
export interface Place {id: string; revision: number; name: string; cities: string[]}
export interface Scenario {id: string; revision: number; name: string; description: string; icon: string; enabled: boolean; mode: 'exact' | 'smart'; phrases: string[]; actions: Block[]; variants: Variant[]; conditions: Condition[]; triggers: {kind: 'voice' | 'manual' | 'schedule'; at: string; weekdays: number[]}[]; variables: {name: string; kind: 'number' | 'string' | 'boolean'; default: Json}[]; confirmation: 'auto' | 'always'}
export const clone = <T>(value: T): T => structuredClone(value);
export const newId = (): string => crypto.randomUUID().replaceAll('-', '');
export function newBlock(kind: Block['kind'] = 'action', capability = ''): Block {
  return {kind, capability, parameters: {}, seconds: 1, condition: kind === 'if' ? {source: 'variable', key: '', operator: 'eq', value: true} : null, children: [], otherwise: [], on_error: 'stop', enabled: true, required: true};
}
export function newScenario(): Scenario {
  return {id: '', revision: 0, name: 'Новая команда', description: '', icon: '✦', enabled: true, mode: 'smart', phrases: [], actions: [], variants: [], conditions: [], triggers: [{kind: 'voice', at: '22:00', weekdays: [0,1,2,3,4,5,6]}, {kind: 'manual', at: '22:00', weekdays: [0,1,2,3,4,5,6]}], variables: [], confirmation: 'auto'};
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
  return scenarios.filter(s => [s.name, s.description, ...s.phrases, ...s.variants.map(v => v.name)].some(text => text.toLocaleLowerCase('ru').includes(value)));
}

import test from 'node:test';
import assert from 'node:assert/strict';
import {addVariant, clone, moveBlock, newBlock, newScenario, searchCommands} from '../src/automation_state.ts';

const dorm = {id:'dorm',revision:1,name:'Общага',cities:[]};
const home = {id:'home',revision:1,name:'Дом',cities:[]};

test('one command keeps phrases and common actions while creating missing place versions', () => {
  const command = newScenario(); command.name = 'Я спать'; command.phrases = ['Я спать', 'Пора ложиться'];
  command.actions.push({...newBlock('action','mac.brightness'),parameters:{level:10}});
  assert.equal(command.variants.length,0);
  const a=addVariant(command,dorm), b=addVariant(command,home);
  assert.equal(a.name,'Общага'); assert.equal(b.name,'Дом');
  assert.notEqual(a.id,b.id); assert.deepEqual(command.phrases,['Я спать','Пора ложиться']);
  assert.equal(command.actions.length,1);
  assert.throws(() => addVariant(command,home), /уже есть версия/);
});

test('copied location version and nested blocks can be edited without changing original or common steps', () => {
  const command = newScenario();
  command.actions.push({...newBlock('action','mac.brightness'),parameters:{level:10}});
  const a = addVariant(command,dorm);
  a.actions.push({...newBlock('parallel'),children:[{...newBlock('action','led.set'),parameters:{color:'#FF0020'}}]});
  const b = addVariant(command,home,a);
  b.actions[0].children[0].parameters.color = '#FFFFFF'; b.actions[0].enabled = false;
  assert.equal(a.actions[0].children[0].parameters.color,'#FF0020');
  assert.equal(a.actions[0].enabled,true); assert.equal(b.place_id,'home');
  assert.equal(command.actions[0].parameters.level,10);
});

test('revert restores a complete draft including deleted version, disabled step and optional device', () => {
  const saved=newScenario(); const a=addVariant(saved,dorm);
  a.actions.push({...newBlock('action','led.set'),enabled:false,required:false,parameters:{brightness:10}});
  const draft=clone(saved); draft.variants.splice(0,1); draft.phrases.push('Изменено');
  const reverted=clone(saved);
  assert.equal(reverted.variants.length,1); assert.equal(reverted.variants[0].actions[0].enabled,false);
  assert.equal(reverted.variants[0].actions[0].required,false); assert.equal(reverted.phrases.length,0);
});

test('reordering a specific version leaves other versions and shared sequence unchanged', () => {
  const command=newScenario(), a=addVariant(command,dorm), b=addVariant(command,home);
  a.actions.push({...newBlock('delay'),seconds:1},{...newBlock('delay'),seconds:2});
  b.actions.push({...newBlock('delay'),seconds:3});
  moveBlock(a.actions,0,1); assert.deepEqual(a.actions.map(v=>v.seconds),[2,1]);
  assert.equal(b.actions[0].seconds,3); assert.equal(command.actions.length,0);
});

test('search finds command by Russian phrase and user-defined version name', () => {
  const command=newScenario();command.name='Сон';command.phrases=['Пора ложиться'];
  const a=addVariant(command,home);a.name='Моя домашняя версия';
  assert.equal(searchCommands([command],'ЛОЖИТЬСЯ').length,1);
  assert.equal(searchCommands([command],'домашняя').length,1);
  assert.equal(searchCommands([command],'работать').length,0);
});

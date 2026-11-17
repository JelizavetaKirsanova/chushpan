import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {stripTypeScriptTypes} from 'node:module';

const source=stripTypeScriptTypes(readFileSync(new URL('../src/local_media.ts',import.meta.url),'utf8'),{mode:'transform'}).replace(/^export /gm,'')+'\nexports.api={classifyMedia};';
const sandbox={exports:{} as Record<string,any>};
vm.runInNewContext(source,sandbox);

test('local imports are classified from MIME or file extension',()=>{
  const {classifyMedia}=sandbox.exports.api;
  assert.equal(classifyMedia('track','audio/mpeg'),'music');
  assert.equal(classifyMedia('episode.mp4',''),'video');
  assert.equal(classifyMedia('cover','image/webp'),'image');
  assert.equal(classifyMedia('notes.md',''),'document');
  assert.equal(classifyMedia('installer.pkg','application/octet-stream'),null);
});

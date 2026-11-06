// Start scripts.check_personal_modules --workspace. Synthetic data only.
import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';

const {chromium} = await import(process.argv[2]);
const output = process.argv[4] ?? '/tmp/chushpan-final-browser';
await mkdir(output, {recursive: true});
const browser = await chromium.launch({
  headless: true, executablePath: process.argv[3], args: ['--mute-audio'],
});
try {
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [], requests = [], geometry = [], tabs = [], categories = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', d => d.accept());
  page.on('websocket', ws => ws.on('framesent', frame => {
    try { requests.push(JSON.parse(String(frame.payload))); } catch { /* protocol ping */ }
  }));
  const navigate = async view => {
    const sheet = page.locator('dialog.device-sheet[open]');
    if (await sheet.count()) await sheet.press('Escape');
    await page.locator(`.workspace-nav [data-view="${view}"]`).click();
    await page.waitForFunction(v => document.querySelector('.hud').dataset.view === v, view);
  };
  const measure = async label => {
    const size = await page.evaluate(() => ({
      viewport: innerWidth, document: document.documentElement.scrollWidth,
    }));
    geometry.push({label, ...size});
    assert.ok(size.document <= size.viewport, `${label}: horizontal overflow`);
  };
  await page.goto(process.argv[5] ?? 'http://127.0.0.1:8878');
  await navigate('home');
  await page.getByRole('img', {name: 'Персонаж Чушпан', exact: true}).waitFor();
  await page.screenshot({path: `${output}/home.png`, fullPage: true});
  // Add an unverified synthetic peripheral without scanning, connecting or testing it.
  await navigate('devices');
  await page.locator('.device-add').click();
  const wizard = page.getByRole('dialog', {name: 'Добавление устройства'});
  await wizard.getByRole('button', {name: 'LED-лента', exact: true}).click();
  await wizard.getByText('Добавить вручную', {exact: true}).click();
  await wizard.getByLabel('Идентификатор подключения', {exact: true})
    .fill('11111111-1111-4111-8111-111111111111');
  await wizard.getByRole('button', {name: 'Использовать идентификатор'}).click();
  await wizard.getByRole('button', {name: 'Добавить с непроверенным подключением'}).click();
  await wizard.getByLabel('Название', {exact: true}).fill('QA · лента без подключения');
  await wizard.getByRole('button', {name: 'Сохранить устройство', exact: true}).click();
  await page.getByRole('button', {name: 'Открыть QA · лента без подключения', exact: true}).click();
  const editor = page.getByLabel('RGB-редактор', {exact: true});
  await editor.getByLabel('HEX цвета', {exact: true}).fill('#B450FF');
  await editor.getByLabel('Название пользовательского цвета', {exact: true}).fill('QA Лавандовый');
  await editor.getByRole('button', {name: 'Сохранить новый цвет', exact: true}).click();
  await editor.getByRole('button', {name: 'QA Лавандовый', exact: true}).waitFor();
  assert.equal(await editor.getByRole('button', {name: 'Применить', exact: true}).isDisabled(), true);
  for (const width of [580, 900, 1440]) {
    await page.setViewportSize({width, height: 1000});
    await measure(`RGB/${width}`);
    await page.screenshot({path: `${output}/rgb-${width}.png`, fullPage: true});
  }
  await page.reload();
  await navigate('devices');
  await page.getByRole('button', {name: 'Открыть QA · лента без подключения', exact: true}).click();
  await editor.getByRole('button', {name: 'QA Лавандовый', exact: true}).waitFor();
  assert.equal(await editor.getByLabel('HEX цвета', {exact: true}).inputValue(), '#B450FF');

  const views = ['home', 'automations', 'devices', 'cinema', 'memory', 'mac-control',
    'my-devices', 'analytics', 'performance', 'settings'];
  for (const width of [580, 900, 1440, 1920, 2560]) {
    await page.setViewportSize({width, height: 1000});
    for (const view of views) {
      await navigate(view);
      await measure(`${view}/${width}`);
      if (width === 1440) await page.screenshot({path: `${output}/${view}.png`, fullPage: true});
    }
  }
  await page.setViewportSize({width: 1440, height: 1000});
  for (const view of ['memory', 'mac-control', 'my-devices', 'analytics', 'performance', 'cinema']) {
    await navigate(view);
    const names = await page.getByRole('tab').filter({visible: true}).allTextContents();
    for (const name of names) {
      const tab = page.getByRole('tab', {name, exact: true}).filter({visible: true});
      if (await tab.count() === 1) {
        await tab.click();
        await measure(`${view}/${name}`);
        tabs.push({view, name});
      }
    }
  }
  for (const width of [580, 1440]) {
    await page.setViewportSize({width, height: 1000});
    await navigate('settings');
    // Open settings categories only. No permission-request or native action buttons.
    const keys = await page.locator('.settings-category[data-category]')
      .evaluateAll(nodes => nodes.map(n => n.dataset.category));
    for (const key of keys) {
      await page.locator(`.settings-category[data-category="${key}"]`).click();
      await measure(`settings/${key}/${width}`);
      categories.push({width, key});
      await page.locator('#settings-back').click();
    }
  }
  const forbidden = requests.filter(r => r.type === 'devices' &&
    ['act', 'probe_led', 'confirm_probe', 'discover', 'diagnose_ble', 'inspect',
      'check_candidate', 'audio_refresh', 'group_act'].includes(r.value?.op));
  assert.deepEqual(forbidden, [], 'No physical operations may be requested');
  assert.deepEqual(errors, []);
  await writeFile(`${output}/result.json`, JSON.stringify({
    status: 'passed', geometry, tabs, categories, errors,
    flows: ['unverified RGB preview/save/reload', 'palette and preview persistence',
      '50 main-page layouts', 'settings categories', 'home cat'],
    native: 'isolated fixture', physicalRequests: forbidden,
  }, null, 2));
  process.stdout.write(`Passed ${geometry.length} layouts, ${tabs.length} tabs, ${categories.length} settings pages; no physical commands.\n`);
} finally {
  await browser.close();
}

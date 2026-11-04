// Real browser → local WebSocket → temporary SQLite. Native operations are mocked by the server.
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
const {chromium}=await import(process.argv[2]);
const output=process.argv[3];
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,args:['--mute-audio']});
try {
  const page=await browser.newPage({viewport:{width:1440,height:1000},deviceScaleFactor:1});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));page.on('dialog',dialog=>dialog.accept());
  await page.goto('http://127.0.0.1:8878');
  await page.locator('.workspace-nav [data-view=memory]').click();
  await page.getByRole('heading',{name:'Память',exact:true}).waitFor();
  await page.locator('#module-memory').getByRole('button',{name:'+ Воспоминание',exact:true}).click();
  await page.getByLabel('Что запомнить',{exact:true}).fill('Тест: вечером предпочитаю тёплый свет');
  await page.locator('dialog').getByRole('button',{name:'Сохранить',exact:true}).click();
  await page.locator('.memory-open').filter({hasText:'Тест: вечером'}).waitFor();
  await page.locator('#module-memory').getByRole('tab',{name:'Проекты',exact:true}).click();
  await page.getByRole('button',{name:'+ Создать проект',exact:true}).click();
  await page.getByLabel('Название',{exact:true}).fill('Тестовый проект');
  await page.getByLabel('Описание',{exact:true}).fill('Изолированная проверка интерфейса');
  await page.locator('dialog').getByRole('button',{name:'Создать',exact:true}).click();
  await page.getByRole('heading',{name:'Тестовый проект',exact:true}).waitFor();
  await page.locator('#module-memory').getByRole('tab',{name:'Обзор',exact:true}).click();
  await page.screenshot({path:output+'/memory-1440.png',fullPage:true});
  await page.locator('.workspace-nav [data-view=mac-control]').click();
  await page.getByRole('button',{name:'Обновить приложения и доступ',exact:true}).click();
  await page.locator('#module-mac-control').getByRole('heading',{name:'Test Editor',exact:true}).waitFor();
  await page.getByRole('button',{name:'Анализировать текст экрана',exact:true}).click();
  await page.locator('.screen-ocr').filter({hasText:'TEST FIXTURE'}).waitFor();
  assert.equal(await page.locator('.capture-indicator').isVisible(),false);
  await page.screenshot({path:output+'/mac-1440.png',fullPage:true});
  await page.locator('.workspace-nav [data-view=my-devices]').click();
  await page.getByRole('heading',{name:'Мои устройства',exact:true}).waitFor();
  assert.match(await page.locator('#module-my-devices').innerText(),/Сервер выключен/);
  await page.screenshot({path:output+'/devices-1440.png',fullPage:true});
  await page.locator('.workspace-nav [data-view=analytics]').click();
  await page.getByRole('tab',{name:'Сбор данных',exact:true}).click();
  await page.getByLabel('Собирать минимальную локальную статистику',{exact:true}).check();
  await page.getByRole('button',{name:'Сохранить настройки',exact:true}).click();
  await page.locator('#module-analytics .module-feedback').filter({hasText:'Данные обновлены'}).waitFor();
  await page.getByRole('tab',{name:'Обзор',exact:true}).click();
  await page.screenshot({path:output+'/analytics-1440.png',fullPage:true});
  await page.getByRole('tab',{name:'Сбор данных',exact:true}).click();
  await page.getByLabel('Собирать минимальную локальную статистику',{exact:true}).uncheck();
  await page.getByRole('button',{name:'Сохранить настройки',exact:true}).click();
  await page.getByRole('tab',{name:'Обзор',exact:true}).click();
  for(const width of [580,900,1920,2560]){
    await page.setViewportSize({width,height:1000});
    for(const view of ['memory','mac-control','my-devices','analytics']){
      await page.locator(`.workspace-nav [data-view="${view}"]`).click();
      const root=page.locator('#module-'+view);await root.waitFor({state:'visible'});
      const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
      assert.equal(overflow,false,view+' overflows at '+width);
      if(width===900)await page.screenshot({path:output+'/'+view+'-900.png',fullPage:true});
    }
  }
  await page.locator('.workspace-nav [data-view=memory]').click();
  await page.locator('#module-memory').getByRole('tab',{name:'Воспоминания',exact:true}).click();
  await page.locator('#module-memory').getByRole('button',{name:'Подробнее',exact:true}).first().click();
  await page.locator('dialog').getByRole('button',{name:'Удалить',exact:true}).click();
  await page.locator('#module-memory').getByRole('heading',{name:'Пока пусто',exact:true}).waitFor();
  assert.deepEqual(errors,[]);
  await writeFile(output+'/verification.json',JSON.stringify({status:'passed',widths:[580,900,1440,1920,2560],flows:['memory CRUD and projects','local OCR with native fixture','sync disabled status','analytics policy persistence'],native:'mocked',errors},null,2));
  process.stdout.write('Passed: browser → WebSocket → SQLite → UI; 5 widths; no browser errors.\n');
} finally {await browser.close();}

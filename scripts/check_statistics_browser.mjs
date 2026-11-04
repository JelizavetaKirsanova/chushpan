import assert from 'node:assert/strict';
const {chromium}=await import(process.argv[2]);
const browser=await chromium.launch({executablePath:process.argv[3],headless:true,args:['--mute-audio']});
try {
  const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
  page.setDefaultTimeout(8000);
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('console',message=>{if(message.type()==='warning'&&message.text().startsWith('Bad HUD event'))errors.push(message.text());});
  await page.goto(process.argv[4]);
  await page.locator('.workspace-nav [data-view=performance]').click();
  await page.locator('.statistics-periods').getByRole('button',{name:'Год',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.statistics-status')?.textContent.includes('сохранённые события'));
  assert.equal(await page.locator('.statistics-kpi').count(),4);
  const requests=Number(await page.locator('.statistics-kpi').first().locator('strong').textContent());
  assert.ok(Number.isFinite(requests));
  await page.screenshot({path:process.argv[5]});
  for(const label of ['Производительность','Разговоры','Команды','Сценарии','Приложения','Отчёты','Обзор']) {
    await page.locator('.statistics-tabs').getByRole('tab',{name:label,exact:true}).click();
    assert.equal(await page.locator('.statistics-tabs [aria-selected=true]').textContent(),label);
  }
  await page.locator('.statistics-tabs').getByRole('tab',{name:'Производительность',exact:true}).click();
  await page.locator('#performance-sort').selectOption('slow');
  if(await page.locator('#performance-requests .perf-request').count()) {
    await page.locator('#performance-requests .perf-request').first().click();
    await page.waitForFunction(()=>document.querySelector('#performance-detail h2')?.textContent.startsWith('Запрос'));
    assert.equal(await page.locator('#performance-trace-disclosure').getAttribute('open'),'');
  }
  await page.locator('.statistics-periods').getByRole('button',{name:'Произвольный период',exact:true}).click();
  await page.getByLabel('Начало периода',{exact:true}).fill('1901-01-01');
  await page.getByLabel('Конец периода',{exact:true}).fill('1901-01-01');
  await page.locator('.statistics-custom').getByRole('button',{name:'Применить',exact:true}).click();
  await page.locator('.statistics-tabs').getByRole('tab',{name:'Обзор',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.statistics-kpi strong')?.textContent==='0');
  assert.equal(await page.locator('.statistics-bars,.statistics-line-chart,.statistics-donut').count(),0);
  await page.screenshot({path:process.argv[5].replace('.png','-empty.png')});
  await page.locator('.workspace-nav [data-view=cinema]').click();
  await page.locator('.cinema-tabs').getByRole('button',{name:'Каталог',exact:true}).click();
  await page.locator('.cinema-catalogue-grid').waitFor();
  await page.waitForFunction(()=>document.querySelector('.cinema-catalogue-grid')?.getAttribute('aria-busy')==='false');
  assert.equal(await page.locator('#cinema-root > .cinema-composer').count(),0);
  const count=await page.locator('.cinema-catalogue-grid .cinema-card').count();
  if(count) {
    const favorite=page.locator('.cinema-catalogue-grid .cinema-quick-favorite').first();
    const before=await favorite.getAttribute('aria-pressed');
    await favorite.click();
    await page.waitForFunction(before=>document.querySelector('.cinema-catalogue-grid .cinema-quick-favorite')?.getAttribute('aria-pressed')!==before,before);
    const first=page.locator('.cinema-catalogue-grid .cinema-card').first();
    const rect=await first.boundingBox();await first.hover();const hover=await first.boundingBox();
    assert.equal(rect.width,hover.width);assert.equal(rect.height,hover.height);
  }
  await page.locator('.cinema-ai-toggle').click();
  await page.getByLabel('Команда Чушпану о кино',{exact:true}).fill('Найди детектив на вечер');
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('.cinema-ai-panel').isVisible(),false);
  assert.equal(await page.locator('.hud').getAttribute('data-view'),'cinema');
  await page.locator('.cinema-ai-toggle').click();
  await page.getByLabel('Команда Чушпану о кино',{exact:true}).fill('Найди детектив на вечер');
  await page.getByLabel('Отправить команду о кино',{exact:true}).click();
  assert.equal(await page.locator('.cinema-ai-panel').isVisible(),false);
  await page.getByLabel('Фильтры каталога',{exact:true}).click();
  await page.getByRole('dialog',{name:'Фильтры каталога',exact:true}).waitFor();
  assert.equal(await page.getByLabel('Сортировка каталога',{exact:true}).count(),1);
  await page.getByLabel('Закрыть фильтры',{exact:true}).click();
  const search=page.getByLabel('Поиск по кинотеке',{exact:true});
  await search.fill('Несуществующий фильм 12345');
  await page.waitForTimeout(450);
  await page.waitForFunction(()=>document.querySelector('.cinema-catalogue-grid')?.getAttribute('aria-busy')==='false');
  assert.equal(await page.locator('.cinema-catalogue-grid .cinema-card').count(),0);
  await search.fill('');await page.waitForTimeout(450);
  await page.waitForFunction(()=>document.querySelector('.cinema-catalogue-grid')?.getAttribute('aria-busy')==='false');
  const scroller=page.locator('.cinema-page');
  for(let i=0;i<6;i++)await scroller.evaluate(n=>{n.scrollTop+=n.clientHeight;});
  await scroller.evaluate(n=>{n.scrollTop=n.scrollHeight;});
  assert.equal(await page.locator('.cinema-tabs').evaluate(n=>getComputedStyle(n).position),'static');
  await page.screenshot({path:process.argv[5].replace('.png','-cinema-bottom.png')});
  await scroller.evaluate(n=>{n.scrollTop=0;});
  await page.screenshot({path:process.argv[5].replace('.png','-cinema.png')});
  for(const width of [900,600,390]) {
    await page.setViewportSize({width,height:900});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await scroller.evaluate(n=>n.scrollWidth>n.clientWidth+1),false);
    await page.locator('.workspace-nav [data-view=performance]').click();
    assert.equal(await page.locator('.performance-page').evaluate(n=>n.scrollWidth>n.clientWidth+1),false);
    await page.locator('.workspace-nav [data-view=cinema]').click();
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({browser:true,real_saved_requests:requests,catalogue_cards:count,all_tabs:true,custom_empty:true,filters:true,search:true,favorite:count>0,escape:true,no_overlays:true,widths:[1440,900,600,390],errors}));
} finally {await browser.close();}

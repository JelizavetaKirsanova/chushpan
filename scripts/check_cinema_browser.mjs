// Test-only transport: production PLAYER code runs in a real, muted headless browser.
import {createInterface} from 'node:readline';
import {readFile} from 'node:fs/promises';
const {chromium}=await import(process.argv[2]);
const browser=await chromium.launch({executablePath:process.argv[3]||undefined,headless:true,args:['--mute-audio','--autoplay-policy=no-user-gesture-required']});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
context.setDefaultTimeout(7000);
let page=await context.newPage();
const player=await readFile(new URL('../backend/cinema/player.js',import.meta.url),'utf8');
for await(const line of createInterface({input:process.stdin})) {
  try {
    const request=JSON.parse(line);let result={};
    if(request.op==='open') {if(page.isClosed())page=await context.newPage();await page.goto(request.url);await page.locator('video').evaluate(video=>new Promise(resolve=>{if(video.readyState>=1)resolve();else video.addEventListener('loadedmetadata',resolve,{once:true});}));}
    if(request.op==='eval') {result=JSON.parse(await page.evaluate(({player,payload})=>(0,eval)(player+'('+JSON.stringify(payload)+')'),{player,payload:{...request.payload,url:request.url}}));}
    if(request.op==='close_page')await page.close();
    if(request.op==='change_media'){
      await page.locator('video').evaluate((video,url)=>new Promise(resolve=>{video.addEventListener('loadedmetadata',resolve,{once:true});video.src=url;video.load();}),request.media);
    }
    if(request.op==='ui') {
      const hud=await context.newPage();const errors=[];hud.on('pageerror',error=>errors.push(error.message));
      await hud.goto(request.url);await hud.locator('.workspace-nav [data-view=cinema]').click();
      await hud.getByRole('heading',{name:'Медиатека',exact:true}).waitFor();
      await hud.locator('.cinema-content .cinema-title').first().waitFor();
      if(request.screenshot)await hud.screenshot({path:request.screenshot,fullPage:true});
      await hud.locator('.cinema-content .cinema-title').first().click();
      await hud.locator('dialog[open]').waitFor();
      await hud.getByLabel('Сезон',{exact:true}).selectOption('1');
      await hud.getByRole('button',{name:'Закрыть',exact:true}).click();
      await hud.locator('.cinema-tabs').getByRole('button',{name:'Просмотрено',exact:true}).click();
      await hud.locator('summary').filter({hasText:'Журнал просмотра'}).click();
      await hud.locator('.cinema-history article').first().waitFor();
      await hud.locator('#cinema-root').getByRole('button',{name:'Настройки',exact:true}).click();
      await hud.getByRole('button',{name:'Источники просмотра',exact:true}).click();await hud.getByRole('heading',{name:'Локальный HTML5',exact:true}).waitFor();
      const diagnostics=hud.locator('.cinema-source-diagnostics');
      await diagnostics.getByRole('button',{name:'Проверить источники',exact:true}).waitFor();
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-diagnostics.png'),fullPage:true});
      await hud.setViewportSize({width:720,height:900});
      const overflow=await hud.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
      if(overflow)throw Error('Cinema UI overflows the viewport');
      if(errors.length)throw Error(errors.join('; '));
      await hud.close();result={ui:true,mobile:true,errors};
    }
    if(request.op==='catalogue_ui') {
      const hud=await context.newPage();const errors=[];hud.on('pageerror',error=>errors.push(error.message));
      await hud.route('https://image.tmdb.org/**',route=>{const id=Number(route.request().url().match(/fixture(\d+)/)?.[1]??1);const colors=['#513450','#293d59','#61432b','#364b43'];const color=colors[id%colors.length];return route.fulfill({contentType:'image/svg+xml',body:`<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600"><defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="${color}"/><stop offset="1" stop-color="#15101e"/></linearGradient></defs><rect width="400" height="600" fill="url(#g)"/><circle cx="210" cy="240" r="135" fill="#f0d7b5" opacity=".09"/><path d="M0 500 175 240 400 580V600H0" fill="#080b18" opacity=".6"/><text x="35" y="510" font-family="Arial" font-size="32" fill="#e8deef">STORY ${id}</text></svg>`});});
      await hud.addInitScript(()=>{window.__catalogueRequests=[];const send=WebSocket.prototype.send;WebSocket.prototype.send=function(value){try{const v=JSON.parse(value);if(v.type==='cinema'&&v.value?.op==='catalogue_page')window.__catalogueRequests.push(v.value);}catch{}return send.call(this,value);};});
      await hud.goto(request.url);await hud.locator('.workspace-nav [data-view=cinema]').click();await hud.getByRole('button',{name:'Каталог',exact:true}).click();await hud.getByRole('button',{name:'Подборки',exact:true}).click();
      await hud.locator('.cinema-shelf[data-feed=trending] .cinema-card').first().waitFor();
      if(await hud.locator('.cinema-shelf').count()!==13)throw Error('Animation shelf must follow the default catalogue policy');
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-catalogue.png'),fullPage:true});
      await hud.locator('.cinema-shelf[data-feed=popular_movies]').getByRole('button',{name:'Смотреть всё'}).click();
      await hud.waitForFunction(()=>document.querySelectorAll('.cinema-catalogue-grid .cinema-card').length===20);
      await hud.locator('.cinema-sentinel').scrollIntoViewIfNeeded();
      await hud.waitForFunction(()=>document.querySelectorAll('.cinema-catalogue-grid .cinema-card').length===40);
      await hud.locator('.cinema-sentinel').scrollIntoViewIfNeeded();
      await hud.locator('.cinema-catalogue-footer').getByText('TMDB временно недоступен.',{exact:false}).waitFor();
      if(await hud.locator('.cinema-catalogue-grid .cinema-card').count()!==40)throw Error('Error discarded loaded cards');
      await hud.getByRole('button',{name:'Повторить загрузку'}).click();
      await hud.waitForFunction(()=>document.querySelectorAll('.cinema-catalogue-grid .cinema-card').length>=60);
      // Stress the former six-page boundary. Existing DOM nodes and prefixes must survive.
      const prefix=await hud.locator('.cinema-catalogue-grid .cinema-card').evaluateAll(cards=>{
        window.__firstCatalogueCard=cards[0];return cards.map(card=>card.dataset.catalogueKey);
      });
      for(let count=80;count<=160;count+=20){
        await hud.locator('.cinema-sentinel').scrollIntoViewIfNeeded();
        await hud.waitForFunction(expected=>document.querySelectorAll('.cinema-catalogue-grid .cinema-card').length===expected,count);
        const stable=await hud.evaluate(prefix=>document.querySelector('.cinema-catalogue-grid .cinema-card')===window.__firstCatalogueCard&&prefix.every((key,i)=>document.querySelectorAll('.cinema-catalogue-grid .cinema-card')[i]?.dataset.catalogueKey===key),prefix);
        if(!stable)throw Error('Pagination changed an earlier card or its DOM node');
      }
      // A detail dialog and personal-library snapshots must leave the catalogue in place.
      const target=hud.locator('.cinema-catalogue-grid .cinema-card').nth(75);
      await target.scrollIntoViewIfNeeded();
      const before=await target.evaluate(card=>({top:card.getBoundingClientRect().top,scroll:card.closest('.workspace-page').scrollTop}));
      await target.locator('.cinema-title').click();await hud.locator('dialog[open]').waitFor();
      await hud.getByRole('tab',{name:'О фильме',exact:true}).click();
      await hud.locator('.cinema-rich-info').waitFor();
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-details.png'),fullPage:true});
      await hud.getByRole('tab',{name:'Похожие',exact:true}).click();await hud.locator('.cinema-similar-grid .cinema-card').first().waitFor();

      await hud.getByRole('button',{name:'Закрыть',exact:true}).click();
      const after=await target.evaluate(card=>({top:card.getBoundingClientRect().top,scroll:card.closest('.workspace-page').scrollTop}));
      if(Math.abs(before.top-after.top)>2||Math.abs(before.scroll-after.scroll)>2)throw Error('Detail changed catalogue scroll position');
      const beforeLibrary=await target.locator('.cinema-title').textContent();
      await target.getByRole('button',{name:'В избранное',exact:true}).first().click();
      await target.locator('.cinema-favorite[aria-pressed=true]').waitFor();
      // Component navigation should preserve scroll. Avoid Playwright first scrolling
      // the offscreen navigation into view, which changes the state being measured.
      await hud.locator('.cinema-tabs').getByRole('button',{name:'Избранное',exact:true}).dispatchEvent('click');
      await hud.locator('.cinema-content .cinema-title').filter({hasText:beforeLibrary}).click();
      await hud.getByRole('button',{name:'Оценить фильм',exact:true}).click();
      const rating=hud.getByLabel('Выбранная оценка',{exact:true});await rating.focus();await rating.press('End');await rating.press('ArrowLeft');
      await hud.getByLabel('Заметка · необязательно',{exact:true}).fill('Сильный финал');
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-rating.png'),fullPage:true});
      await hud.getByRole('button',{name:'Сохранить оценку',exact:true}).click();
      await hud.locator('dialog .cinema-personal-rating').filter({hasText:'9.5'}).waitFor();
      await hud.locator('.cinema-detail-main-actions').getByRole('button',{name:'Просмотрено',exact:true}).click();
      await hud.getByRole('button',{name:'Закрыть',exact:true}).click();
      await hud.locator('.cinema-tabs').getByRole('button',{name:'Просмотрено',exact:true}).dispatchEvent('click');
      await hud.locator('.cinema-content .cinema-personal-rating').filter({hasText:'9.5'}).waitFor();
      await hud.locator('.cinema-tabs').getByRole('button',{name:'Каталог',exact:true}).dispatchEvent('click');
      const restored=await target.evaluate(card=>({top:card.getBoundingClientRect().top,scroll:card.closest('.workspace-page').scrollTop}));
      if(Math.abs(before.top-restored.top)>2||Math.abs(before.scroll-restored.scroll)>2)throw Error(`Returning from favorites changed catalogue scroll: ${JSON.stringify({before,restored})}`);
      const keys=await hud.locator('.cinema-catalogue-grid .cinema-card').evaluateAll(cards=>cards.map(card=>card.dataset.catalogueKey));if(new Set(keys).size!==keys.length)throw Error('Duplicate catalogue cards');
      const search=hud.locator('.cinema-search input');await search.fill('Interstellar');
      await hud.getByRole('heading',{name:'Поиск: Interstellar',exact:true}).waitFor();
      await hud.locator('.cinema-catalogue-grid .cinema-card').first().waitFor();
      await hud.getByRole('button',{name:'Фильмы',exact:true}).click();
      await hud.getByRole('button',{name:/^Фильтры ·/}).click();
      await hud.getByLabel('триллер',{exact:true}).check();
      await hud.getByLabel('фантастика',{exact:true}).check();
      await hud.getByLabel('Режим жанров').selectOption('and');
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-filters.png'),fullPage:true});
      await hud.getByRole('button',{name:/^Применить/}).click();
      await hud.locator('.cinema-catalogue-grid .cinema-card').first().waitFor();
      await hud.setViewportSize({width:720,height:900});
      if(await hud.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Catalogue overflows mobile viewport');
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-catalogue-mobile.png'),fullPage:true});
      if(errors.length)throw Error(errors.join('; '));await hud.close();result={catalogue:true,pages:8,stable_order:true,scroll_restored:true,favorites:true,half_rating:true,multiple_genres:true,infinite_scroll:true,retry:true,mobile:true};
    }
    if(request.op==='personal_ui') {
      const hud=await context.newPage();const errors=[];hud.on('pageerror',error=>errors.push(error.message));
      const media=await readFile(request.video);
      await hud.route('https://cinema-fixture.example/**',route=>{
        const range=/bytes=(\d+)-(\d*)/.exec(route.request().headers().range??'');
        if(!range)return route.fulfill({body:media,contentType:'video/mp4',headers:{'Accept-Ranges':'bytes'}});
        const start=Number(range[1]);const end=Math.min(media.length-1,range[2]?Number(range[2]):media.length-1);
        return route.fulfill({status:206,body:media.subarray(start,end+1),contentType:'video/mp4',headers:{'Accept-Ranges':'bytes','Content-Range':`bytes ${start}-${end}/${media.length}`}});
      });
      await hud.route('https://image.tmdb.org/**',route=>route.fulfill({status:404}));
      await hud.goto(request.url);await hud.locator('.workspace-nav [data-view=cinema]').click();
      await hud.getByRole('button',{name:'Мой кинопрофиль',exact:true}).click();
      await hud.locator('.cinema-profile-stats').waitFor();await hud.getByText('Тестовый режиссёр',{exact:true}).waitFor();
      await hud.locator('.cinema-chart-bar').first().waitFor();
      await hud.locator('.cinema-personal').evaluate(element=>Promise.all(element.getAnimations().map(animation=>animation.finished)));
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-profile.png'),fullPage:true});
      if(request.catalogue){await hud.getByRole('button',{name:'Для тебя',exact:true}).click();await hud.locator('.cinema-personal-card').first().waitFor();if(await hud.locator('.cinema-personal .cinema-shelf').count()!==7)throw Error('Expected six personal shelves and a separate rewatch shelf');}
      await hud.getByRole('button',{name:'Смотрю',exact:true}).click();
      await hud.locator('.cinema-content').getByRole('button',{name:'Встроенный тест',exact:true}).click();
      await hud.locator('dialog').getByRole('button',{name:'Найти и смотреть',exact:true}).click();
      const video=hud.locator('.cinema-embedded video');await video.waitFor();
      await hud.waitForFunction(()=>{const v=document.querySelector('.cinema-embedded video');return v&&!v.paused&&v.currentTime>3.2;}).catch(async error=>{throw Error('Embedded start failed: '+JSON.stringify(await video.evaluate(v=>({paused:v.paused,time:v.currentTime,ready:v.readyState,error:v.error?.message,feedback:v.parentElement.textContent}))))});
      await hud.getByText('Воспроизведение подтверждено.',{exact:true}).waitFor();
      await video.evaluate(v=>{window.__embeddedVideo=v;v.pause();v.currentTime=5;});
      await hud.waitForFunction(()=>document.querySelector('.cinema-embedded video')===window.__embeddedVideo&&Math.abs(window.__embeddedVideo.currentTime-5)<.2).catch(async error=>{throw Error('Embedded seek failed: '+JSON.stringify(await video.evaluate(v=>({paused:v.paused,time:v.currentTime,ready:v.readyState,error:v.error?.message}))))});
      const viewer=hud.locator('.cinema-viewer');
      await viewer.getByRole('button',{name:'Продолжить',exact:true}).click();
      await hud.waitForFunction(()=>{const v=document.querySelector('.cinema-embedded video');return v&&!v.paused&&v.currentTime>5.2;});
      await viewer.getByRole('button',{name:'Пауза',exact:true}).click();
      await hud.waitForFunction(()=>document.querySelector('.cinema-embedded video')?.paused===true);
      await viewer.getByText('На паузе',{exact:true}).waitFor();
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-viewer.png'),fullPage:true});
      await hud.setViewportSize({width:390,height:844});
      if(await hud.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Personal cinema UI overflows mobile viewport');
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-embedded.png'),fullPage:true});
      await viewer.getByRole('button',{name:'Сохранить и закрыть',exact:true}).click();
      await hud.locator('.cinema-embedded').waitFor({state:'detached'});

      await hud.locator('#cinema-root').getByRole('button',{name:'Настройки',exact:true}).click();
      await hud.getByRole('button',{name:'Источники просмотра',exact:true}).click();
      if(await hud.locator('.cinema-settings-pane input[type=password]').count())throw Error('TMDB credentials remain in viewing sources');
      const ids=await hud.locator('.cinema-source-card').evaluateAll(rows=>rows.map(r=>r.dataset.sourceId));
      if(ids.length>1){await hud.locator('.cinema-source-card').nth(1).dragTo(hud.locator('.cinema-source-card').nth(0));await hud.waitForFunction(id=>document.querySelector('.cinema-source-card')?.dataset.sourceId===id,ids[1]);}
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-sources.png'),fullPage:true});
      await hud.getByRole('button',{name:'Поиск и Safari',exact:true}).click();
      if(await hud.getByLabel('Провайдер интернет-поиска',{exact:true}).inputValue()!=='tavily')throw Error('Tavily is not the default provider');
      if(await hud.getByLabel('Ключ Tavily API',{exact:true}).getAttribute('type')!=='password')throw Error('Tavily key input is not masked');
      await hud.getByRole('button',{name:'Проверить подключение',exact:true}).waitFor();
      if(request.screenshot){await hud.setViewportSize({width:1200,height:1000});await hud.getByRole('heading',{name:'Tavily Search API',exact:true}).scrollIntoViewIfNeeded();await hud.screenshot({path:request.screenshot.replace('.png','-search.png'),fullPage:true});}
      await hud.getByRole('button',{name:'Рекомендации',exact:true}).click();await hud.getByLabel('Учитывать мои оценки',{exact:true}).waitFor();
      await hud.getByRole('button',{name:'Приватность и данные',exact:true}).click();
      const downloading=hud.waitForEvent('download');await hud.getByRole('button',{name:'Экспортировать медиатеку',exact:true}).click();const downloaded=await downloading;
      const document=JSON.parse(await readFile(await downloaded.path(),'utf8'));
      if(document.schema!=='chushpan.cinema'||JSON.stringify(document).includes('fixture-token-for-catalogue'))throw Error('Invalid or secret-bearing cinema backup');
      await hud.getByLabel('Импортировать JSON-копию',{exact:true}).setInputFiles({name:'cinema.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(document))});
      await hud.getByRole('button',{name:'Импортировать',exact:true}).click();await hud.getByText('Импортировано названий: 0. Существующие данные сохранены.',{exact:true}).waitFor();
      if(errors.length)throw Error(errors.join('; '));await hud.close();result={profile:true,recommendations:!!request.catalogue,source_search:true,embedded:true,confirmed_playback:true,seek:true,mobile:true};
    }
    if(request.op==='compact_ui'){
      const hud=await context.newPage();const errors=[];hud.on('pageerror',e=>errors.push(e.message));
      const media=await readFile(request.video);await hud.route('https://cinema-fixture.example/**',route=>{
        const range=/bytes=(\d+)-(\d*)/.exec(route.request().headers().range??'');
        if(!range)return route.fulfill({body:media,contentType:'video/mp4',headers:{'Accept-Ranges':'bytes'}});
        const start=Number(range[1]);const end=Math.min(media.length-1,range[2]?Number(range[2]):media.length-1);
        return route.fulfill({status:206,body:media.subarray(start,end+1),contentType:'video/mp4',headers:{'Accept-Ranges':'bytes','Content-Range':`bytes ${start}-${end}/${media.length}`}});
      });
      await hud.goto(request.url);await hud.locator('.workspace-nav [data-view=cinema]').click();
      await hud.getByRole('button',{name:'Смотрю',exact:true}).click();
      await hud.locator('.cinema-content').getByRole('button',{name:'Встроенный тест',exact:true}).click();
      await hud.locator('dialog').getByRole('button',{name:'Найти и смотреть',exact:true}).click();
      const manual=hud.locator('dialog[open]').getByRole('button',{name:/^(Выбрать вручную|Продолжить в Safari)$/});await manual.waitFor();
      if(await hud.locator('.cinema-embedded video').count())throw Error('Unknown cost was opened automatically under free-only');
      const dialog=hud.locator('dialog[open]');if((await dialog.boundingBox()).width>620)throw Error('Source search is too wide');
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-compact.png'),fullPage:true});
      await hud.setViewportSize({width:390,height:844});if(await hud.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Compact search overflows mobile viewport');
      await manual.click();
      await hud.locator('.cinema-embedded video').waitFor();
      await hud.getByText('Воспроизведение подтверждено.',{exact:true}).waitFor();
      await hud.getByRole('button',{name:'Сохранить и закрыть плеер',exact:true}).click();
      if(errors.length)throw Error(errors.join('; '));await hud.close();result={compact:true,unknown_manual_choice:true,free_filter:true,confirmed_playback:true};
    }
    if(request.op==='shutdown'){await browser.close();process.stdout.write(JSON.stringify({ok:true})+'\n');process.stdin.destroy();break;}
    process.stdout.write(JSON.stringify({ok:true,result})+'\n');
  }catch(error){process.stdout.write(JSON.stringify({ok:false,error:String(error)})+'\n');}
}
await browser.close();

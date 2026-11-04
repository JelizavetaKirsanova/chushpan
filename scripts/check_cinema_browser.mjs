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
    if(request.op==='ui') {
      const hud=await context.newPage();const errors=[];hud.on('pageerror',error=>errors.push(error.message));
      await hud.goto(request.url);await hud.locator('.workspace-nav [data-view=cinema]').click();
      await hud.getByRole('heading',{name:'Фильмы и сериалы'}).waitFor();
      await hud.locator('.cinema-content .cinema-title').first().waitFor();
      if(request.screenshot)await hud.screenshot({path:request.screenshot,fullPage:true});
      await hud.locator('.cinema-content .cinema-title').first().click();
      await hud.locator('dialog[open]').waitFor();
      await hud.locator('dialog[open] select').first().selectOption('1');
      await hud.getByRole('button',{name:'Закрыть',exact:true}).click();
      await hud.getByRole('button',{name:'Просмотрено',exact:true}).click();
      await hud.locator('summary').filter({hasText:'Журнал просмотра'}).click();
      await hud.locator('.cinema-history article').first().waitFor();
      await hud.locator('#cinema-root').getByRole('button',{name:'Настройки',exact:true}).click();
      await hud.getByRole('heading',{name:'Локальный HTML5',exact:true}).waitFor();
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
      await hud.goto(request.url);await hud.locator('.workspace-nav [data-view=cinema]').click();await hud.getByRole('button',{name:'Каталог',exact:true}).click();
      await hud.locator('.cinema-shelf[data-feed=trending] .cinema-card').first().waitFor();
      if(await hud.locator('.cinema-shelf').count()!==14)throw Error('Expected fourteen catalogue shelves');
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
      await hud.getByRole('button',{name:'Закрыть',exact:true}).click();
      const after=await target.evaluate(card=>({top:card.getBoundingClientRect().top,scroll:card.closest('.workspace-page').scrollTop}));
      if(Math.abs(before.top-after.top)>2||Math.abs(before.scroll-after.scroll)>2)throw Error('Detail changed catalogue scroll position');
      const beforeLibrary=await target.locator('.cinema-title').textContent();
      await target.getByRole('button',{name:'В избранное',exact:true}).first().click();
      await target.locator('.cinema-favorite[aria-pressed=true]').waitFor();
      await hud.locator('.cinema-tabs').getByRole('button',{name:'Избранное',exact:true}).click();
      await hud.locator('.cinema-content .cinema-title').filter({hasText:beforeLibrary}).click();
      await hud.getByLabel('Моя оценка',{exact:true}).selectOption('9.5');
      await hud.locator('dialog .cinema-personal-rating').filter({hasText:'9.5'}).waitFor();
      await hud.getByLabel('Статус',{exact:true}).selectOption('watched');
      await hud.getByRole('button',{name:'Закрыть',exact:true}).click();
      await hud.locator('.cinema-tabs').getByRole('button',{name:'Просмотрено',exact:true}).click();
      await hud.locator('.cinema-content .cinema-personal-rating').filter({hasText:'9.5'}).waitFor();
      await hud.locator('.cinema-tabs').getByRole('button',{name:'Каталог',exact:true}).click();
      const restored=await target.evaluate(card=>({top:card.getBoundingClientRect().top,scroll:card.closest('.workspace-page').scrollTop}));
      if(Math.abs(before.top-restored.top)>2||Math.abs(before.scroll-restored.scroll)>2)throw Error(`Returning from favorites changed catalogue scroll: ${JSON.stringify({before,restored})}`);
      const keys=await hud.locator('.cinema-catalogue-grid .cinema-card').evaluateAll(cards=>cards.map(card=>card.dataset.catalogueKey));if(new Set(keys).size!==keys.length)throw Error('Duplicate catalogue cards');
      const search=hud.locator('.cinema-search input');await search.fill('Interstellar');
      await hud.getByRole('heading',{name:'Поиск: Interstellar',exact:true}).waitFor();
      await hud.locator('.cinema-catalogue-grid .cinema-card').first().waitFor();
      await hud.getByRole('button',{name:'Фильмы',exact:true}).click();
      await hud.locator('.cinema-genre-panel > summary').click();
      await hud.getByLabel('триллер',{exact:true}).check();
      await hud.getByLabel('фантастика',{exact:true}).check();
      await hud.getByLabel('Режим жанров').selectOption('and');
      await hud.getByRole('button',{name:'Найти',exact:true}).click();
      await hud.locator('.cinema-catalogue-grid .cinema-card').first().waitFor();
      await hud.setViewportSize({width:720,height:900});
      if(await hud.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Catalogue overflows mobile viewport');
      if(request.screenshot)await hud.screenshot({path:request.screenshot.replace('.png','-catalogue-mobile.png'),fullPage:true});
      if(errors.length)throw Error(errors.join('; '));await hud.close();result={catalogue:true,pages:8,stable_order:true,scroll_restored:true,favorites:true,half_rating:true,multiple_genres:true,infinite_scroll:true,retry:true,mobile:true};
    }
    if(request.op==='shutdown'){await browser.close();process.stdout.write(JSON.stringify({ok:true})+'\n');process.stdin.destroy();break;}
    process.stdout.write(JSON.stringify({ok:true,result})+'\n');
  }catch(error){process.stdout.write(JSON.stringify({ok:false,error:String(error)})+'\n');}
}
await browser.close();

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
      await hud.getByRole('heading',{name:'Кино и сериалы'}).waitFor();
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
    if(request.op==='shutdown'){await browser.close();process.stdout.write(JSON.stringify({ok:true})+'\n');process.stdin.destroy();break;}
    process.stdout.write(JSON.stringify({ok:true,result})+'\n');
  }catch(error){process.stdout.write(JSON.stringify({ok:false,error:String(error)})+'\n');}
}
await browser.close();

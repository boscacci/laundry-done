const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM} : {})});
  const page=await browser.newPage();const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const base=process.env.SMOKE_URL || 'http://127.0.0.1:8090';
  const secret=process.env.SMOKE_ADMIN_SECRET;
  const headers=secret ? {'x-laundry-admin-secret':secret} : {};
  await page.setExtraHTTPHeaders(headers);
  try {
    const response=await page.goto(base+'/monitor',{waitUntil:'networkidle'});
    assert.equal(response.status(),200);
    await page.waitForFunction(()=>document.querySelector('#load-state').textContent!=='Loading monitoring status…');
    assert(!/unavailable|HTTP/.test(await page.locator('#load-state').innerText()));
    await page.locator('[data-action="start"]').click();
    await page.getByText('start: accepted',{exact:true}).waitFor();
    await page.locator('[data-action="pause"]').click();
    await page.getByText('pause: accepted',{exact:true}).waitFor();
    assert((await page.locator('#voice-status').innerText()).includes('Calls paused'));
    await page.locator('[data-action="resume"]').click();
    await page.getByText('resume: accepted',{exact:true}).waitFor();
    page.once('dialog',dialog=>dialog.accept());
    await page.locator('[data-action="test"]').click();
    await page.getByText('test: accepted',{exact:true}).waitFor();
    await page.waitForFunction(()=>document.querySelector('#voice-status').textContent.includes('Latest alert: simulated'),null,{timeout:15000});
    await page.locator('#clip-preview').selectOption('laundry-01');
    await page.waitForFunction(()=>document.querySelector('#clip-player').readyState>=2);
    await page.locator('#pause').click();assert.equal(await page.locator('#pause').innerText(),'Resume');
    await page.locator('#pause').click();await page.locator('#zoom').selectOption('900000');
    await page.locator('#scale-mode').selectOption('detail');
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.locator('[data-action="cancel"]').click();
    await page.getByText('cancel: accepted',{exact:true}).waitFor();
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({smoke:'passed',url:base,checks:['controls','simulated delivery','audio preview','chart','mobile','no JS errors']}));
  } catch(e) {
    await page.screenshot({path:process.env.SMOKE_SCREENSHOT || 'outputs/dev-smoke-failure.png',fullPage:true});throw e;
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

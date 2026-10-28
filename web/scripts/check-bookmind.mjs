import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const out = new URL('../../data/native-verification/', import.meta.url);
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,args:['--no-proxy-server']});
const context = await browser.newContext({viewport:{width:1600,height:1000}});
const page = await context.newPage();
const errors = [], failedRequests = [];
page.on('pageerror', error => errors.push(error.message));
page.on('response', response => {if(response.status()>=400) failedRequests.push({url:response.url(),status:response.status()});});
const base='http://127.0.0.1:8018';
const books=await (await context.request.get(base+'/api/bookmind/books')).json();
assert(Array.isArray(books)&&books.length>=1);
const small=books.find(book=>book.title.includes('二叉树')) || books[0];
const big=books.find(book=>book.objectives.length>100);
const checks=[];
try {
  await page.goto(base+'/');
  await page.getByRole('heading',{name:'一本教材，一条学习主线。'}).waitFor();
  assert((await page.title()).includes('BookMind'));
  await page.screenshot({path:fileURLToPath(new URL('01-bookshelf.png',out)),fullPage:true});
  checks.push('bookshelf');
  for(const mode of ['learn','guide','practice','notes']) {
    await page.goto(base+small.links[mode]);
    await page.locator('.bm-context-header nav a').last().waitFor();
    assert.equal(await page.locator('.bm-context-header nav a').count(),3);
    if(mode === 'learn') await page.locator('[data-reader-unit]').first().waitFor();
    else if(mode === 'guide') await page.locator('#mastery-outline-start').waitFor();
    else if(mode === 'practice') await page.getByRole('heading',{name:'用一道题，检验一份理解'}).waitFor();
    else await page.getByRole('button',{name:/笔记|record|编辑/i}).first().waitFor().catch(()=>{});
    await page.screenshot({path:fileURLToPath(new URL(`02-${mode}.png`,out)),fullPage:false});
    checks.push(mode);
  }
  await page.goto(base+'/api-settings');
  await page.getByLabel('API Key',{exact:true}).waitFor();
  assert.equal(await page.getByLabel('API Key',{exact:true}).inputValue(),'');
  await page.screenshot({path:fileURLToPath(new URL('03-settings.png',out))});
  checks.push('redacted-settings');
  if(big) {
    await page.goto(base+big.links.learn+'?locator=133');
    await page.locator('[data-reading-workspace]').waitFor();
    await page.waitForTimeout(4500);
    await page.screenshot({path:fileURLToPath(new URL('04-textbook.png',out))});
    const count=await page.locator('canvas').count();
    checks.push({large_pdf_canvases:count});
    await page.goto(base+big.links.guide);
    await page.getByRole('button',{name:'查看教材原文',exact:true}).click();
    await page.locator('.bm-source-text').waitFor();
    const raw=page.waitForResponse(response=>response.url().includes(`/materials/${big.material_id}/raw`),{timeout:30000});
    await page.getByRole('button',{name:'查看 PDF 原页',exact:true}).click();
    const pdfResponse=await raw;
    assert.equal(pdfResponse.headers()['content-type'],'application/pdf');
    await page.locator('.bm-source-body iframe').waitFor();
    await page.waitForTimeout(1500);
    await page.screenshot({path:fileURLToPath(new URL('04-pdf-source.png',out))});
    checks.push('pdf-source-preview');
  }
  await page.setViewportSize({width:390,height:844});
  await page.goto(base+'/');
  await page.getByRole('heading',{name:'一本教材，一条学习主线。'}).waitFor();
  await page.screenshot({path:fileURLToPath(new URL('05-mobile.png',out)),fullPage:true});
  checks.push('mobile-bookshelf');
} finally {
  writeFileSync(new URL('report.json',out),JSON.stringify({checks,errors,failedRequests},null,2));
  await browser.close();
}
console.log(JSON.stringify({checks,errors,failedRequests}));
assert.equal(errors.length,0);

import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const resumeSession = process.argv.find(value => value.startsWith('--resume-session='))?.split('=')[1];
const live = process.argv.includes('--live') || Boolean(resumeSession);
const base = 'http://127.0.0.1:8018';
const out = new URL('../../data/diagram-verification/', import.meta.url);
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true, args: ['--no-proxy-server'] });
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const checks = [], errors = [];
let sessionId = '', toolNames = [], elapsedSeconds = 0;
const check = name => { checks.push(name); console.log('PASS ' + name); };
page.on('pageerror', error => errors.push(error.message));
const get = async path => {
  const response = await context.request.get(base + path);
  assert.equal(response.status(), 200, path);
  return response.json();
};
try {
  const books = await get('/api/bookmind/books');
  const textbook = books.find(book => book.material_id === 'bf98a73e3e46dbfa');
  if (textbook) {
    const material = await get('/api/reading/materials/' + textbook.material_id);
    assert(!material.outline.some(row => /^[\uf06e\uf0a7\uf0b7]/.test(row.title)));
    assert(material.outline.some(row => row.locator === 39 && row.title === '数组求和'));
    check('existing-pdf-outline-clean-with-original-page-and-text');
  }
  const demo = books.find(book => book.title === '二叉树 · 学习闭环演示');
  assert(demo, 'Only the dedicated demo may receive live messages.');
  await page.goto(base + demo.links.learn + (resumeSession ? '/sessions/' + resumeSession : ''));
  await page.locator('[data-reading-workspace] textarea').waitFor();
  await page.locator('[data-reader-unit]').first().waitFor();
  if (live) {
    const input = page.locator('[data-reading-workspace] textarea');
    const began = Date.now();
    if (!resumeSession) {
      await input.fill('画个二叉树的示意图我看一下');
      await input.press('Enter');
    }
    await page.waitForURL(url => url.pathname.includes('/sessions/'), { timeout: 180000 });
    sessionId = new URL(page.url()).pathname.split('/').at(-1);
    await page.locator('[data-bookmind-diagram] .dt-svg-root svg').first().waitFor({ timeout: 240000 });
    let saved;
    for (let attempt = 0; attempt < 90; attempt += 1) {
      saved = await get('/api/sessions/' + sessionId);
      if (!['queued', 'running'].includes(saved.status)) break;
      await page.waitForTimeout(1000);
    }
    elapsedSeconds = Math.round((Date.now() - began) / 1000);
    assert(!['queued', 'running'].includes(saved.status));
    const assistant = saved.messages.findLast(message => message.role === 'assistant');
    if (resumeSession && assistant.trace?.ended_at && assistant.trace?.started_at) {
      elapsedSeconds = Math.round(assistant.trace.ended_at - assistant.trace.started_at);
    }
    toolNames = assistant.events.filter(event => event.type === 'tool_call').map(event => event.metadata.tool_name);
    assert(toolNames.includes('submit_visualization'));
    assert(!toolNames.includes('view_figure'));
    assert(toolNames.length < 13, 'A simple diagram should not repeat the previous 13-call sequence.');
    const accepted = assistant.events.find(event => event.type === 'tool_result' && event.metadata.tool_metadata?.bookmind_diagram);
    assert(accepted, 'The diagram envelope must be in the saved session preview.');
    assert.equal(accepted.metadata.tool_metadata.bookmind_diagram.render_type, 'svg');
    assert.equal(saved.preferences.workspace_mode, 'immersive_reading');
    assert.equal(saved.preferences.reading_workspace_id, demo.workspace_id);
    const graphic = page.locator('[data-bookmind-diagram] .dt-svg-root svg').first();
    assert(await graphic.locator('circle, ellipse, rect').count() >= 3, 'A tree needs visible nodes.');
    assert(await graphic.locator('line, path, polyline').count() >= 2, 'A tree needs parent-child edges.');
    await page.locator('[data-bookmind-diagram]').scrollIntoViewIfNeeded();
    await page.screenshot({ path: fileURLToPath(new URL('reading-binary-tree.png', out)) });
    check('live-ordinary-chat-generates-a-native-svg-with-fewer-tools');
    await page.reload();
    await page.locator('[data-bookmind-diagram] .dt-svg-root svg').first().waitFor();
    check('diagram-survives-session-reload');
    await page.setViewportSize({ width: 390, height: 844 });
    // Let the desktop sidebar finish its mobile off-canvas transition.
    await page.waitForTimeout(350);
    await page.locator('[data-bookmind-diagram]').scrollIntoViewIfNeeded();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: fileURLToPath(new URL('mobile-binary-tree.png', out)) });
    check('diagram-fits-mobile-layout');
  }
  assert.equal(errors.length, 0, errors.join('\n'));
} catch (error) {
  await page.screenshot({ path: fileURLToPath(new URL('failure.png', out)) });
  writeFileSync(new URL('failure.txt', out), `${page.url()}\n${await page.locator('body').innerText()}`);
  throw error;
} finally {
  writeFileSync(new URL('report.json', out), JSON.stringify({ live, checks, errors, sessionId, toolNames, elapsedSeconds }, null, 2));
  await browser.close();
}
console.log(JSON.stringify({ live, checks, toolNames, elapsedSeconds }));

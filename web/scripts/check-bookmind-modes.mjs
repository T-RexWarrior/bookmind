import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// Uses only the dedicated demo material. Pass --live to make two real LLM calls.
const live = process.argv.includes('--live');
const base = 'http://127.0.0.1:8018';
const out = new URL('../../data/mode-verification/', import.meta.url);
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true, args: ['--no-proxy-server'] });
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const errors = [], checks = [];
page.on('pageerror', error => errors.push(error.message));
const screenshot = name => page.screenshot({ path: fileURLToPath(new URL(`${name}.png`, out)) });
const check = name => { checks.push(name); console.log('PASS ' + name); };
try {
  const books = await (await context.request.get(base + '/api/bookmind/books')).json();
  const demo = books.find(book => book.title === '二叉树 · 学习闭环演示');
  assert(demo, 'Dedicated demo textbook must exist; never run live checks on a learner textbook.');
  assert.notEqual(demo.links.guide, demo.links.practice);
  const tools = await (await context.request.get(base + '/api/tools')).json();
  assert(tools.enabled_optional_tools.every(name => ['reason', 'brainstorm'].includes(name)));
  for (const name of ['imagegen', 'videogen', 'web_search', 'paper_search']) {
    assert.equal(tools.tools.find(tool => tool.name === name)?.available, false);
  }
  const visuals = await (await context.request.get(base + '/api/visualizers/list')).json();
  assert(visuals.visualizers.every(item => ['svg', 'chartjs', 'mermaid', 'mindmap', 'html'].includes(item.id)));
  check('configured-services-only');

  await page.goto(base + demo.links.learn);
  await page.locator('[data-reader-unit]').first().waitFor();
  assert.equal(await page.locator('.bm-context-header nav a').count(), 3);
  assert.equal(await page.locator('.bm-learning-modes a').count(), 2);
  assert.match(await page.locator('.bm-learning-modes a.active').innerText(), /自主学习/);
  await screenshot('01-self-directed');
  check('self-directed-reading');

  await page.goto(base + demo.links.guide);
  await page.locator('#mastery-outline-start').waitFor();
  assert.match(await page.locator('.bm-learning-modes a.active').innerText(), /AI 导学/);
  assert.match(await page.locator('.bm-context-header nav a.active').innerText(), /学习/);
  await page.getByRole('button', { name: '查看教材原文', exact: true }).click();
  await page.locator('.bm-source-text').waitFor();
  assert((await page.locator('.bm-source-text').innerText()).length > 10);
  await screenshot('02-guidance-source');
  await page.keyboard.press('Escape');
  await page.getByRole('dialog', { name: '教材原文' }).waitFor({ state: 'hidden' });
  check('guidance-map-and-source-preview');

  const history = await (await context.request.get(base + '/api/mastery-paths/topics/' + demo.path_id + '/sessions')).json();
  const recent = history.sessions.find(session => !session.archived);
  if (recent) {
    const sessionId = recent.session_id || recent.id;
    assert(sessionId);
    const guidedUrl = base + demo.links.guide + '/sessions/' + sessionId;
    await page.goto(guidedUrl);
    await page.getByRole('button', { name: '图解说明', exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: /Play audio|朗读此回复|播放音频/i }).count(), 0);
    await page.locator('.bm-learning-modes a').filter({ hasText: '自主学习' }).click();
    await page.locator('[data-reader-unit]').first().waitFor();
    await page.locator('.bm-learning-modes a').filter({ hasText: 'AI 导学' }).click();
    await page.waitForURL(guidedUrl);
    await page.getByRole('button', { name: '图解说明', exact: true }).waitFor();
    check('learning-mode-resumes-its-own-session');
  }

  await page.goto(base + demo.links.practice);
  await page.getByRole('heading', { name: '用一道题，检验一份理解' }).waitFor();
  assert.equal(await page.locator('.bm-learning-modes').count(), 0);
  await screenshot('03-practice');
  if (live) {
    const before = demo.objectives.reduce((sum, point) => sum + point.evidence_count, 0);
    const responsePromise = page.waitForResponse(response => response.url().includes('/extensions/quiz/actions/start') && response.request().method() === 'POST', { timeout: 240000 });
    await page.getByRole('button', { name: '生成三道题', exact: true }).click();
    const response = await responsePromise;
    assert.equal(response.status(), 200, await response.text());
    const result = await response.json();
    assert.equal(result.payload.questions.length, 3);
    assert(result.payload.questions.every(question => !('correct_choice_index' in question) && !('evidence' in question)));
    const first = result.payload.questions[0];
    const answerPromise = page.waitForResponse(response => response.url().includes('/extensions/quiz/answers') && response.request().method() === 'POST');
    await page.locator('.bm-practice-quiz').getByRole('button').filter({ hasText: first.choices[0] }).first().click();
    assert.equal((await answerPromise).status(), 200);
    const afterBook = await (await context.request.get(base + '/api/bookmind/books/' + demo.id)).json();
    assert(afterBook.objectives.reduce((sum, point) => sum + point.evidence_count, 0) > before);
    await screenshot('04-practice-answer');
    check('live-native-quiz-hidden-key-and-shared-evidence');
  }
  await page.getByRole('button', { name: '错题本', exact: true }).click();
  await page.getByRole('heading', { name: '本书错题', exact: true }).waitFor();
  check('same-book-mistakes');
  await page.getByRole('button', { name: '复习安排', exact: true }).click();
  await page.getByRole('heading', { name: '让记住的知识，留得更久' }).waitFor();
  await screenshot('05-review');
  check('shared-review-schedule');

  if (live) {
    await page.goto(base + demo.links.guide + '/sessions');
    const input = page.locator('textarea').last();
    await input.waitFor();
    await input.fill('请只核对教材第1节原文：先调用 read_material 读取该节，再用两句话解释二叉树每个结点最多有几个孩子，使用 [p.1] 引用。现在不出题，也不评估掌握度。');
    await input.press('Enter');
    await page.locator('.bm-response-sources a').first().waitFor({ timeout: 240000 });
    await page.locator('.bm-response-sources a').first().click();
    await page.locator('.bm-source-text').waitFor();
    assert.match(await page.locator('.bm-source-text').innerText(), /二叉树/);
    await screenshot('06-live-guidance-citation');
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('button', { name: '图解说明', exact: true }).count(), 1);
    check('live-guidance-reading-evidence-and-citation-navigation');
  }
  await page.goto(base + '/api-settings');
  assert.equal(await page.getByLabel('API Key', { exact: true }).inputValue(), '');
  await page.getByRole('region', { name: '当前能力范围' }).waitFor();
  check('settings-key-redacted-and-services-explained');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base + demo.links.practice);
  await page.getByRole('heading', { name: '用一道题，检验一份理解' }).waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await screenshot('07-mobile-practice');
  await page.goto(base + demo.links.guide);
  await page.locator('#mastery-outline-start').waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await screenshot('08-mobile-guidance');
  check('mobile-modes-and-practice');
  assert.equal(errors.length, 0, errors.join('\n'));
} finally {
  const report = JSON.stringify({ live, checks, errors }, null, 2);
  writeFileSync(new URL('report.json', out), report);
  writeFileSync(new URL(live ? 'live-report.json' : 'smoke-report.json', out), report);
  await browser.close();
}
console.log(JSON.stringify({ live, checks, errors }));

import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// --live creates one dedicated demonstration through the real native Books pipeline.
const live = process.argv.includes('--live');
const resumeId = process.argv.find(value => value.startsWith('--resume='))?.slice('--resume='.length);
const finishId = process.argv.find(value => value.startsWith('--finish='))?.slice('--finish='.length);
const base = 'http://127.0.0.1:8018';
const out = new URL('../../data/goal-verification/', import.meta.url);
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true, args: ['--no-proxy-server'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const checks = [], errors = [];
let generatedBookId = '';
page.on('pageerror', error => errors.push(error.message));
page.setDefaultTimeout(30000);
const check = name => { checks.push(name); console.log('PASS ' + name); };
const screenshot = name => page.screenshot({ path: fileURLToPath(new URL(name + '.png', out)) });
try {
  const response = await page.request.get(base + '/api/books');
  assert.equal(response.status(), 200);
  assert(Array.isArray((await response.json()).books));
  check('native-books-api-restored');
  await page.goto(base + '/');
  await page.getByRole('heading', { name: '已有资料', exact: true }).waitFor();
  await page.getByRole('heading', { name: '已有目标', exact: true }).waitFor();
  const initialBooks = await (await page.request.get(base + '/api/bookmind/books')).json();
  if (initialBooks.length) await page.locator('.bm-book-grid article').first().waitFor();
  await screenshot('01-two-starting-points');
  await page.getByRole('link', { name: '从目标生成学习资料', exact: true }).click();
  await page.getByRole('heading', { name: '从一个目标，开始学习', exact: true }).waitFor();
  await page.getByLabel('想学习什么？').waitFor();
  await screenshot('02-goal-form');
  check('goal-entry-and-form');
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  const main = page.locator('.bm-goal-page');
  await main.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await screenshot('03-mobile-goal-form');
  check('mobile-goal-form');
  await page.setViewportSize({ width: 1440, height: 1000 });
  if (live) {
    if (resumeId || finishId) {
      generatedBookId = resumeId || finishId;
      await page.goto(base + '/learning/goals?book=' + encodeURIComponent(generatedBookId));
      check('resume-saved-native-book');
    } else {
    await page.getByLabel('想学习什么？').fill('我想理解数据结构中的二叉树与空树，只学习这个小知识点。请仅设计1个章，中文写作，精简讲解，围绕左右孩子的区别、每个结点至多两个孩子、空树展开。');
    await page.getByLabel('已有基础与学习要求（可选）').fill('这是 BookMind 目标生成的专用联调演示。会 C++ 基础；不讲遍历，不需要长篇扩展。');
    await page.getByLabel('资料深度').selectOption('brief');
    await page.getByRole('button', { name: '生成学习方案', exact: true }).click();
    await page.getByRole('heading', { name: '确认学习方案', exact: true }).waitFor({ timeout: 600000 });
    generatedBookId = new URL(page.url()).searchParams.get('book');
    assert(generatedBookId);
    writeFileSync(new URL('live-book.json', out), JSON.stringify({ generatedBookId }));
    await page.getByLabel('资料标题', { exact: true }).fill('二叉树入门 · 目标生成演示');
    await page.getByLabel('预计章节数', { exact: true }).fill('1');
    await screenshot('04-native-proposal');
    check('live-native-ideation-proposal');
    await page.getByRole('button', { name: '确认方案，生成目录', exact: true }).click();
    }
    if (!finishId) {
    await page.getByRole('heading', { name: '调整章节与学习目标', exact: true }).waitFor({ timeout: 600000 });
    const chapters = page.locator('.bm-goal-outline > article');
    while (await chapters.count() > 1) await page.getByRole('button', { name: `删除第 ${await chapters.count()} 章`, exact: true }).click();
    await page.getByLabel('章节标题', { exact: true }).fill('二叉树与空树');
    await page.getByLabel('学习目标（每行一个）', { exact: true }).fill('理解二叉树每个结点至多有两个孩子\n区分左孩子和右孩子\n理解空树也是二叉树');
    await page.getByLabel('章节说明', { exact: true }).fill('仅讲二叉树与空树的基础定义，用两个简短例子说明。精简入门，不扩展遍历或复杂算法。');
    await screenshot('05-native-spine');
    check('live-native-spine-editing');
    await page.getByRole('button', { name: '确认目录，生成资料', exact: true }).click();
    await page.getByRole('heading', { name: '正在逐章生成学习资料', exact: true }).waitFor();
    await page.reload();
    await page.getByRole('heading', { name: '正在逐章生成学习资料', exact: true }).waitFor();
    check('generation-survives-page-reload');
    }
    await page.getByRole('heading', { name: '学习资料已生成', exact: true }).waitFor({ timeout: 1200000 });
    await screenshot('06-native-compiled-pages');
    check('live-native-chapter-compilation');
    let textbook = (await (await page.request.get(base + '/api/bookmind/books')).json()).find(book => book.generated_book_id === generatedBookId);
    if (textbook) {
      await page.getByRole('button', { name: /继续学习|加入教材并开始学习/, exact: true }).click();
    } else {
      const importResponse = page.waitForResponse(response => response.url().includes('/generated/') && new URL(response.url()).pathname.endsWith('/import') && response.request().method() === 'POST');
      await page.getByRole('button', { name: '加入教材并开始学习', exact: true }).click();
      const imported = await importResponse;
      assert.equal(imported.status(), 200, await imported.text());
      textbook = await imported.json();
    }
    assert.equal(textbook.generated_book_id, generatedBookId);
    assert(textbook.objectives.length > 0);
    assert.deepEqual(Object.keys(textbook.links).sort(), ['guide', 'learn', 'notes', 'practice']);
    await page.locator('[data-reader-unit]').first().waitFor();
    await screenshot('07-generated-material-reading');
    check('generated-book-imported-into-native-reading');
    for (const mode of ['guide', 'practice', 'notes']) {
      await page.goto(base + textbook.links[mode]);
      await page.locator('.bm-page-nav a.active').waitFor();
      assert.equal(await page.locator('.bm-page-nav a').count(), 4);
    }
    const again = await page.request.post(base + `/api/bookmind/generated/${generatedBookId}/import`);
    assert.equal((await again.json()).id, textbook.id);
    check('shared-learning-path-practice-notes-and-idempotent-import');
  }
  assert.deepEqual(errors, []);
} catch (error) {
  errors.push(error.message);
  throw error;
} finally {
  writeFileSync(new URL(live ? 'live-report.json' : 'smoke-report.json', out), JSON.stringify({ live, generatedBookId, checks, errors }, null, 2));
  await browser.close();
}
console.log(JSON.stringify({ live, generatedBookId, checks, errors }));

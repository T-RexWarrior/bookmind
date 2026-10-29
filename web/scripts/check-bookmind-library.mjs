import assert from 'node:assert/strict';
import { chromium } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const base = process.env.BOOKMIND_URL || 'http://127.0.0.1:8018';
const out = new URL('../../data/library-verification/', import.meta.url);
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.BOOKMIND_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
page.setDefaultTimeout(30000);
const errors = [], checks = [];
const pass = label => { checks.push(label); console.log('PASS', label); };
const screenshot = name => page.screenshot({ path: fileURLToPath(new URL(name + '.png', out)) });
page.on('pageerror', error => errors.push(error.message));
let disposable, generated;
try {
  const title = `教材删除恢复验证-${Date.now()}`;
  const uploaded = await page.request.post(base + '/api/reading/materials', { multipart: {
    file: { name: title + '.md', mimeType: 'text/markdown', buffer: Buffer.from('# 删除恢复测试\n\n只供书架回归。\n\n## 知识点\n\n二叉树每个结点至多有两个孩子。') },
  } });
  assert.equal(uploaded.status(), 200, await uploaded.text());
  const material = await uploaded.json();
  const bound = await page.request.post(base + '/api/bookmind/books', { data: { material_id: material.material_id } });
  assert.equal(bound.status(), 201);
  disposable = await bound.json();
  await page.goto(base + '/');
  await page.getByRole('button', { name: `删除教材《${disposable.title}》`, exact: true }).click();
  await page.getByRole('alertdialog').waitFor();
  await screenshot('01-removal-confirmation');
  await page.getByRole('button', { name: '取消', exact: true }).click();
  assert((await (await page.request.get(base + '/api/bookmind/books')).json()).some(b => b.id === disposable.id));
  pass('cancel-preserves-the-book');
  await page.getByRole('button', { name: `删除教材《${disposable.title}》`, exact: true }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await page.getByRole('alertdialog').waitFor({ state: 'hidden' });
  await page.reload();
  await page.locator('.bm-removed-books summary').waitFor();
  assert.equal(await page.getByRole('button', { name: `删除教材《${disposable.title}》`, exact: true }).count(), 0);
  assert.equal((await page.request.get(base + '/api/bookmind/books/' + disposable.id)).status(), 404);
  pass('removal-survives-reload');
  await page.locator('.bm-removed-books summary').click();
  const removedRow = page.locator('.bm-removed-books > div').filter({ hasText: disposable.title });
  await removedRow.getByRole('button', { name: '恢复教材', exact: true }).click();
  await page.getByRole('button', { name: `删除教材《${disposable.title}》`, exact: true }).waitFor();
  const restored = await (await page.request.get(base + '/api/bookmind/books/' + disposable.id)).json();
  assert.equal(restored.path_id, disposable.path_id);
  assert.equal(restored.notebook_id, disposable.notebook_id);
  assert.deepEqual(restored.objectives, disposable.objectives);
  pass('restore-keeps-original-learning-identities');
  // Optional: verify the dedicated, real-model generation demo, never a user's book.
  if (process.argv.includes('--generated')) {
    const demo = JSON.parse(readFileSync(new URL('../../data/generation-quality-verification/report.json', import.meta.url), 'utf8'));
    generated = await (await page.request.get(base + '/api/bookmind/books/' + demo.book_id)).json();
    assert(generated.generated_book_id === demo.native_book_id);
    await page.getByRole('button', { name: `删除教材《${generated.title}》`, exact: true }).click();
    await page.getByRole('button', { name: '确认删除', exact: true }).click();
    await page.getByRole('alertdialog').waitFor({ state: 'hidden' });
    await page.reload();
    await page.locator('.bm-removed-books summary').waitFor();
    assert.equal(await page.locator('.bm-goal-drafts a').filter({ hasText: generated.title }).count(), 0);
    await page.request.post(base + `/api/bookmind/books/${generated.id}/restore`);
    pass('removed-generated-book-does-not-resurface-as-a-draft');
    await page.goto(base + generated.links.learn);
    await page.locator('[data-reader-unit]').first().waitFor();
    const expand = page.getByRole('button', { name: '展开目录', exact: true });
    if (await expand.isVisible()) await expand.click();
    // Resolve a real source locator rather than racing reader navigation/loading.
    let figureLocator = 0;
    for (let i = 1; i <= generated.unit_count; i++) {
      const source = await (await page.request.get(base + `/api/reading/materials/${generated.material_id}/units/${i}`)).json();
      if (source.text.includes('```svg')) { figureLocator = i; break; }
    }
    assert(figureLocator > 0);
    await page.goto(base + generated.links.learn + '?locator=' + figureLocator);
    const figure = page.locator('[data-book-figure="svg"]');
    await figure.first().locator('svg').waitFor();
    await figure.first().screenshot({ path: fileURLToPath(new URL('02-generated-figure.png', out)) });
    pass('generated-svg-renders-as-a-diagram');
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await screenshot('03-mobile-reader');
    pass('mobile-reader-width');
  }
  assert.deepEqual(errors, []);
} catch (error) {
  await screenshot('failure');
  throw error;
} finally {
  if (disposable) await page.request.delete(base + `/api/bookmind/books/${disposable.id}`);
  if (generated) await page.request.post(base + `/api/bookmind/books/${generated.id}/restore`);
  writeFileSync(new URL('report.json', out), JSON.stringify({ checks, errors }, null, 2));
  await browser.close();
}

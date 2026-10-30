import assert from 'node:assert/strict';
import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// Read-only guidance check by default. --clear-generations explicitly removes
// all currently visible, unimported generation records through the shelf UI.
const clear = process.argv.includes('--clear-generations');
const bookId = process.argv.find(arg => arg.startsWith('--book='))?.slice(7);
const base = process.env.BOOKMIND_URL || 'http://127.0.0.1:8018';
const out = new URL('../../data/guidance-ui-verification/', import.meta.url);
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.BOOKMIND_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
const page = await browser.newPage({ viewport: { width: 1550, height: 1100 } });
page.setDefaultTimeout(30000);
const checks = [], errors = [];
page.on('pageerror', error => errors.push(error.message));
const pass = text => { checks.push(text); console.log('PASS', text); };
const screenshot = name => page.screenshot({ path: fileURLToPath(new URL(name + '.png', out)) });
let cleared = [];
try {
  const books = await (await page.request.get(base + '/api/bookmind/books')).json();
  const book = books.find(row => row.id === bookId);
  assert(book, 'Pass --book=<an existing textbook id>.');
  const topicResponse = await page.request.get(base + '/api/mastery-paths/topics/' + book.path_id);
  assert.equal(topicResponse.status(), 200);
  const topic = await topicResponse.json();
  assert(topic.map.counts.total > 0);
  await page.goto(base + book.links.guide);
  await page.getByRole('heading', { name: '学习记录', exact: true }).waitFor();
  assert.equal(await page.getByText('找不到这张地图', { exact: true }).count(), 0);
  pass('guidance-route-and-learning-records-render');
  assert.equal(await page.getByRole('region', { name: 'AI 学习目标规划' }).count(), 0);
  assert.equal(await page.getByText('这一节，需要学会什么？', { exact: true }).count(), 0);
  pass('guidance-opens-without-a-separate-planning-form');
  await page.locator('main.mastery-shell details').first().locator('summary').click();
  assert.equal(await page.getByRole('button', { name: /删除主题|Delete topic/ }).count(), 0);
  await page.locator('main.mastery-shell details').first().locator('summary').click();
  pass('bound-textbook-has-no-independent-delete-topic-action');
  await screenshot('01-restored-guidance');
  const conversation = page.getByRole('button').filter({ hasText: '二叉树深度与高度导学' });
  if (await conversation.count()) {
    await conversation.click();
    await page.getByText(/我正在学二叉树，知道每个结点至多有两个孩子/).first().waitFor();
    await page.getByText(/我记住了：结点深度从根往下数/).first().waitFor();
    assert.equal(await page.getByText(/【演示学习】|【演示作答】|【演示小结】/).count(), 0);
    await screenshot('02-guidance-conversation');
    pass('saved-guidance-conversation-is-readable-without-demo-labels');
  }
  if (clear) {
    const allBooks = await (await page.request.get(base + '/api/bookmind/books?include_removed=true')).json();
    const imported = new Set(allBooks.map(row => row.generated_book_id).filter(Boolean));
    const native = (await (await page.request.get(base + '/api/books')).json()).books;
    const targets = native.filter(row => row.status !== 'archived' && !imported.has(row.id));
    await page.goto(base + '/');
    if (targets.length) {
      await page.getByRole('button', { name: '清空记录', exact: true }).click();
      await page.getByRole('alertdialog').waitFor();
      await screenshot('03-clear-generation-confirmation');
      const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/bookmind/generated/clear' && r.request().method() === 'POST');
      await page.getByRole('button', { name: '确认清理', exact: true }).click();
      const result = await response;
      assert.equal(result.status(), 200, await result.text());
      cleared = (await result.json()).removed;
      assert.deepEqual([...cleared].sort(), targets.map(row => row.id).sort());
    }
    await page.reload();
    await page.getByRole('heading', { name: '继续学习', exact: true }).waitFor();
    assert.equal(await page.locator('.bm-goal-drafts').count(), 0);
    const remaining = await (await page.request.get(base + '/api/bookmind/books')).json();
    assert.deepEqual(remaining.map(row => row.id).sort(), books.map(row => row.id).sort());
    const after = await (await page.request.get(base + '/api/mastery-paths/topics/' + book.path_id)).json();
    assert.equal(after.path_revision, topic.path_revision);
    await screenshot('04-clean-home');
    pass('generation-records-cleared-without-changing-textbooks-or-learning');
  }
  assert.deepEqual(errors, []);
} catch (error) {
  await screenshot('failure');
  throw error;
} finally {
  writeFileSync(new URL('report.json', out), JSON.stringify({ checks, errors, cleared }, null, 2));
  await browser.close();
}

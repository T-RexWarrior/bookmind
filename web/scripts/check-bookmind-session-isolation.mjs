import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// All writes, including --live's single model turn, use only the dedicated demo book.
const live = process.argv.includes('--live');
const base = 'http://127.0.0.1:8018';
const out = new URL('../../data/session-isolation-verification/', import.meta.url);
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true, args: ['--no-proxy-server'] });
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(30000);
const checks = [], errors = [], commands = [], loadedSessions = [];
const check = name => { checks.push(name); console.log('PASS ' + name); };
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => {
  const match = new URL(request.url()).pathname.match(/^\/api\/sessions\/([^/]+)$/);
  if (match && request.method() === 'GET') loadedSessions.push(match[1]);
});
page.on('websocket', socket => socket.on('framesent', event => {
  try { const value = JSON.parse(String(event.payload)); if (value.type === 'start_turn') commands.push(value); } catch { /* Subscription frames need no assertions. */ }
}));
const json = async path => {
  const response = await context.request.get(base + path);
  assert.equal(response.status(), 200, path);
  return response.json();
};
const practice = async book => {
  const response = await context.request.post(`${base}/api/bookmind/books/${book.id}/practice/session`);
  assert.equal(response.status(), 200);
  return response.json();
};
const readingReady = async () => {
  await page.locator('[data-reading-workspace]').waitFor();
  await page.locator('[data-reader-unit]').first().waitFor();
  await page.locator('[data-reading-workspace] textarea').waitFor();
};
const tab = name => page.getByRole('navigation', { name: '教材页面' }).getByRole('link', { name, exact: true });

try {
  const books = await json('/api/bookmind/books');
  const book = books.find(row => row.title === '二叉树 · 学习闭环演示');
  assert(book, 'Never use learner material for live checks.');
  const before = await practice(book);
  assert(before.questions.length >= 2, 'The dedicated demo needs an existing multi-question practice session.');
  const practiceSession = await json('/api/sessions/' + before.session_id);
  const practicePrompts = practiceSession.messages.filter(row => row.role === 'user' && row.content?.length > 20).map(row => row.content);
  assert(practicePrompts.length);

  await page.goto(base + book.links.practice);
  await page.locator('.bm-practice-quiz').waitFor();
  await page.getByRole('button', { name: '生成练习', exact: true }).waitFor();
  // Delay textbook hydration to exercise the original practice -> reading race.
  await page.route(`**/api/reading/workspaces/${book.workspace_id}`, async route => {
    await new Promise(resolve => setTimeout(resolve, 700));
    await route.continue();
  });
  await tab('自主学习').click();
  await readingReady();
  assert(!page.url().includes(before.session_id));
  for (const prompt of practicePrompts) assert(!(await page.locator('[data-chat-scroll-root]').innerText()).includes(prompt));
  assert.equal(new URL(page.url()).pathname, book.links.learn, 'Opening a fresh reading draft must not inherit a selected practice id.');
  check('practice-to-reading-waits-for-own-draft');

  let ownReadingId;
  if (live) {
    const marker = '自主学习会话隔离验证通过';
    const input = page.locator('[data-reading-workspace] textarea');
    await input.fill(`请只回复“${marker}”。无需出题，无需调用工具，也不要评估掌握度。`);
    await input.press('Enter');
    await page.waitForURL(url => url.pathname.startsWith(book.links.learn + '/sessions/'), { timeout: 180000 });
    ownReadingId = new URL(page.url()).pathname.split('/').at(-1);
    assert.notEqual(ownReadingId, before.session_id);
    const command = commands.at(-1);
    assert(command, 'Observe the actual start_turn command.');
    assert.equal(command.workspace_mode, 'immersive_reading');
    assert.equal(command.reading_workspace_id, book.workspace_id);
    assert(!command.mastery_path_id);
    assert(!command.session_id || command.session_id === ownReadingId);
    let saved;
    await page.waitForFunction(() => !document.querySelector('[data-reading-workspace] textarea')?.disabled);
    for (let attempt = 0; attempt < 90; attempt += 1) {
      saved = await json('/api/sessions/' + ownReadingId);
      if (saved.messages.some(row => row.role === 'assistant' && row.content?.includes(marker)) && saved.status !== 'running' && saved.status !== 'queued') break;
      await page.waitForTimeout(1000);
    }
    assert(saved.messages.some(row => row.role === 'assistant' && row.content?.includes(marker)));
    assert.equal(saved.preferences.workspace_mode, 'immersive_reading');
    assert.equal(saved.preferences.reading_workspace_id, book.workspace_id);
    assert(!saved.preferences.mastery_path_id);
    assert(!saved.messages.some(row => row.role === 'user' && practicePrompts.includes(row.content)));
    await page.screenshot({ path: fileURLToPath(new URL('reading-own-response.png', out)) });
    check('live-message-admitted-to-independent-reading-session');
  }

  for (let attempt = 0; attempt < 3; attempt += 1) {
    await tab('练习巩固').click();
    await page.locator('.bm-practice-quiz').waitFor();
    assert.equal(await page.locator('[data-reading-workspace]').count(), 0);
    await tab('自主学习').click();
    await readingReady();
    assert(!page.url().includes(before.session_id));
    if (ownReadingId) assert(page.url().includes(ownReadingId));
    for (const prompt of practicePrompts) assert(!(await page.locator('[data-chat-scroll-root]').innerText()).includes(prompt));
  }
  check('repeated-tab-switches-resume-the-correct-conversation');

  const history = await json(`/api/mastery-paths/topics/${book.path_id}/sessions`);
  const guided = history.sessions.find(row => !row.archived);
  const guideId = guided?.session_id || guided?.id;
  if (guideId) {
    await page.goto(base + book.links.guide + '/sessions/' + guideId);
    await page.getByRole('button', { name: '图解说明', exact: true }).waitFor();
    await tab('自主学习').click();
    await readingReady();
    assert(!page.url().includes(guideId));
    if (ownReadingId) assert(page.url().includes(ownReadingId));
    check('guidance-to-reading-resumes-independent-chat');
  }

  const readingRows = await json(`/api/reading/workspaces/${book.workspace_id}/sessions`);
  assert(readingRows.sessions.length, 'Recovery should have a valid existing reading conversation.');
  for (const foreignId of [before.session_id, guideId].filter(Boolean)) {
    const requestCount = loadedSessions.filter(id => id === foreignId).length;
    await page.goto(`${base}${book.links.learn}/sessions/${foreignId}?locator=1`);
    await page.waitForURL(url => readingRows.sessions.some(row => url.pathname.endsWith('/sessions/' + row.session_id)));
    await readingReady();
    assert.equal(loadedSessions.filter(id => id === foreignId).length, requestCount, 'Never fetch a foreign transcript from a poisoned reading URL.');
    assert.equal(new URL(page.url()).searchParams.get('locator'), '1');
  }
  check('old-poisoned-reading-urls-recover-without-loading-foreign-transcripts');

  const after = await practice(book);
  assert.equal(after.session_id, before.session_id);
  assert.equal(after.turn_id, before.turn_id);
  assert.deepEqual(after.questions, before.questions);
  const refreshedBook = await json('/api/bookmind/books/' + book.id);
  assert.deepEqual(refreshedBook.objectives.map(row => row.evidence_count), book.objectives.map(row => row.evidence_count));
  check('practice-questions-answers-and-shared-assessment-evidence-preserved');
  assert.equal(errors.length, 0, errors.join('\n'));
} catch (error) {
  await page.screenshot({ path: fileURLToPath(new URL('failure.png', out)) });
  writeFileSync(new URL('failure.txt', out), `${page.url()}\n${await page.locator('body').innerText()}`);
  throw error;
} finally {
  writeFileSync(new URL('report.json', out), JSON.stringify({ live, checks, errors }, null, 2));
  await browser.close();
}

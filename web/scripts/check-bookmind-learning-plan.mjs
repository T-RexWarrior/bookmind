import assert from 'node:assert/strict';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';

const live = process.argv.includes('--live');
const base = 'http://127.0.0.1:8018';
const out = new URL('../../data/learning-plan-verification/', import.meta.url);
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.BOOKMIND_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const books = await (await page.request.get(base + '/api/bookmind/books')).json();
  const demo = JSON.parse(readFileSync(new URL('demo-book.json', out), 'utf8'));
  const book = books.find(row => row.id === demo.id && row.workspace_id === demo.workspace_id);
  assert(book, 'Prepare the dedicated learning-plan demo before running this check.');
  const section = book.sections.find(row => row.name.includes('中序遍历'));
  await page.goto(base + book.links.guide + '?objective=' + section.knowledge_point_id);
  const panel = page.getByRole('region', { name: 'AI 学习目标规划' });
  await panel.getByLabel('规划教材小节').selectOption(section.knowledge_point_id);
  const unplanned = await panel.getByRole('button', { name: '生成学习目标', exact: true }).count();
  if (unplanned) {
    await panel.getByLabel('学习目的', { exact: true }).fill('既要理解中序遍历原理，也要能独立推演并实现递归算法');
    await panel.getByLabel('已有基础', { exact: true }).fill('知道二叉树和递归的基本概念，尚不能独立写出遍历');
  }
  if (live && unplanned) {
    const response = page.waitForResponse(value => new URL(value.url()).pathname.endsWith('/learning-plan/preview') && value.request().method() === 'POST', { timeout: 180000 });
    await panel.getByRole('button', { name: '生成学习目标', exact: true }).click();
    const result = await response;
    const preview = await result.json();
    assert.equal(result.status(), 200, JSON.stringify(preview));
    assert(preview.goals.some(goal => goal.type === 'procedure'), 'Algorithm implementation needs a procedure objective.');
    assert(preview.goals.every(goal => goal.mastery_requirement && goal.source_locators.length));
    await panel.getByRole('button', { name: '采用学习目标', exact: true }).waitFor();
    await panel.screenshot({ path: fileURLToPath(new URL('preview.png', out)) });
    await panel.getByRole('button', { name: '采用学习目标', exact: true }).click();
    await panel.getByText('本节学习目标 · 已加入导学路线').waitFor();
    writeFileSync(new URL('preview.json', out), JSON.stringify(preview, null, 2));
    console.log('PASS live source-grounded goals include procedural learning and explicit mastery requirements');
  }
  await panel.getByText('本节学习目标 · 已加入导学路线').waitFor();
  await panel.screenshot({ path: fileURLToPath(new URL('../../docs/images/ai-learning-plan.png', import.meta.url)) });
  await page.reload();
  await page.getByText('本节学习目标 · 已加入导学路线').waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  const mobilePanel = page.getByRole('region', { name: 'AI 学习目标规划' });
  await mobilePanel.waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2));
  await mobilePanel.screenshot({ path: fileURLToPath(new URL('mobile.png', out)) });
  assert.deepEqual(errors, []);
  console.log('PASS learning plan persists, stays linked to the textbook and fits mobile');
} finally { await browser.close(); }

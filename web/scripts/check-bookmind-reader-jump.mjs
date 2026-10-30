import assert from 'node:assert/strict';
import { chromium } from '@playwright/test';

// Uses an existing text material and mocks its position endpoint, so checking
// the resume/navigation race does not change the learner's saved position.
const bookId = process.argv.find(arg => arg.startsWith('--book='))?.slice(7);
const locator = Number(process.argv.find(arg => arg.startsWith('--locator='))?.slice(10));
assert(bookId && locator > 1, 'Pass --book=<textbook id> --locator=<section after 1>.');
const savedLocator = locator === 2 ? 1 : 2;
const base = process.env.BOOKMIND_URL || 'http://127.0.0.1:8018';
const browser = await chromium.launch({ executablePath: process.env.BOOKMIND_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
let release;
const positionReady = new Promise(resolve => { release = resolve; });
let positionRequested = false;
try {
  const books = await (await page.request.get(base + '/api/bookmind/books')).json();
  const book = books.find(row => row.id === bookId);
  assert(book);
  const positionPath = `/api/reading/materials/${book.material_id}/position`;
  await page.route('**' + positionPath + '*', async route => {
    if (route.request().method() === 'GET') {
      positionRequested = true;
      await positionReady;
      await route.fulfill({ json: { locator: savedLocator, source_anchor: '', percentage: 0.1, updated_at: 1 } });
    } else {
      const value = route.request().postDataJSON();
      await route.fulfill({ json: { ...value, source_anchor: '', updated_at: 1 } });
    }
  });
  await page.goto(base + book.links.learn + '?locator=' + locator);
  const target = page.locator(`[data-reader-unit="${locator}"]`);
  await target.waitFor();
  assert(positionRequested, 'The test must exercise the outstanding resume request.');
  const resumed = page.waitForResponse(response => new URL(response.url()).pathname === positionPath && response.request().method() === 'GET');
  release();
  await (await resumed).finished();
  // Let the response handler and React effects finish before checking that
  // the older saved position did not replace the explicit destination.
  await page.waitForTimeout(300);
  assert.equal(await target.count(), 1);
  assert.deepEqual(errors, []);
  console.log('PASS explicit section jump survives a later saved-position response');
} finally {
  release();
  await browser.close();
}

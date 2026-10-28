/** Verify brand migration against real browser storage, without learner data. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import ts from 'typescript';
import { chromium } from '@playwright/test';

const server = createServer((_request, response) => {
  response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  response.end('<!doctype html><html><body>Storage migration verification</body></html>');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({
  executablePath: process.env.BOOKMIND_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: true,
});
try {
  const page = await browser.newPage();
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  const compiled = ts.transpileModule(
    readFileSync(new URL('../lib/brand-migration.ts', import.meta.url), 'utf8'),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
  ).outputText;
  await page.addScriptTag({ content: `const exports = {}; ${compiled}; window.brand = exports;` });
  const result = await page.evaluate(async () => {
    const { LEGACY_STORAGE_PREFIX: old, STORAGE_MIGRATION_SCRIPT, openBookMindDatabase } = window.brand;
    localStorage.setItem(`${old}-theme`, 'dark');
    localStorage.setItem('bookmind-theme', 'snow');
    localStorage.setItem(`${old}-language`, 'zh');
    localStorage.setItem(`${old}x-unrelated`, 'keep');
    sessionStorage.setItem(`${old}.activeSessionId.tab`, 'reading-example');
    const runMigration = () => {
      const script = document.createElement('script');
      script.textContent = STORAGE_MIGRATION_SCRIPT;
      document.head.appendChild(script);
    };
    runMigration();
    const copiedLanguage = localStorage.getItem('bookmind-language');
    localStorage.removeItem('bookmind-language');
    runMigration();
    const storage = {
      theme: localStorage.getItem('bookmind-theme'), copiedLanguage,
      active: sessionStorage.getItem('bookmind.activeSessionId.tab'),
      unrelatedCopied: localStorage.getItem('bookmindx-unrelated'),
      deletedRestored: localStorage.getItem('bookmind-language'),
      oldKept: localStorage.getItem(`${old}-language`),
    };
    const seed = (name, version, keyPath, rows) => new Promise((resolve, reject) => {
      const request = indexedDB.open(name, version);
      request.onupgradeneeded = () => request.result.createObjectStore('records', keyPath ? { keyPath } : undefined);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const db = request.result, tx = db.transaction('records', 'readwrite');
        for (const [key, value] of rows) {
          if (keyPath) tx.objectStore('records').put(value);
          else tx.objectStore('records').put(value, key);
        }
        tx.oncomplete = () => { db.close(); resolve(); };
        tx.onerror = () => reject(tx.error);
      };
    });
    const all = db => new Promise((resolve, reject) => {
      const request = db.transaction('records').objectStore('records').getAll();
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const upgrade = db => { if (!db.objectStoreNames.contains('records')) db.createObjectStore('records'); };
    await seed(`${old}-draft-check`, 1, null, [
      ['unsent', { text: '尚未发送的草稿', attachments: [{ filename: 'demo.txt', base64: 'ZGVtbw==' }] }],
      ['conflict', { text: '旧值' }],
    ]);
    await seed('bookmind-draft-check', 1, null, [['conflict', { text: '新值' }]]);
    let db = await openBookMindDatabase('bookmind-draft-check', 2, upgrade);
    const drafts = await all(db);
    await new Promise((resolve, reject) => {
      const tx = db.transaction('records', 'readwrite');
      tx.objectStore('records').delete('unsent');
      tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error);
    });
    db.close();
    db = await openBookMindDatabase('bookmind-draft-check', 2, upgrade);
    const afterReopen = await all(db); db.close();
    await seed(`${old}-agents-check`, 1, 'source', [['unused', { source: 'codex', folderName: 'example' }]]);
    db = await openBookMindDatabase('bookmind-agents-check', 3,
      target => target.createObjectStore('records', { keyPath: 'id' }),
      (_store, value) => ({ ...value, id: `${value.source}-legacy` }));
    const agents = await all(db); db.close();
    db = await openBookMindDatabase('bookmind-absent-check', 2, upgrade); db.close();
    const names = (await indexedDB.databases()).map(row => row.name);
    return { storage, drafts, afterReopen, agents, absentLegacyCreated: names.includes(`${old}-absent-check`) };
  });
  assert.deepEqual(result.storage, {
    theme: 'snow', copiedLanguage: 'zh', active: 'reading-example',
    unrelatedCopied: null, deletedRestored: null, oldKept: 'zh',
  });
  assert.equal(result.drafts.length, 2);
  assert(result.drafts.some(row => row.text === '新值'));
  assert(result.drafts.some(row => row.text === '尚未发送的草稿' && row.attachments[0].base64 === 'ZGVtbw=='));
  assert.deepEqual(result.afterReopen, [{ text: '新值' }]);
  assert.equal(result.agents[0].id, 'codex-legacy');
  assert.equal(result.absentLegacyCreated, false);
  console.log('PASS storage migration: preferences, session choice, drafts, attachments, key-path conversion, conflicts and repeat opens');
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}

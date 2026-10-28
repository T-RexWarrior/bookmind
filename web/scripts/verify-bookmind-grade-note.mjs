// Continue the dedicated live-verification textbook; do not grade a user's book.
import { chromium, expect } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
const out=new URL('../../data/native-live/',import.meta.url);
const report=JSON.parse(readFileSync(new URL('report.json',out),'utf8'));
const book=report.book, base='http://127.0.0.1:8018';
const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,args:['--no-proxy-server']});
const context=await browser.newContext({viewport:{width:1600,height:1000}}), page=await context.newPage();
async function waitFor(test,timeout=90000){const start=Date.now();while(Date.now()-start<timeout){const value=await test();if(value)return value;await new Promise(resolve=>setTimeout(resolve,600));}throw new Error('Verification did not converge');}
try {
  const sessions=await (await context.request.get(base+`/api/mastery-paths/topics/${book.path_id}/sessions`)).json();
  const session=sessions.sessions.find(item=>item.has_pending_question); assert(session);
  report.mastery_session_id=session.session_id;
  await page.goto(base+book.links.practice+'/sessions/'+session.session_id);
  await page.getByRole('button',{name:/只有一个孩子的结点属于叶结点/}).click();
  await page.getByRole('button',{name:'提交',exact:true}).last().click();
  const mistakes=await waitFor(async()=>{
    const result=await (await context.request.get(base+`/api/question-notebook/entries?mastery_path_id=${book.path_id}&mistakes_only=true`)).json();
    return result.total>0?result:null;
  });
  assert(mistakes.items.every(item=>item.mastery_path_id===book.path_id));
  report.checks.push({native_mastery_wrong_answer_recorded:mistakes.total});
  await expect(page.getByRole('button',{name:/^(发送|Send)$/}).last()).toBeVisible({timeout:90000});
  await page.screenshot({path:fileURLToPath(new URL('05-native-grading.png',out))});
  writeFileSync(new URL('report.json',out),JSON.stringify(report,null,2));

  await page.locator('button:has(svg.lucide-bookmark-plus)').first().click();
  const dialog=page.getByRole('dialog'); await dialog.waitFor();
  await dialog.locator('input[type=text],input:not([type])').first().fill('只有一个孩子的结点不是叶结点');
  await dialog.getByRole('button',{name:/保存/}).last().click();
  const saved=await waitFor(async()=>{
    const result=await (await context.request.get(base+`/api/bookmind/books/${book.id}`)).json();
    return result.note_count>0?result:null;
  });
  const notebook=await (await context.request.get(base+`/api/notebooks/${book.notebook_id}`)).json();
  const record=notebook.records.find(item=>item.title==='只有一个孩子的结点不是叶结点');
  assert(record?.metadata?.bookmind?.book_id===book.id);
  assert(record.metadata.bookmind.knowledge_point_id);
  report.checks.push({native_note_saved:saved.note_count,source_linked:true});
  await page.goto(base+book.links.notes);
  await page.getByRole('button',{name:/只有一个孩子的结点不是叶结点/}).first().click();
  await page.getByRole('link',{name:/返回教材/}).waitFor();
  await page.screenshot({path:fileURLToPath(new URL('06-linked-note.png',out))});
  await page.getByRole('link',{name:/返回教材/}).click();
  await page.locator('[data-reader-unit]').first().waitFor();
  report.checks.push('note_returns_to_native_reader');

  await page.goto(base+book.links.practice+'/mistakes');
  await expect(page.getByText('正在读取作答记录…')).toHaveCount(0);
  await expect(page.getByText(/某棵二叉树中有一个结点只有右孩子/).first()).toBeVisible({timeout:15000});
  await page.screenshot({path:fileURLToPath(new URL('07-native-mistake-card.png',out))});
  report.checks.push('native_mistake_card_visible');
} finally {writeFileSync(new URL('report.json',out),JSON.stringify(report,null,2));await browser.close();}
console.log(JSON.stringify({checks:report.checks,book_id:book.id}));

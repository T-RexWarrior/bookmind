// Manual live verification: uses the configured text API and a separate test book.
import { chromium, expect } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const base='http://127.0.0.1:8018';
const out=new URL('../../data/native-live/',import.meta.url);mkdirSync(out,{recursive:true});
const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,args:['--no-proxy-server']});
const context=await browser.newContext({viewport:{width:1600,height:1000}}), page=await context.newPage();
const report={checks:[],errors:[]};
page.on('pageerror',error=>report.errors.push(error.message));
const text='# 二叉树结构与空树\n\n二叉树的每个结点至多有两个孩子，分别称为左孩子和右孩子，左右孩子的位置不能随意交换。二叉树允许一个结点只有左孩子，也允许只有右孩子。没有任何结点的二叉树称为空树。没有孩子的结点称为叶结点。根结点是树的起点，非空二叉树只有一个根结点。结点可以使用 left 和 right 两个引用分别连接左右孩子，缺少的孩子用空引用表示。理解结点关系时，应区分结点的值与结点的位置。\n\n# 中序遍历\n\n中序遍历的顺序是：先遍历左子树，再访问根结点，最后遍历右子树。对每棵非空子树都重复这一规则，遇到空树时直接返回。若根结点的值为 2，左孩子的值为 1，右孩子的值为 3，则中序结果为 1、2、3。普通二叉树并不保证左边的值小于根、右边的值大于根，因此普通二叉树的中序结果不一定有序。只有额外满足二叉搜索树的大小关系时，中序遍历才产生有序结果。\n\n# 边界与自查\n\n实现递归遍历时，先检查当前结点是否为空；若为空则返回。对于只有一个结点的树，只访问该根结点一次。访问位置决定遍历类型：先序先访问根，中序在左右子树之间访问根，后序最后访问根。';
try {
  await page.goto(base+'/');
  await page.locator('input[type=file]').setInputFiles({name:'原版模块联调演示.md',mimeType:'text/markdown',buffer:Buffer.from(text)});
  await page.waitForURL(/\/learning\/reading\/rw_/,{timeout:30000});
  const books=await (await context.request.get(base+'/api/bookmind/books')).json();
  const book=books.find(item=>item.title.includes('原版模块联调演示'));
  assert(book); report.book_id=book.id; report.book=book;
  report.checks.push('upload_and_bind_native_modules');
  writeFileSync(new URL('report.json',out),JSON.stringify(report,null,2));

  await page.goto(base+book.links.learn+'?locator=2');
  await page.locator('[data-reader-unit]').first().waitFor();
  const composer=page.locator('[data-reading-composer]');
  await composer.locator('textarea').fill('请用一句话解释：为什么普通二叉树的中序遍历不一定有序？请给出教材原文位置。');
  await composer.getByRole('button',{name:/^(发送|Send)$/}).click();
  await expect(page.locator('[data-chat-message-role="assistant"]').last()).toContainText(/二叉|中序/,{timeout:120000});
  await expect(composer.getByRole('button',{name:/^(发送|Send)$/})).toBeVisible({timeout:120000});
  const answer=await page.locator('[data-chat-message-role="assistant"]').last().innerText();
  assert(answer.length>30); report.checks.push({native_reading_reply_chars:answer.length});
  await page.screenshot({path:fileURLToPath(new URL('01-reading-answer.png',out))});
  writeFileSync(new URL('report.json',out),JSON.stringify(report,null,2));

  const quizPromise=page.waitForResponse(response=>response.url().includes('/extensions/quiz')&&response.request().method()==='POST',{timeout:90000});
  await page.locator('.reading-v2 > header button:has(svg.lucide-list-checks)').click();
  const quizResponse=await quizPromise, quiz=await quizResponse.json();
  assert(quizResponse.ok(),JSON.stringify(quiz));
  const questions=quiz.payload.questions; assert.equal(questions.length,3);
  assert(questions.every(question=>!('correct_choice_index' in question)&&!('evidence' in question)));
  const fieldsets=page.locator('fieldset'); await expect(fieldsets).toHaveCount(3,{timeout:10000});
  for(let index=0;index<3;index++) {
    await fieldsets.nth(index).getByRole('button').first().click();
    await expect(fieldsets.nth(index).getByRole('status')).toBeVisible();
  }
  const refreshed=await (await context.request.get(base+'/api/bookmind/books/'+book.id)).json();
  const evidence=refreshed.objectives.reduce((sum,point)=>sum+point.evidence_count,0);
  assert.equal(evidence,3); report.checks.push({native_reading_quiz:3,shared_learning_evidence:evidence});
  await page.screenshot({path:fileURLToPath(new URL('02-shared-quiz.png',out))});
  writeFileSync(new URL('report.json',out),JSON.stringify(report,null,2));

  await page.goto(base+book.links.practice+'/mistakes');
  await page.getByRole('heading',{name:'本书错题'}).waitFor();
  await page.screenshot({path:fileURLToPath(new URL('03-mistakes.png',out))});
  report.checks.push('textbook_scoped_native_mistakes');

  await page.goto(base+book.links.practice+'/sessions');
  const masteryInput=page.locator('textarea').last(); await masteryInput.waitFor();
  await masteryInput.fill('请根据本教材直接针对“二叉树结构与空树”出一道可作答的选择题检查我的理解。请显示原版题卡，先不要给出答案，不要替我作答。');
  await page.getByRole('button',{name:/^(发送|Send)$/}).last().click();
  await page.waitForTimeout(1000);
  await expect(page.locator('[data-chat-message-role="assistant"]').last()).toContainText(/二叉|孩子|空树|题/,{timeout:120000});
  await expect(page.getByRole('button',{name:/^(发送|Send)$/}).last()).toBeVisible({timeout:120000});
  await page.screenshot({path:fileURLToPath(new URL('04-native-mastery.png',out))});
  report.checks.push('native_mastery_live_turn');
} finally {
  writeFileSync(new URL('report.json',out),JSON.stringify(report,null,2));
  await browser.close();
}
console.log(JSON.stringify({checks:report.checks,errors:report.errors,book_id:report.book_id}));

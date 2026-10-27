import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
const live = JSON.parse(
  readFileSync(
    new URL("../../data/live-verification.json", import.meta.url),
    "utf8",
  ),
);
const sid = live.reading_space_id;
const upload = live.checks.find(
  (c: { name: string }) => c.name === "upload_and_read",
);

test("阅读位置跨页面与刷新保留，图解可逐步播放", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(
    `/space/${sid}/learn?material=${upload.material_id}&page=${upload.page}`,
  );
  await expect(page.locator(".document-page")).toContainText(
    "三种深度优先遍历",
  );
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.locator(".document-page")).toContainText("层序遍历");
  await page.getByRole("link", { name: "练习", exact: true }).click();
  await expect(page.locator(".source-scope")).toContainText("第 4 节");
  await page.getByRole("link", { name: "学习", exact: true }).click();
  await expect(page.locator(".document-page")).toContainText("层序遍历");
  await page.reload();
  await expect(page.getByLabel("当前页码")).toHaveValue("4");
  await page.getByRole("button", { name: "上一页", exact: true }).click();
  await expect(page.locator(".document-page")).toContainText(
    "三种深度优先遍历",
  );
  await page.locator(".artifact-preview").first().click();
  await expect(page.locator(".step-description")).toContainText("STEP 01");
  await page.getByRole("button", { name: "下一步", exact: true }).click();
  await expect(page.locator(".step-description")).toContainText("STEP 02");
  await page.getByRole("button", { name: "播放", exact: true }).click();
  await expect(page.locator(".step-description")).toContainText("STEP 03", {
    timeout: 8000,
  });
  await page.getByRole("button", { name: "暂停", exact: true }).click();
  await page.getByRole("button", { name: "关闭图解", exact: true }).click();
  await expect(page.locator(".document-page")).toContainText(
    "三种深度优先遍历",
  );
  expect(errors).toEqual([]);
});

test("笔记新增、编辑、Markdown导出、删除与来源跳转", async ({ page }) => {
  await page.goto(`/space/${sid}/notes`);
  await expect(page.locator(".note-source-card")).toBeVisible();
  await page.locator(".note-source-card").click();
  await expect(page).toHaveURL(
    new RegExp(`learn\\?material=${upload.material_id}&page=${upload.page}`),
  );
  await expect(page.locator(".document-page")).toContainText(
    "三种深度优先遍历",
  );
  await page.getByRole("link", { name: /^笔记/ }).click();
  await page.getByRole("button", { name: "新建笔记", exact: true }).click();
  await page.getByLabel("笔记标题").fill("浏览器验收临时笔记");
  await page
    .getByLabel("笔记内容")
    .fill("## 我的理解\n\n中序的根，在左右子树之间。");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.locator(".note-content>h1")).toHaveText(
    "浏览器验收临时笔记",
  );
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await page
    .getByLabel("笔记内容")
    .fill("修改后：递归进入节点，不代表立刻访问节点。");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.locator(".note-content>.markdown")).toContainText("修改后");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出", exact: true }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("浏览器验收临时笔记.md");
  const path = await download.path();
  expect(readFileSync(path!, "utf8")).toContain("修改后");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除笔记", exact: true }).click();
  await expect(page.locator(".note-item-heading")).not.toContainText([
    "浏览器验收临时笔记",
  ]);
});

test("真实练习显示提交结果，未提交的题目隐藏解析", async ({
  page,
  request,
}) => {
  const quizzes = await (
    await request.get(`/api/spaces/${sid}/quizzes`)
  ).json();
  const quiz = quizzes.find((q: { id: string }) => q.id === live.quiz_id);
  const remaining = quiz.questions.find(
    (q: { attempt?: unknown }) => !q.attempt,
  );
  await page.goto(`/space/${sid}/practice`);
  await expect(page.locator(".question-card")).toHaveCount(3);
  await expect(page.locator(".question-card").first()).toContainText(
    "参考解析",
  );
  if (remaining) {
    const index = quiz.questions.findIndex(
      (q: { id: string }) => q.id === remaining.id,
    );
    const card = page.locator(".question-card").nth(index);
    await expect(card.locator(".answer-explanation")).toHaveCount(0);
    await card.locator(".question-option").first().click();
    await card.getByRole("button", { name: "提交答案" }).click();
    await expect(card.locator(".answer-explanation")).toBeVisible();
    await expect(card).toContainText("原文依据");
  }
  await expect(page.locator(".learning-evidence")).toContainText("状态仅依据");
});

test("API设置不回显密钥，实际连接测试并保存", async ({ page, request }) => {
  const config = await (await request.get("/api/settings")).json();
  expect(config.api_key).toBeUndefined();
  expect(config.has_api_key).toBeTruthy();
  await page.goto("/settings");
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "测试连接", exact: true }).click();
  await expect(page.locator(".connection-result")).toContainText(
    "连接测试通过",
    { timeout: 45000 },
  );
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(page.locator('.toast[role="status"]')).toContainText("已保存");
});

test("PDF原文画布、文字层、翻页和划选", async ({ page, request }) => {
  const existing = await (await request.get("/api/spaces")).json();
  const space =
    existing.find((s: { title: string }) => s.title === "PDF 阅读演示") ||
    (await (
      await request.post("/api/spaces", { data: { title: "PDF 阅读演示" } })
    ).json());
  const pdf = readFileSync(
    fileURLToPath(new URL("../../tests/fixtures/reading.pdf", import.meta.url)),
  );
  const mat = await (
    await request.post(`/api/spaces/${space.id}/materials`, {
      multipart: {
        file: {
          name: "PDF阅读示例.pdf",
          mimeType: "application/pdf",
          buffer: pdf,
        },
      },
    })
  ).json();
  await page.goto(`/space/${space.id}/learn?material=${mat.id}&page=1`);
  await expect(page.locator(".textLayer>span").first()).toBeVisible({
    timeout: 20000,
  });
  await expect(page.locator(".textLayer")).toContainText("Binary Tree");
  await page
    .locator(".textLayer>span")
    .first()
    .evaluate((el) => {
      const range = document.createRange();
      range.selectNodeContents(el);
      const selection = window.getSelection()!;
      selection.removeAllRanges();
      selection.addRange(range);
      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    });
  await expect(page.locator(".selection-bar")).toContainText("已选中");
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.locator(".textLayer")).toContainText("Inorder");
  await page.getByRole("button", { name: "文字视图", exact: true }).click();
  await expect(page.locator(".plain-source")).toContainText(
    "left, root, right",
  );
  // Leave the demonstration space usable, no private material was uploaded.
});

test("手机宽度下各页没有横向溢出", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of [
    "/",
    `/space/${sid}/learn`,
    `/space/${sid}/practice`,
    `/space/${sid}/notes`,
    "/settings",
  ]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
      path,
    ).toBeLessThanOrEqual(391);
  }
});

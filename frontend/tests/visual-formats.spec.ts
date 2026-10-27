import { test, expect } from "@playwright/test";
import { readFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
const live = JSON.parse(
  readFileSync(
    new URL("../../data/live-verification.json", import.meta.url),
    "utf8",
  ),
);

test("实际模型生成的Mermaid与SVG均可安全绘制", async ({ page, request }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`/space/${live.reading_space_id}/learn`);
  const out = new URL("../../docs/screenshots/", import.meta.url);
  mkdirSync(out, { recursive: true });
  for (const fmt of ["mermaid", "svg"]) {
    const aid = live[fmt + "_artifact_id"];
    expect(aid).toBeTruthy();
    const artifact = await (
      await request.get(`/api/spaces/${live.reading_space_id}/artifacts/${aid}`)
    ).json();
    await page
      .locator(".artifact-preview")
      .filter({ hasText: artifact.title })
      .click();
    await expect(page.locator(".diagram-page .svg-wrap svg")).toBeVisible({
      timeout: 25000,
    });
    await expect(page.locator(".diagram-page .svg-wrap")).toContainText(
      fmt === "mermaid" ? "中序" : "根节点",
    );
    await page.getByRole("button", { name: "放大图形", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "重置图形缩放", exact: true }),
    ).toHaveText("125%");
    await page
      .getByRole("button", { name: "重置图形缩放", exact: true })
      .click();
    await expect(page.locator(".diagram-page .error-inline")).toHaveCount(0);
    await expect(
      page.locator(".diagram-page script,.diagram-page foreignObject"),
    ).toHaveCount(0);
    await page.screenshot({
      path: fileURLToPath(new URL(`03-${fmt}.png`, out)),
    });
  }
  expect(errors).toEqual([]);
});

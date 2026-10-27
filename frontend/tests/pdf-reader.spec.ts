import { test, expect } from "@playwright/test";

async function smallPdf(request: import("@playwright/test").APIRequestContext) {
  const spaces = await (await request.get("/api/spaces")).json();
  const space = spaces.find(
    (s: { title: string }) => s.title === "PDF 阅读演示",
  );
  const detail = await (await request.get(`/api/spaces/${space.id}`)).json();
  return {
    space: detail,
    material: detail.materials.find((m: { kind: string }) => m.kind === "pdf"),
  };
}

test("读取新页文字时不移除原版画布或重复下载文件", async ({
  page,
  request,
}) => {
  const { space, material } = await smallPdf(request);
  const downloads: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/files/") && !r.headers()["range"])
      downloads.push(r.url());
  });
  try {
    await page.goto(`/space/${space.id}/learn?material=${material.id}&page=1`);
    await expect(page.locator(".textLayer")).toContainText("Binary Tree");
    await page
      .locator(".pdf-standard canvas")
      .evaluate((el) => el.setAttribute("data-reader-retained", "yes"));
    await page.route(
      `**/api/spaces/${space.id}/materials/${material.id}/pages/2`,
      async (route) => {
        await new Promise((resolve) => setTimeout(resolve, 350));
        await route.continue();
      },
    );
    await page.getByRole("button", { name: "下一页", exact: true }).click();
    await expect(page.locator(".pdf-standard canvas")).toHaveAttribute(
      "data-reader-retained",
      "yes",
    );
    await expect(page.locator(".textLayer")).toContainText("Inorder");
    await expect(page.locator(".pdf-display-bar")).toContainText("标准显示");
    expect(downloads).toHaveLength(1);
  } finally {
    await request.put(`/api/spaces/${space.id}/focus`, {
      data: { material_id: material.id, page: space.focus_page },
    });
  }
});

test("文字选区失败时保留已绘制页面并使用原文坐标选区", async ({
  page,
  request,
}) => {
  const { space, material } = await smallPdf(request);
  await page.addInitScript(() => {
    const original = CSSStyleDeclaration.prototype.setProperty;
    CSSStyleDeclaration.prototype.setProperty = function (
      name,
      value,
      priority,
    ) {
      if (name === "--min-font-size")
        throw new Error("Injected PDF text-layer failure");
      return original.call(this, name, value, priority);
    };
  });
  try {
    await page.goto(`/space/${space.id}/learn?material=${material.id}&page=1`);
    await expect(page.locator(".pdf-display-bar")).toContainText(
      "兼容文字选区",
    );
    await expect(page.locator(".pdf-standard canvas")).toBeVisible();
    await expect(page.locator(".pdf-reader .error-inline")).toHaveCount(0);
    const text = page.locator(".pdf-server-text text").first();
    await expect(text).toContainText("Binary Tree");
    await text.evaluate((el) => {
      const range = document.createRange();
      range.selectNodeContents(el);
      const selection = window.getSelection()!;
      selection.removeAllRanges();
      selection.addRange(range);
      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    });
    await expect(page.locator(".selection-bar")).toContainText("已选中");
  } finally {
    await request.put(`/api/spaces/${space.id}/focus`, {
      data: { material_id: material.id, page: space.focus_page },
    });
  }
});

test("画布失败时自动显示兼容页面，刷新后保留可用模式", async ({
  page,
  request,
}) => {
  const { space, material } = await smallPdf(request);
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (
      ...args: Parameters<typeof original>
    ) {
      if (this.closest(".pdf-standard"))
        throw new Error("Injected PDF canvas failure");
      return original.apply(this, args);
    } as typeof original;
  });
  try {
    await page.goto(`/space/${space.id}/learn?material=${material.id}&page=1`);
    const image = page.locator(".pdf-compatible img");
    await expect(image).toBeVisible();
    await expect
      .poll(() =>
        image.evaluate(
          (el: HTMLImageElement) => el.complete && el.naturalWidth > 0,
        ),
      )
      .toBe(true);
    await expect(page.locator(".pdf-reader .error-inline")).toHaveCount(0);
    await page.reload();
    await expect(page.locator(".pdf-display-bar")).toContainText("兼容显示");
    await expect
      .poll(() =>
        image.evaluate(
          (el: HTMLImageElement) => el.complete && el.naturalWidth > 0,
        ),
      )
      .toBe(true);
  } finally {
    await request.put(`/api/spaces/${space.id}/focus`, {
      data: { material_id: material.id, page: space.focus_page },
    });
  }
});

test("669页真实教材按页显示，封面、正文、跳页和划选均可使用", async ({
  page,
  request,
}) => {
  const spaces = await (await request.get("/api/spaces")).json();
  const target = spaces.find(
    (s: { title: string }) => s.title === "dsacpp-3rd-edn",
  );
  test.skip(
    !target,
    "The user-provided textbook is kept only in local data, not in the repository.",
  );
  const space = await (await request.get(`/api/spaces/${target.id}`)).json();
  const material = space.materials.find(
    (m: { kind: string }) => m.kind === "pdf",
  );
  const rawDownloads: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/files/")) rawDownloads.push(r.url());
  });
  try {
    await page.goto(`/space/${space.id}/learn?material=${material.id}&page=1`);
    await expect(page.locator(".pdf-display-bar")).toContainText("兼容显示");
    const image = page.locator(".pdf-compatible img");
    await expect
      .poll(() =>
        image.evaluate(
          (el: HTMLImageElement) => el.complete && el.naturalWidth > 0,
        ),
      )
      .toBe(true);
    for (const number of [26, 100, 669]) {
      await page.getByLabel("当前页码").fill(String(number));
      await page.getByLabel("当前页码").press("Enter");
      await expect(image).toHaveAttribute("alt", `教材原版第 ${number} 页`);
      await expect
        .poll(() =>
          image.evaluate(
            (el: HTMLImageElement) => el.complete && el.naturalWidth > 0,
          ),
        )
        .toBe(true);
      await expect(page.locator(".pdf-reader .error-inline")).toHaveCount(0);
      if (number === 26) {
        const text = page
          .locator(".pdf-server-text text")
          .filter({ hasText: "起泡排序" })
          .first();
        await expect(text).toBeVisible();
        await text.evaluate((el) => {
          const range = document.createRange();
          range.selectNodeContents(el);
          const sel = window.getSelection()!;
          sel.removeAllRanges();
          sel.addRange(range);
          el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
        });
        await expect(page.locator(".selection-bar")).toContainText("起泡排序");
        const content = await (
          await request.get(
            `/api/spaces/${space.id}/materials/${material.id}/pages/26`,
          )
        ).json();
        expect(content.text).toContain("起泡排序");
      }
    }
    expect(rawDownloads).toEqual([]);
  } finally {
    await request.put(`/api/spaces/${space.id}/focus`, {
      data: { material_id: material.id, page: space.focus_page },
    });
  }
});

import { chromium } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
  args: ["--no-proxy-server"],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 960 },
});
const spaces = await (
  await context.request.get("http://127.0.0.1:8018/api/spaces")
).json();
const space = spaces.find((s) => s.title.includes(process.argv[2] || "dsacpp"));
if (!space) throw new Error("Target learning space not found");
const detail = await (
  await context.request.get(`http://127.0.0.1:8018/api/spaces/${space.id}`)
).json();
const mat = detail.materials.find((m) => m.kind === "pdf");
const events = [];
const page = await context.newPage();
page.on("pageerror", (e) =>
  events.push({ type: "pageerror", message: e.message }),
);
page.on("console", (m) => {
  if (["error", "warning"].includes(m.type()))
    events.push({ type: m.type(), message: m.text().slice(0, 1000) });
});
page.on("requestfailed", (r) =>
  events.push({
    type: "requestfailed",
    url: r.url(),
    error: r.failure()?.errorText,
  }),
);
page.on("response", (r) => {
  if (
    r.url().includes("/files/") ||
    r.url().includes("/pdf/") ||
    /\/pages\/\d+\/(image|layout)/.test(r.url())
  )
    events.push({
      type: "response",
      url: r.url(),
      status: r.status(),
      range: r.request().headers()["range"],
    });
});
const out = new URL("../../data/pdf-diagnosis/", import.meta.url);
mkdirSync(out, { recursive: true });
try {
  for (const number of [26, 1, 20, 100]) {
    await page.goto(
      `http://127.0.0.1:8018/space/${space.id}/learn?material=${mat.id}&page=${number}`,
    );
    await page.locator(".pdf-reader").waitFor();
    await page.waitForLoadState('networkidle');
    await page
      .waitForFunction(
        () => {
          const box = document.querySelector(".pdf-reader");
          const image = box?.querySelector(".pdf-compatible img");
          return (
            !!box?.querySelector(".error-inline") ||
            (((image?.complete && image?.naturalWidth > 0) ||
              !!box?.querySelector("canvas")?.width) &&
              !box?.querySelector(".pdf-loading"))
          );
        },
        {},
        { timeout: 45000 },
      )
      .catch((e) =>
        events.push({ type: "timeout", message: e.message.slice(0, 200) }),
      );
    events.push({
      type: "state",
      page: number,
      data: await page.locator(".pdf-reader").evaluate((el) => ({
        error: el.querySelector(".error-inline")?.textContent || "",
        canvas: [
          el.querySelector("canvas")?.width,
          el.querySelector("canvas")?.height,
        ],
        textSpans: el.querySelectorAll(".textLayer span").length,
        compatibleImage: [
          el.querySelector(".pdf-compatible img")?.naturalWidth,
          el.querySelector(".pdf-compatible img")?.naturalHeight,
        ],
        originalTextSpans: el.querySelectorAll(".pdf-server-text text").length,
      })),
    });
    await page.screenshot({
      path: fileURLToPath(new URL(`page-${number}.png`, out)),
    });
  const spans = page.locator('.pdf-server-text text');
  if (number === 26 && await spans.count() > 6) {
    const selected = await page.evaluate(() => {
      const items = document.querySelectorAll('.pdf-server-text text');
      const range = document.createRange();
      range.setStart(items[3].firstChild, 0);
      range.setEnd(items[6].firstChild, items[6].textContent.length);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      return selection.toString();
    });
    const original = await (await context.request.get(`http://127.0.0.1:8018/api/spaces/${space.id}/materials/${mat.id}/pages/${number}`)).json();
    events.push({type:'selection-check',characters:selected.length,whitespaceInsensitiveMatch:original.text.replace(/\s/g,'').includes(selected.replace(/\s/g,''))});
  }
  }
} finally {
  await context.request.put(
    `http://127.0.0.1:8018/api/spaces/${space.id}/focus`,
    { data: { material_id: mat.id, page: space.focus_page } },
  );
}
writeFileSync(
  new URL("events.json", out),
  JSON.stringify({ space_id: space.id, material_id: mat.id, events }, null, 2),
);
console.log(JSON.stringify(events.filter((e) => e.type !== "response")));
await browser.close();

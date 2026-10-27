import { cpSync, existsSync, mkdirSync } from "node:fs";
for (const dir of ["cmaps", "standard_fonts", "wasm"]) {
  const source = new URL(`../node_modules/pdfjs-dist/${dir}`, import.meta.url);
  const target = new URL(`../public/pdf/${dir}`, import.meta.url);
  if (existsSync(source)) {
    mkdirSync(target, { recursive: true });
    cpSync(source, target, { recursive: true });
  }
}

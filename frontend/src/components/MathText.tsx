import type { ReactNode } from "react";
import katex from "katex";
import "katex/dist/katex.min.css";

/** Render a safe, intentionally small Markdown subset from model output. */
export function MathText({ text }: { text: string }) {
  const lines = (text || "").replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    if (line.trimStart().startsWith("```")) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trimStart().startsWith("```")) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      blocks.push(<pre className="md-code" key={`code-${index}`}><code>{code.join("\n")}</code></pre>);
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const Tag = heading[1].length === 1 ? "h3" : heading[1].length === 2 ? "h4" : "h5";
      blocks.push(<Tag className="md-heading" key={`heading-${index}`}>{inline(heading[2], index)}</Tag>);
      index += 1;
      continue;
    }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) { blocks.push(<hr className="md-rule" key={`rule-${index}`} />); index += 1; continue; }
    if (/^>\s?/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) quote.push(lines[index++].replace(/^>\s?/, ""));
      blocks.push(<blockquote className="md-quote" key={`quote-${index}`}>{inline(quote.join("\n"), index)}</blockquote>);
      continue;
    }
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      const isOrdered = Boolean(ordered);
      const items: string[] = [];
      const pattern = isOrdered ? /^\s*\d+[.)]\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/;
      while (index < lines.length) {
        const item = lines[index].match(pattern);
        if (!item) break;
        items.push(item[1]);
        index += 1;
      }
      const List = isOrdered ? "ol" : "ul";
      blocks.push(<List className="md-list" key={`list-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{inline(item, itemIndex)}</li>)}</List>);
      continue;
    }
    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim()) {
      const candidate = lines[index];
      if (paragraph.length && (/^(#{1,3})\s+/.test(candidate) || candidate.trimStart().startsWith("```") || /^>\s?/.test(candidate) || /^\s*[-*+]\s+/.test(candidate) || /^\s*\d+[.)]\s+/.test(candidate))) break;
      paragraph.push(candidate);
      index += 1;
    }
    blocks.push(<p className="md-paragraph" key={`paragraph-${index}`}>{inline(paragraph.join("\n"), index)}</p>);
  }
  return <>{blocks}</>;
}

function inline(value: string, keySeed: number): ReactNode[] {
  const parts = value.split(/(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^$\n]+?\$|`[^`]+`|\*\*[^*]+\*\*)/g);
  return parts.map((part, index) => {
    const key = `${keySeed}-${index}`;
    const display = part.startsWith("$$") || part.startsWith("\\[");
    const math = display || part.startsWith("\\(") || part.startsWith("$");
    if (math) {
      const source = part.startsWith("$$") ? part.slice(2, -2)
        : part.startsWith("\\[") || part.startsWith("\\(") ? part.slice(2, -2) : part.slice(1, -1);
      try {
        return <span key={key} className={display ? "math-display" : "math-inline"} dangerouslySetInnerHTML={{ __html: katex.renderToString(source, { displayMode: display, throwOnError: true }) }} />;
      } catch { return <span key={key}>{part}</span>; }
    }
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={key}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code className="md-inline-code" key={key}>{part.slice(1, -1)}</code>;
    return <span key={key}>{part}</span>;
  });
}

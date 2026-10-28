/** Keep old center-coordinate node labels working without overriding SVG baselines. */
export function preserveCenteredSvgLabels(root: Element): void {
  // An authored stylesheet owns its text layout; do not add a presentation
  // attribute that could override inherited author rules.
  if (Array.from(root.querySelectorAll('style')).some(style => /(?:dominant|alignment)-baseline\s*:/i.test(style.textContent ?? ''))) return;
  const number = (element: Element, name: string): number => {
    const value = element.getAttribute(name) ?? '';
    return /^-?(?:\d+\.?\d*|\.\d+)$/.test(value) ? Number(value) : NaN;
  };
  for (const text of root.querySelectorAll('text')) {
    if (text.children.length || text.hasAttribute('transform') || text.getAttribute('text-anchor') !== 'middle') continue;
    let explicitBaseline = false;
    for (let ancestor: Element | null = text; ancestor; ancestor = ancestor.parentElement) {
      if (ancestor.hasAttribute('dominant-baseline') || ancestor.hasAttribute('alignment-baseline') ||
        /(?:dominant|alignment)-baseline\s*:/i.test(ancestor.getAttribute('style') ?? '')) explicitBaseline = true;
      if (ancestor === root) break;
    }
    if (explicitBaseline) continue;
    const x = number(text, 'x'), y = number(text, 'y');
    const shape = Array.from(text.parentElement?.children ?? []).find(element => {
      if (element.hasAttribute('transform')) return false;
      const tag = element.localName;
      const cx = tag === 'rect' ? number(element, 'x') + number(element, 'width') / 2 : number(element, 'cx');
      const cy = tag === 'rect' ? number(element, 'y') + number(element, 'height') / 2 : number(element, 'cy');
      return ['circle', 'ellipse', 'rect'].includes(tag) && Math.abs(x - cx) < 0.01 && Math.abs(y - cy) < 0.01;
    });
    // Only the unambiguous legacy convention (x/y exactly at a shape's
    // center) needs a default. A y+offset label keeps standard SVG behavior.
    if (shape) text.setAttribute('dominant-baseline', 'central');
  }
}

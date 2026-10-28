import { describe, expect, it } from 'vitest';
import { preserveCenteredSvgLabels } from '@/lib/svg-text-alignment';

function parse(content: string) {
  return new DOMParser().parseFromString(`<svg xmlns="http://www.w3.org/2000/svg">${content}</svg>`, 'image/svg+xml').documentElement;
}

describe('SVG label baseline compatibility', () => {
  it('keeps an authored alphabetic-baseline offset instead of centering it twice', () => {
    const root = parse('<circle cx="250" cy="70" r="22"/><text x="250" y="77" text-anchor="middle">A</text>');
    preserveCenteredSvgLabels(root);
    expect(root.querySelector('text')?.getAttribute('y')).toBe('77');
    expect(root.querySelector('text')?.hasAttribute('dominant-baseline')).toBe(false);
  });

  it('preserves older center-coordinate labels after removing the blanket CSS override', () => {
    const root = parse('<circle cx="250" cy="70" r="22"/><text x="250" y="70" text-anchor="middle">A</text>');
    preserveCenteredSvgLabels(root);
    expect(root.querySelector('text')?.getAttribute('dominant-baseline')).toBe('central');
  });

  it.each([
    '<text x="20" y="20" text-anchor="middle" dominant-baseline="alphabetic">A</text>',
    '<g dominant-baseline="alphabetic"><circle cx="20" cy="20" r="10"/><text x="20" y="20" text-anchor="middle">A</text></g>',
    '<text x="20" y="20" text-anchor="middle" style="dominant-baseline:middle">A</text>',
    '<text x="20" y="20" text-anchor="middle"><tspan x="20" dy="1em">A</tspan></text>',
    '<text x="20" y="20" transform="translate(0 5)" text-anchor="middle">A</text>',
  ])('does not override explicit, inherited, multiline or transformed layout', markup => {
    const root = parse('<circle cx="20" cy="20" r="10"/>' + markup);
    const before = root.outerHTML;
    preserveCenteredSvgLabels(root);
    expect(root.outerHTML).toBe(before);
  });

  it('leaves an authored stylesheet in control of its baseline rules', () => {
    const root = parse('<style>text {dominant-baseline:alphabetic}</style><circle cx="20" cy="20" r="10"/><text x="20" y="20" text-anchor="middle">A</text>');
    preserveCenteredSvgLabels(root);
    expect(root.querySelector('text')?.hasAttribute('dominant-baseline')).toBe(false);
  });
});

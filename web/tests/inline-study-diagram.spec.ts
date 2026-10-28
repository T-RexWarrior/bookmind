import { describe, expect, it } from 'vitest';
import { extractInlineVisualizeResult } from '@/lib/visualize-types';
import { compactTracePreview } from '@/features/chat/trace/memory';
import type { StreamEvent } from '@/features/chat/model/protocol';

const diagram = (title = '二叉树') => ({
  type: 'tool_result', metadata: { visualization_submitted: true, bookmind_diagram: {
    schema_version: 'deeptutor.visualization/v1', render_type: 'svg',
    payload: { format: 'image/svg+xml', data: '<svg viewBox="0 0 300 200"><title>二叉树</title></svg>' },
    presentation: { title },
  } },
});

describe('native diagrams inside ordinary study messages', () => {
  it('restores the validated canvas from saved tool results, without requiring visualize mode', () => {
    const restored = extractInlineVisualizeResult(JSON.parse(JSON.stringify([
      { type: 'tool_call', metadata: { tool_name: 'read_material' } },
      { type: 'tool_result', metadata: { tool_name: 'submit_visualization', tool_metadata: diagram().metadata } },
      { type: 'result', metadata: { response: '根结点位于顶部。', engine: 'agent_loop' } },
    ])));
    expect(restored?.render_type).toBe('svg');
    expect(restored && 'payload' in restored && restored.payload.data).toContain('<svg');
  });

  it('does not show a failed submission, a call argument or an unrelated tool result as a canvas', () => {
    const event = diagram();
    expect(extractInlineVisualizeResult([{ ...event, type: 'tool_call' }])).toBeNull();
    expect(extractInlineVisualizeResult([{ ...event, metadata: { ...event.metadata, visualization_submitted: false } }])).toBeNull();
    expect(extractInlineVisualizeResult([{ type: 'tool_result', metadata: { content: '<svg/>' } }])).toBeNull();
  });

  it('keeps one latest accepted diagram when the model revises it', () => {
    const result = extractInlineVisualizeResult([diagram('草图'), diagram('完成图')]);
    expect(result && 'presentation' in result && result.presentation.title).toBe('完成图');
  });

  it('keeps the diagram when older trace rows are compacted out', () => {
    const events = [diagram(), ...Array.from({ length: 30 }, () => ({ type: 'tool_result', metadata: {} })),
      { type: 'result', metadata: {} }, { type: 'done', metadata: {} }] as StreamEvent[];
    const preview = compactTracePreview(events, 3);
    expect(preview.truncated).toBe(true);
    expect(extractInlineVisualizeResult(preview.events)?.render_type).toBe('svg');
  });
});

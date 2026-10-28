import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useReadingWorkspace } from '@/components/reading/workspace/useReadingWorkspace';
import { isReadingSessionReady, readingSessionScope } from '@/lib/reading-study-route';

const mocks = vi.hoisted(() => ({
  router: { replace: vi.fn(), push: vi.fn() },
  chat: {
    state: { sessionKey: 'practice', sessionId: 'practice' as string | null,
      workspaceMode: null as string | null, readingWorkspaceId: null as string | null },
    newSession: vi.fn(() => 'reading-draft'), configureSession: vi.fn(),
    loadSession: vi.fn<(id: string, options: { signal: AbortSignal }) => Promise<void>>(),
  },
  reader: { material: null, annotations: [], openMaterial: vi.fn(), closeMaterial: vi.fn(), reportViewport: vi.fn() },
  getWorkspace: vi.fn(), listConversations: vi.fn(), t: (value: string) => value,
}));

vi.mock('next/navigation', () => ({ useRouter: () => mocks.router }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: mocks.t }) }));
vi.mock('@/features/chat/ChatStateAdapter', () => ({ useChatStateAdapter: () => mocks.chat }));
vi.mock('@/context/ReadingContext', () => ({ useReading: () => mocks.reader }));
vi.mock('@/lib/reading-workspace-api', () => ({
  getReadingWorkspace: mocks.getWorkspace, listReadingConversations: mocks.listConversations,
}));
vi.mock('@/lib/reading-turn-state', () => ({ setReadingWorkspace: vi.fn() }));
vi.mock('@/lib/reading-api', () => ({ listBookmarks: vi.fn(), getReadingTranscript: vi.fn() }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, '', '/learning/reading/book');
  mocks.chat.state = { sessionKey: 'practice', sessionId: 'practice', workspaceMode: null, readingWorkspaceId: null };
  mocks.getWorkspace.mockImplementation(async (id: string) => ({ workspace: { workspace_id: id, tabs: [] } }));
  mocks.listConversations.mockResolvedValue([]);
  mocks.chat.loadSession.mockResolvedValue(undefined);
});

describe('reading conversation isolation', () => {
  it('never promotes the previous practice session while the book or new draft is loading', async () => {
    const hydration = deferred<{ workspace: { workspace_id: string; tabs: [] } }>();
    mocks.getWorkspace.mockReturnValue(hydration.promise);
    const replace = vi.spyOn(window.history, 'replaceState');
    const { result, rerender } = renderHook(() => useReadingWorkspace('book', null));
    expect(result.current.sessionLoading).toBe(true);
    expect(replace).not.toHaveBeenCalled();
    await act(async () => { hydration.resolve({ workspace: { workspace_id: 'book', tabs: [] } }); });
    await waitFor(() => expect(mocks.chat.newSession).toHaveBeenCalledOnce());
    expect(result.current.sessionLoading).toBe(true);
    expect(replace).not.toHaveBeenCalled();
    expect(mocks.chat.newSession).toHaveBeenCalledWith(expect.objectContaining({
      workspaceMode: 'immersive_reading', readingWorkspaceId: 'book', masteryPathId: null,
    }));

    mocks.chat.state = { sessionKey: 'reading-draft', sessionId: null,
      workspaceMode: 'immersive_reading', readingWorkspaceId: 'book' };
    rerender();
    expect(result.current.sessionLoading).toBe(false);
    expect(replace).not.toHaveBeenCalled();
    mocks.chat.state = { ...mocks.chat.state, sessionKey: 'new-reading', sessionId: 'new-reading' };
    rerender();
    await waitFor(() => expect(replace).toHaveBeenCalledWith(null, '', '/learning/reading/book/sessions/new-reading'));
    expect(replace.mock.calls.some(call => String(call[2]).includes('/practice'))).toBe(false);
  });

  it.each(['practice', 'guidance'])('recovers a saved reading URL naming a %s session without loading its transcript', async invalidId => {
    window.history.replaceState(null, '', `/learning/reading/book/sessions/${invalidId}?course=course-1&locator=7`);
    mocks.listConversations.mockResolvedValue([{ session_id: 'valid-reading' }]);
    const { result } = renderHook(() => useReadingWorkspace('book', invalidId, 'course-1'));
    await waitFor(() => expect(mocks.router.replace).toHaveBeenCalledWith(
      '/learning/reading/book/sessions/valid-reading?course=course-1&locator=7', { scroll: false },
    ));
    expect(mocks.chat.loadSession).not.toHaveBeenCalled();
    expect(mocks.chat.configureSession).not.toHaveBeenCalled();
    expect(result.current.sessionLoading).toBe(true);
  });

  it('cancels an old reading load before opening another textbook', async () => {
    mocks.listConversations.mockResolvedValue([{ session_id: 'old-reading' }]);
    const pending = deferred<void>();
    mocks.chat.loadSession.mockReturnValue(pending.promise);
    const { result, rerender } = renderHook(({ book, session }: { book: string; session: string | null }) =>
      useReadingWorkspace(book, session), { initialProps: { book: 'book', session: 'old-reading' as string | null } });
    await waitFor(() => expect(mocks.chat.loadSession).toHaveBeenCalledOnce());
    const signal = mocks.chat.loadSession.mock.calls[0][1].signal as AbortSignal;
    rerender({ book: 'other-book', session: null });
    expect(signal.aborted).toBe(true);
    expect(result.current.sessionLoading).toBe(true);
    await act(async () => { pending.resolve(); });
    await waitFor(() => expect(mocks.chat.newSession).toHaveBeenCalledWith(expect.objectContaining({ readingWorkspaceId: 'other-book' })));
    expect(mocks.chat.configureSession).not.toHaveBeenCalled();
  });

  it('only shows a named conversation after its ownership and selected state both match', async () => {
    mocks.listConversations.mockResolvedValue([{ session_id: 'reading' }]);
    const { result, rerender } = renderHook(() => useReadingWorkspace('book', 'reading'));
    await waitFor(() => expect(mocks.chat.configureSession).toHaveBeenCalledWith(expect.objectContaining({
      workspaceMode: 'immersive_reading', readingWorkspaceId: 'book', masteryPathId: null,
    }), 'reading'));
    expect(result.current.sessionLoading).toBe(true);
    mocks.chat.state = { sessionKey: 'reading', sessionId: 'reading', workspaceMode: 'immersive_reading', readingWorkspaceId: 'book' };
    rerender();
    expect(result.current.sessionLoading).toBe(false);
  });

  it('rejects another book, course or the previous mode even when a session id exists', () => {
    const scope = readingSessionScope('book', 'course');
    const input = { binding: { scope, draftKey: 'draft', previousSessionId: 'practice' }, scope,
      routeSessionId: null, workspaceId: 'book', state: { sessionKey: 'new-reading', sessionId: 'new-reading',
        workspaceMode: 'immersive_reading', readingWorkspaceId: 'book' } };
    expect(isReadingSessionReady(input)).toBe(true);
    expect(isReadingSessionReady({ ...input, state: { ...input.state, readingWorkspaceId: 'other-book' } })).toBe(false);
    expect(isReadingSessionReady({ ...input, scope: readingSessionScope('book', 'other-course') })).toBe(false);
    expect(isReadingSessionReady({ ...input, state: { ...input.state, workspaceMode: 'mastery_path' } })).toBe(false);
    expect(isReadingSessionReady({ ...input, state: { ...input.state, sessionKey: 'practice', sessionId: 'practice' } })).toBe(false);
  });
});

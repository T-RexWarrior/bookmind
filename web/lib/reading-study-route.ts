export interface ReadingSessionBinding {
  scope: string;
  draftKey?: string;
  previousSessionId?: string | null;
  sessionId?: string;
}

export function readingSessionScope(workspaceId: string, courseId = ''): string {
  return JSON.stringify([workspaceId, courseId.trim()]);
}

/** A route never inherits the session selected on the previous screen. */
export function isReadingSessionReady(input: {
  binding: ReadingSessionBinding | null;
  scope: string;
  routeSessionId: string | null;
  workspaceId: string;
  state: {
    sessionKey: string;
    sessionId: string | null;
    workspaceMode: string | null;
    readingWorkspaceId?: string | null;
  };
}): boolean {
  const { binding, state } = input;
  if (!binding || binding.scope !== input.scope ||
    state.workspaceMode !== 'immersive_reading' || state.readingWorkspaceId !== input.workspaceId) return false;
  if (binding.sessionId) return input.routeSessionId === binding.sessionId && state.sessionId === binding.sessionId;
  if (!binding.draftKey) return false;
  // BIND_SERVER_SESSION promotes the local draft key to the server id. Only
  // that newly-created reading session may update a previously bare URL.
  const promoted = Boolean(state.sessionId && state.sessionId !== binding.previousSessionId);
  if (input.routeSessionId) return promoted && state.sessionId === input.routeSessionId;
  return state.sessionKey === binding.draftKey || promoted;
}

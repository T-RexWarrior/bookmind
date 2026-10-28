"use client";

import { readingCollectionRoute, readingSessionRoute } from "@/lib/learning-routes";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { useReading } from "@/context/ReadingContext";
import { useChatStateAdapter } from "@/features/chat/ChatStateAdapter";
import { courseSessionConfiguration } from "@/lib/course-session-scope";
import {
  addBookmark,
  deleteBookmark,
  getReadingTranscript,
  listBookmarks,
  type ReadingBookmark,
} from "@/lib/reading-api";
import {
  READER_ACTION_EVENT,
  type ReaderActionPayload,
} from "@/lib/reading-reader-action";
import { setReadingWorkspace } from "@/lib/reading-turn-state";
import { READING_WORKSPACE_MODE } from "@/lib/workspace-mode";
import { isReadingSessionReady, readingSessionScope, type ReadingSessionBinding } from "@/lib/reading-study-route";
import {
  activateReadingMaterial,
  generateMasteryPathFromReading,
  getReadingWorkspace,
  listReadingConversations,
  organizeReadingNotes,
  removeReadingWorkspaceMaterial,
  updateReadingWorkspace,
  type OrganizedReadingNotes,
  type ReadingConversation,
  type ReadingLibraryMaterial,
  type ReadingWorkspace,
} from "@/lib/reading-workspace-api";
import type { TranscriptRow } from "./types";

/**
 * Everything the reading workspace needs from the network, in one place.
 *
 * The page component owns only what the reader touches directly — selection,
 * composer text, which panels are open. Hydration, polling, conversation
 * bootstrapping and the source lifecycle live here so the view stays readable
 * and each effect has one obvious owner.
 */
/** Stable empty list, so a material with no bookmarks is not a new array
 *  identity on every render. */
const NO_BOOKMARKS: ReadingBookmark[] = [];

export function useReadingWorkspace(
  workspaceId: string,
  sessionIdParam: string | null,
  courseId = "",
) {
  const router = useRouter();
  const { t } = useTranslation();
  const { material, annotations, openMaterial, closeMaterial, reportViewport } =
    useReading();
  const {
    state,
    configureSession,
    loadSession,
    newSession,
  } = useChatStateAdapter();

  const [workspace, setWorkspace] = useState<ReadingWorkspace | null>(null);
  const [conversations, setConversations] = useState<ReadingConversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [activeLocator, setActiveLocator] = useState(1);
  const [transcript, setTranscript] = useState<TranscriptRow[]>([]);
  const [organizedNotes, setOrganizedNotes] =
    useState<OrganizedReadingNotes | null>(null);
  const sessionBootRef = useRef("");
  const sessionLoadRef = useRef<AbortController | null>(null);
  const workspaceRequestRef = useRef(0);
  const [sessionBinding, setSessionBinding] = useState<ReadingSessionBinding | null>(null);
  const [sessionFailure, setSessionFailure] = useState<{ routeKey: string; message: string } | null>(null);
  const sessionScope = readingSessionScope(workspaceId, courseId);
  const sessionRouteKey = JSON.stringify([sessionScope, sessionIdParam]);
  const transcriptRequestRef = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++workspaceRequestRef.current;
    const [result, sessionRows] = await Promise.all([getReadingWorkspace(workspaceId), listReadingConversations(workspaceId)]);
    if (requestId === workspaceRequestRef.current) {
      setWorkspace(result.workspace);
      setConversations(sessionRows);
    }
    return { workspace: result.workspace, sessions: sessionRows };
  }, [workspaceId]);

  useEffect(() => {
    let cancelled = false;
    // The async refresh owns the initial network hydration for this route.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh()
      .catch((caught) => {
        if (!cancelled)
          setError(
            caught instanceof Error
              ? caught.message
              : t("This collection could not be opened."),
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      workspaceRequestRef.current += 1;
    };
  }, [refresh, t]);

  useEffect(() => {
    setReadingWorkspace(workspaceId);
    return () => {
      setReadingWorkspace(null);
      closeMaterial();
    };
  }, [closeMaterial, workspaceId]);

  const sessionConfiguration = useMemo(
    () => ({ workspaceMode: READING_WORKSPACE_MODE, readingWorkspaceId: workspaceId,
      masteryPathId: null, masterySessionMode: null }),
    [workspaceId],
  );

  const activeTab = useMemo(
    () =>
      workspace?.tabs.find(
        (tab) => tab.material.material_id === workspace.active_material_id,
      ) ??
      workspace?.tabs[0] ??
      null,
    [workspace],
  );

  useEffect(() => {
    const active = activeTab?.material;
    if (!active || active.status !== "ready") {
      closeMaterial();
      return;
    }
    // Let ReadingContext own the request from its first tick. Prefetching the
    // detail here left `loading` false until that fetch completed, so the
    // reader briefly rendered its unavailable state between workspace and
    // material hydration (most visibly in Safari).
    void openMaterial(active.material_id);
  }, [activeTab?.material, closeMaterial, openMaterial]);

  // Poll while anything is still being processed, backing off as the wait
  // grows. A flat 2.5s forever means a source wedged in "processing" quietly
  // hammers the API for as long as the tab stays open.
  useEffect(() => {
    if (
      !workspace?.tabs.some(
        (tab) =>
          tab.material.status === "processing" ||
          tab.material.status === "queued",
      )
    )
      return;
    let attempt = 0;
    let timer = 0;
    let active = true;
    const tick = () => {
      attempt += 1;
      void refresh().finally(() => {
        if (!active) return;
        timer = window.setTimeout(
          tick,
          Math.min(2500 * 2 ** Math.floor(attempt / 4), 30_000),
        );
      });
    };
    timer = window.setTimeout(tick, 2500);
    return () => { active = false; window.clearTimeout(timer); };
  }, [refresh, workspace]);

  // Route changes cancel only their own load, never a running model turn.
  useEffect(() => () => { sessionLoadRef.current?.abort(); }, [sessionRouteKey]);

  useEffect(() => {
    if (!workspace || workspace.workspace_id !== workspaceId || loading) return;
    if (sessionBootRef.current === sessionRouteKey) return;
    sessionBootRef.current = sessionRouteKey;
    const scoped = courseSessionConfiguration(sessionConfiguration, courseId);
    if (!sessionIdParam) {
      const draftKey = newSession({ ...scoped, capability: null });
      // eslint-disable-next-line react-hooks/set-state-in-effect -- record the draft created for this route.
      setSessionBinding({ scope: sessionScope, draftKey, previousSessionId: state.sessionId });
      return;
    }
    const controller = new AbortController();
    sessionLoadRef.current = controller;
    void (async () => {
      try {
        // A URL can have been polluted by the previously selected practice
        // session. Prove ownership before loading any messages or preferences.
        const rows = await listReadingConversations(workspaceId);
        if (controller.signal.aborted) return;
        if (!rows.some(row => row.session_id === sessionIdParam)) {
          const latest = rows[0];
          const target = latest ? readingSessionRoute(workspaceId, latest.session_id) : readingCollectionRoute(workspaceId);
          const destination = new URL(target, window.location.origin);
          const current = new URL(window.location.href);
          for (const [key, value] of current.searchParams) destination.searchParams.set(key, value);
          setNotice('已切回本书独立的自主学习对话。');
          router.replace(destination.pathname + destination.search, { scroll: false });
          return;
        }
        if (!(state.sessionId === sessionIdParam && state.workspaceMode === READING_WORKSPACE_MODE && state.readingWorkspaceId === workspaceId)) {
          await loadSession(sessionIdParam, { signal: controller.signal });
        }
        if (controller.signal.aborted) return;
        configureSession(scoped, sessionIdParam);
        setSessionBinding({ scope: sessionScope, sessionId: sessionIdParam });
      } catch (caught) {
        if (!controller.signal.aborted) setSessionFailure({ routeKey: sessionRouteKey,
          message: caught instanceof Error ? caught.message : t("The reading conversation could not be loaded.") });
      }
    })();
  }, [configureSession, courseId, loadSession, loading, newSession, router,
    sessionIdParam, sessionConfiguration, sessionScope, sessionRouteKey,
    state.sessionId, state.workspaceMode, state.readingWorkspaceId, t, workspace, workspaceId]);

  const sessionError = sessionFailure?.routeKey === sessionRouteKey ? sessionFailure.message : '';
  const sessionReady = workspace?.workspace_id === workspaceId && isReadingSessionReady({
    binding: sessionBinding, scope: sessionScope, routeSessionId: sessionIdParam, workspaceId, state,
  });

  useEffect(() => {
    if (!material || material.unit !== "segment") return;
    const requestId = ++transcriptRequestRef.current;
    // One request for the whole transcript. Segments follow the speaker's
    // sentences, so a lecture has hundreds of them and fetching each on its own
    // meant hundreds of round trips before the panel could draw anything.
    void getReadingTranscript(material.material_id)
      .then((payload) => {
        if (transcriptRequestRef.current !== requestId) return;
        const rows: TranscriptRow[] = payload.segments
          .filter(
            (row) => row.text !== "[Transcript unavailable for this video.]",
          )
          .map((row) => ({
            locator: row.locator,
            title: row.title || `${row.locator}`,
            text: row.text,
            sourceHref: row.source_href,
          }));
        setTranscript(rows);
      })
      .catch(() => {
        if (transcriptRequestRef.current !== requestId) return;
        setTranscript([]);
        setNotice(t("This transcript could not be loaded."));
      });
  }, [material, t]);

  const activeConversation = useMemo(
    () =>
      conversations.find((row) => row.session_id === state.sessionId) ??
      conversations.find((row) => row.session_id === sessionIdParam) ??
      null,
    [conversations, sessionIdParam, state.sessionId],
  );

  const linkedSessionIds = useMemo(
    () => activeConversation?.linked_session_ids ?? [],
    [activeConversation],
  );

  /* ── Bookmarks ────────────────────────────────────────────────────────
     Kept here rather than inside the reader because two surfaces read them:
     the reader's own toolbar (is *this* page kept?) and the outline panel
     (the list, and jumping to one). Two copies would drift the moment either
     one added a bookmark. */
  const materialId = material?.material_id ?? "";
  const [loadedBookmarks, setLoadedBookmarks] = useState<{
    materialId: string;
    rows: ReadingBookmark[];
  }>({ materialId: "", rows: [] });
  // Derived rather than stored: switching material has to drop the previous
  // one's bookmarks in the same render it switches, and an effect that called
  // setState to clear them would both show the wrong list for a frame and
  // trip the compiler's set-state-in-effect rule.
  const bookmarks =
    loadedBookmarks.materialId === materialId
      ? loadedBookmarks.rows
      : NO_BOOKMARKS;

  useEffect(() => {
    if (!materialId) return;
    let cancelled = false;
    void listBookmarks(materialId)
      .then((rows) => {
        if (!cancelled) setLoadedBookmarks({ materialId, rows });
      })
      .catch(() => {
        if (!cancelled) setLoadedBookmarks({ materialId, rows: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [materialId]);

  // One control, both directions: the toolbar button reads "is this page
  // kept?" off the same list it writes to, so pressing it again removes what
  // the last press added.
  const toggleBookmark = useCallback(
    async (locator: number, label = "") => {
      if (!materialId) return;
      const existing = bookmarks.find((row) => row.locator === locator);
      try {
        if (existing) {
          await deleteBookmark(materialId, existing.bookmark_id);
        } else {
          await addBookmark(materialId, locator, label);
        }
        setLoadedBookmarks({
          materialId,
          rows: await listBookmarks(materialId),
        });
      } catch (caught) {
        setNotice(
          caught instanceof Error
            ? caught.message
            : t("That bookmark could not be saved."),
        );
      }
    },
    [bookmarks, materialId, t],
  );

  const removeBookmark = useCallback(
    async (bookmarkId: string) => {
      if (!materialId) return;
      try {
        await deleteBookmark(materialId, bookmarkId);
        setLoadedBookmarks({
          materialId,
          rows: await listBookmarks(materialId),
        });
      } catch (caught) {
        setNotice(
          caught instanceof Error
            ? caught.message
            : t("That bookmark could not be removed."),
        );
      }
    },
    [materialId, t],
  );

  const switchMaterial = useCallback(
    async (candidate: ReadingLibraryMaterial) => {
      if (!workspace || candidate.material_id === workspace.active_material_id)
        return;
      try {
        const updated = await activateReadingMaterial(
          workspace.workspace_id,
          candidate.material_id,
        );
        setWorkspace(updated);
        setActiveLocator(1);
        reportViewport({ locator: 1, selection: "" });
      } catch (caught) {
        // Without this the tab click is a no-op with no explanation, which is
        // indistinguishable from the click not registering at all.
        setNotice(
          caught instanceof Error
            ? caught.message
            : t("This material could not be opened."),
        );
      }
    },
    [reportViewport, t, workspace],
  );

  useEffect(() => {
    const onReaderAction = (event: Event) => {
      const detail = (event as CustomEvent<ReaderActionPayload>).detail;
      if (detail?.reader_action !== "switch_tab" || !detail.material_id) return;
      const candidate = workspace?.tabs.find(
        (tab) => tab.material.material_id === detail.material_id,
      )?.material;
      if (candidate) void switchMaterial(candidate);
    };
    window.addEventListener(READER_ACTION_EVENT, onReaderAction);
    return () =>
      window.removeEventListener(READER_ACTION_EVENT, onReaderAction);
  }, [switchMaterial, workspace?.tabs]);

  const removeMaterial = useCallback(
    async (candidate: ReadingLibraryMaterial) => {
      if (!workspace) return;
      setWorkspace(
        await removeReadingWorkspaceMaterial(
          workspace.workspace_id,
          candidate.material_id,
        ),
      );
    },
    [workspace],
  );

  // The same gesture /chat's "new chat" makes: reset to a local draft and
  // navigate to the URL that *means* "new". No row is written until the
  // learner actually says something, and the title is then the one the model
  // writes from that first turn rather than a placeholder every conversation
  // shares.
  //
  // Deliberately does not cancel the streaming turn. It used to, copying
  // /chat — and copying its bug: the turn being cancelled belongs to the
  // conversation being navigated *away* from, so starting a new one killed
  // the previous answer mid-flight.
  const newConversation = useCallback(() => {
    if (!workspace) return;
    sessionLoadRef.current?.abort();
    const draftKey = newSession({ ...courseSessionConfiguration(sessionConfiguration, courseId), capability: null });
    const scope = readingSessionScope(workspace.workspace_id, courseId);
    sessionBootRef.current = JSON.stringify([scope, null]);
    setSessionBinding({ scope, draftKey, previousSessionId: state.sessionId });
    const destination = new URL(readingCollectionRoute(workspace.workspace_id), window.location.origin);
    if (courseId.trim()) destination.searchParams.set('course', courseId.trim());
    router.push(destination.pathname + destination.search);
  }, [
    newSession, router, sessionConfiguration, workspace, courseId, state.sessionId,
  ]);

  // When the first turn assigns a session id, put it in the URL and let the
  // conversation menu see the row the backend just attached. Without it a
  // draft conversation would stay on the bare collection URL and a refresh
  // would silently start over.
  //
  // The URL is written with the native history API rather than `router.replace`
  // on purpose. `/reading/<ws>` and `/reading/<ws>/sessions/<id>` are different
  // route matches, and App Router treats moving between them — even within one
  // catch-all segment, which was tried — as a navigation: it unmounted the
  // whole workspace and mounted it again mid-answer. Measured, that meant the
  // reader's subtree left the DOM, "Opening collection…" painted for a frame,
  // the material was re-fetched and the page the learner was on reset to 1.
  // Asking the first question of a conversation blinked the entire screen.
  //
  // Nothing about that navigation was real: the learner stayed exactly where
  // they were and the conversation they are watching stream is the one being
  // named. So the address bar is corrected in place and `usePathname` — which
  // does follow the native API — keeps `sessionIdParam` honest for refreshes,
  // links and the back button.
  useEffect(() => {
    if (!sessionReady || !state.sessionId || sessionIdParam) return;
    const destination = new URL(readingSessionRoute(workspaceId, state.sessionId), window.location.origin);
    const current = new URL(window.location.href);
    for (const [key, value] of current.searchParams) destination.searchParams.set(key, value);
    window.history.replaceState(null, "", destination.pathname + destination.search);
    void listReadingConversations(workspaceId)
      .then(setConversations)
      .catch(() => {});
  }, [sessionIdParam, state.sessionId, workspaceId, sessionReady]);

  const organizeNotes = useCallback(async () => {
    if (!workspace) return;
    try {
      setNotice(t("Organizing notes…"));
      const notes = await organizeReadingNotes(workspace.workspace_id);
      setOrganizedNotes(notes);
      setNotice(t("Notes organized, each one citing where it came from."));
    } catch (caught) {
      setNotice(
        caught instanceof Error
          ? caught.message
          : t("Could not organize notes."),
      );
    }
  }, [t, workspace]);

  const buildMasteryPath = useCallback(
    async (bookId: string) => {
      if (!workspace) return;
      try {
        setNotice(t("Building Mastery Path…"));
        await generateMasteryPathFromReading(
          workspace.workspace_id,
          bookId.trim(),
        );
        setNotice(t("Mastery Path created. Open Learning Space to begin."));
      } catch (caught) {
        setNotice(
          caught instanceof Error
            ? caught.message
            : t("Mastery Path creation failed."),
        );
        throw caught;
      }
    },
    [t, workspace],
  );

  const renameWorkspace = useCallback(
    async (title: string) => {
      if (!workspace) return;
      if (!title?.trim() || title.trim() === workspace.title) return;
      setWorkspace(
        await updateReadingWorkspace(workspace.workspace_id, {
          title: title.trim(),
        }),
      );
    },
    [workspace],
  );

  return {
    workspace,
    setWorkspace,
    conversations,
    setConversations,
    loading,
    sessionLoading: !sessionReady && !sessionError,
    sessionError,
    error,
    notice,
    setNotice,
    material,
    annotations,
    activeTab,
    activeConversation,
    linkedSessionIds,
    activeLocator,
    setActiveLocator,
    bookmarks,
    toggleBookmark,
    removeBookmark,
    transcript,
    organizedNotes,
    setOrganizedNotes,
    refresh,
    switchMaterial,
    removeMaterial,
    newConversation,
    organizeNotes,
    buildMasteryPath,
    renameWorkspace,
    reportViewport,
  };
}

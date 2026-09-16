// MessageTimeline — renders an ApiMessage's content_blocks into structured
// cards. Each block type maps to a dedicated component; nothing falls back to
// raw markdown. Citations are clickable to open the Reader.

import type { ContentBlock, JudgmentCardData, QuestionSignalData, StateChangeData, TaskCardData, TaskCompletionCardData } from "../types/blocks";
import {
  CitationChip,
  ContextCard,
  ErrorRetryCard,
  JudgmentCard,
  QuestionSignalCard,
  StateChangeCard,
  StatusLine,
  TaskCard,
  TaskCompletionCard,
  TaskOptionsCard,
} from "./cards/Cards";
import { MathText } from "./MathText";

export function MessageBlocks({
  blocks,
  onCitation,
  onTaskSubmit,
  onTaskHint,
  onTaskSkip,
  onStartConceptTask,
  onNextTask,
  onExplainTask,
  onPracticeTask,
  onBackToSource,
  onFinishConsolidation,
  onSupplementAnswer,
  onTaskFollowup,
  onTaskFollowupEnd,
  hideHint,
  activeTaskId,
  activeFollowupTaskId,
  canSubmit,
  busy,
  onRetry,
}: {
  blocks: ContentBlock[];
  onCitation: (bookId: string, page: number) => void;
  onTaskSubmit: () => void;
  onTaskHint: (taskId: string) => void;
  onTaskSkip: (taskId: string) => void;
  onStartConceptTask: (conceptId: string) => void;
  onNextTask: (taskId: string) => void;
  onExplainTask: (taskId: string) => void;
  onPracticeTask: (taskId: string) => void;
  onBackToSource: (sourceId: string, page: number) => void;
  onFinishConsolidation: () => void;
  onSupplementAnswer: () => void;
  onTaskFollowup: (taskId: string) => void;
  onTaskFollowupEnd: (taskId: string) => void;
  hideHint?: boolean;
  activeTaskId?: string;
  activeFollowupTaskId?: string;
  canSubmit: boolean;
  busy: boolean;
  onRetry: () => void;
}) {
  // Kept in the public renderer contract for existing callers; “下一题” is
  // now the single continuation action rendered by TaskCompletionCard.
  void onPracticeTask;
  return (
    <>
      {blocks.map((b, i) => {
        switch (b.type) {
          case "text":
            return (
              <div key={i} style={{ fontSize: 14, whiteSpace: "pre-wrap", wordBreak: "break-word", padding: "2px 0" }}>
                <MathText text={b.text || ""} />
              </div>
            );
          case "citation":
            return (
              <CitationChip
                key={i}
                label={b.label || ""}
                sourceId={b.source_id || b.book_id || ""}
                page={b.page || ""}
                onOpen={onCitation}
              />
            );
          case "context":
            return <ContextCard key={i} data={b.data || {}} />;
          case "question_signal":
            return <QuestionSignalCard key={i} data={(b.data || {}) as unknown as QuestionSignalData} onStart={onStartConceptTask} busy={busy} />;
          case "status":
            return <StatusLine key={i} text={b.text || ""} />;
          case "error":
            return <ErrorRetryCard key={i} message={b.text || ""} onRetry={onRetry} />;
          case "task": {
            const d = (b.data || {}) as unknown as TaskCardData & JudgmentCardData;
            if (d.kind === "judgment") return (
              <JudgmentCard
                key={i}
                data={d}
                onExplain={onExplainTask}
                onSupplement={onSupplementAnswer}
                onSkip={onTaskSkip}
                busy={busy}
              />
            );
            if (d.kind === "task_complete") return (
              <TaskCompletionCard
                key={i}
                data={d as unknown as TaskCompletionCardData}
                onNext={onNextTask}
                onExplain={onExplainTask}
                onFollowup={onTaskFollowup}
                onFollowupEnd={onTaskFollowupEnd}
                onBackToSource={onBackToSource}
                onFinish={onFinishConsolidation}
                followupActive={d.task_id === activeFollowupTaskId}
                busy={busy}
              />
            );
            if (d.kind === "task_options") return (
              <TaskOptionsCard
                key={i}
                taskId={d.task_id}
                onHint={onTaskHint}
                onExplain={onExplainTask}
                onSkip={onTaskSkip}
                onSupplement={onSupplementAnswer}
                hideHint={hideHint}
                busy={busy}
              />
            );
            return (
              <TaskCard
                key={i}
                data={d}
                onSubmit={onTaskSubmit}
                onHint={() => d.task_id && onTaskHint(d.task_id)}
                onSkip={() => d.task_id && onTaskSkip(d.task_id)}
                hideHint={hideHint}
                interactive={Boolean(d.task_id) && d.task_id === activeTaskId}
                canSubmit={canSubmit}
                busy={busy}
              />
            );
          }
          case "state_change":
            return <StateChangeCard key={i} data={(b.data || {}) as StateChangeData} />;
          default:
            return null;
        }
      })}
    </>
  );
}

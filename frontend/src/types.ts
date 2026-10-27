export type Ref = {
  material_id: string;
  material_title: string;
  page: number;
  page_title: string;
};
export type Material = {
  id: string;
  title: string;
  kind: "pdf" | "markdown" | "generated";
  page_count: number;
  outline: { title: string; page: number; level: number }[];
  warning?: string;
};
export type Topic = {
  topic: string;
  total: number;
  answered: number;
  correct: number;
  independent: number;
  wrong: number;
  status: string;
};
export type Progress = {
  topics: Topic[];
  answered: number;
  correct: number;
  note_count: number;
};
export type Space = {
  id: string;
  title: string;
  goal: string;
  focus_material_id: string | null;
  focus_page: number;
  materials: Material[];
  progress: Progress;
  updated: string;
  material_count?: number;
  note_count?: number;
};
export type Job = {
  id: string;
  kind: string;
  status: string;
  stage: string;
  partial: string;
  error?: string;
  result?: { material_id?: string; artifact_id?: string; quiz_id?: string };
  created: string;
};
export type Message = {
  id: string;
  role: string;
  content: string;
  refs: Ref[];
  artifact_id?: string;
  created: string;
};
export type Artifact = {
  id: string;
  title: string;
  format: "mermaid" | "svg" | "steps";
  refs: Ref[];
  payload: {
    code?: string;
    explanation: string;
    nodes?: { id: string; label: string; x: number; y: number }[];
    edges?: { from: string; to: string; label: string }[];
    steps?: {
      title: string;
      description: string;
      active: string[];
      visited: string[];
      state: string;
    }[];
  };
};
export type Attempt = {
  id: string;
  answer: string;
  score: number;
  feedback: string;
  hint_used: number;
};
export type Question = {
  id: string;
  type: string;
  topic: string;
  stem: string;
  choices: string[];
  answer?: number;
  explanation?: string;
  evidence?: string;
  hint?: string;
  attempt?: Attempt;
  refs?: Ref[];
};
export type Quiz = {
  id: string;
  title: string;
  topic: string;
  refs: Ref[];
  questions: Question[];
  created: string;
};
export type Note = {
  id: string;
  title: string;
  content: string;
  source: Partial<Ref> & {
    artifact_id?: string;
    message_id?: string;
    quiz_id?: string;
  };
  updated: string;
};
export type Settings = {
  base_url: string;
  model: string;
  has_api_key: boolean;
  api_key?: string;
  temperature: number;
  max_tokens: number;
  timeout: number;
};

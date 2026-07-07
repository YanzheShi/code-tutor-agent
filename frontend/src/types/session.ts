/** 镜像后端 Pydantic：ProblemMeta / Message / SessionStateResp / SubmitResponse */
export type ProblemMeta = {
  problem_id: number;
  title: string;
  description: string;
  description_html?: string;
  topic: string;
  difficulty: string;
  starter_code: string;
  visible_test_cases: Array<{ input_args: string[]; expected_output: string; explanation: string }>;
  novelty_score?: number;
};

export type VisibleTestCase = {
  input_args: string[];
  expected_output: string;
  explanation: string;
};

export type Message = {
  role: "user" | "tutor" | "system";
  content: string;
  metadata?: Record<string, unknown>;
};

/** GET /session/{sid}/state 返回的全量状态 */
export type SessionStateResp = {
  session_id: string;
  status: string;              // "generating" | "awaiting_submit" | "done" | ...
  problem: ProblemMeta | null;
  submissions: unknown[];
  tutor_messages: Message[];
  hint_level: number;
  last_verdict: string | null;
  last_review_payload: Record<string, unknown> | null;
  error_message: string;
  progress_messages: string[];  // 出题进度
};

/** POST /session/{sid}/submit 返回 */
export type SubmitResponse = {
  session_id: string;
  status: string;
  verdict: string | null;
  tutor_message: string | null;
  hint_level: number;
};

/** POST /session/{sid}/run 返回 */
export type RunResult = {
  test_case_id: number;
  passed: boolean;
  status: string;
  detail: string;
  input_args: string[];
  expected: string;
  runtime_ms: number;
  memory_kb: number;
};

export type RunCodeResponse = {
  session_id: string;
  all_passed: boolean;
  results: RunResult[];
  total: number;
  passed: number;
};

export type Submission = {
  code: string;
  verdict: string;
  timestamp: string;
  judge_results: JudgeResult[];
};

export type JudgeResult = {
  phase: string;
  status: string;
  detail: string;
  runtime_ms: number;
};
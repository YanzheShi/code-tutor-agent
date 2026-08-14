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
  constraints?: string[];
  examples?: string[];
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

/**
 * Agent 模式新增字段（后端 SerialiseState 返回）:
 *   - mode: "agent" 表示 agent 驱动模式
 *   - repair_suggestion: 判题后的修复建议
 *   - warm_feedback: 判题后的温暖反馈
 *   - judge_cycle: 当前判题轮次
 */
export type SessionStateResp = {
  session_id: string;
  status: string;              // "dialog" | "awaiting_submit" | "tutoring" | "done" | ...
  mode?: string;               // "practice" | "agent"
  problem: ProblemMeta | null;
  submissions: unknown[];
  tutor_messages: Message[];
  hint_level: number;
  last_verdict: string | null;
  last_review_payload: Record<string, unknown> | null;
  error_message: string;
  progress_messages: string[];  // 出题进度
  repair_suggestion?: string;   // Agent 模式
  warm_feedback?: string;       // Agent 模式
  judge_cycle?: number;         // Agent 模式
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
  index: number;
  language: string;
  hint_level_given: number;
};

export type JudgeResult = {
  phase: string;
  status: string;
  detail: string;
  runtime_ms: number;
  /** Bug 2: 首个失败用例的结构化字段，供「期望 vs 实际」对比面板 */
  input_args?: string[];
  expected_output?: string;
  actual_output?: string;
};

/** 失败用例对比数据（从最后一条 submission 的 base 阶段取） */
export type FailedCase = {
  input_args: string[];
  expected_output: string;
  actual_output: string;
};
/** 镜像后端：JudgeReport（评审卡片） */

export type JudgeReport = {
  time_complexity?: string;
  space_complexity?: string;
  style_rating?: string;         // "good" | "fair" | "needs_improvement"
  style_notes?: string[];
  solution_category?: string;    // "standard" | "optimal" | "brute"
  summary?: string;
};
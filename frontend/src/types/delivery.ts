/** 交付抽象（扩展口子，MVP 只用 web_monaco） */

export type TutorDelivery = {
  channel: "web_monaco" | "vscode_mcp";
  highlight_lines?: number[];
  insert_edits?: { file: string; line: number; content: string }[];
};
import type { ProcessingTask } from "./types";

const terminalStatuses = new Set(["已完成", "不适用", "已取消", "处理失败"]);

export function selectAnalysisRunTasks(items: ProcessingTask[], runId?: string) {
  if (!items.length) return [];
  const latest = [...items].sort((a, b) =>
    (b.analysisRunCreatedAt ?? b.createdAt ?? "").localeCompare(
      a.analysisRunCreatedAt ?? a.createdAt ?? "",
    ),
  )[0];
  const selectedRunId = runId ?? latest.analysisRunId;
  return items.filter((item) => item.analysisRunId === selectedRunId)
    .sort((a, b) => a.stageOrder - b.stageOrder);
}

export function analysisOutcome(tasks: ProcessingTask[]) {
  const runStatus = tasks[0]?.analysisRunStatus;
  if (runStatus === "处理失败") return "failed" as const;
  if (runStatus === "已取消") return "cancelled" as const;
  if (runStatus === "已完成") return "completed" as const;
  if (runStatus === "运行中" || runStatus === "排队中") return null;
  if (!tasks.length || !tasks.every((task) => terminalStatuses.has(task.status))) return null;
  if (tasks.some((task) => task.status === "处理失败")) return "failed" as const;
  if (tasks.some((task) => task.status === "已取消")) return "cancelled" as const;
  return "completed" as const;
}

function nextPoll(signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      resolve();
    };
    const timer = setTimeout(finish, 2_000);
    signal.addEventListener("abort", finish, { once: true });
    if (signal.aborted) finish();
  });
}

export async function watchAnalysisRun(input: {
  runId: string;
  signal: AbortSignal;
  load: () => Promise<{ items: ProcessingTask[] }>;
  onTasks: (tasks: ProcessingTask[]) => void;
  onConnectionError: (reason: string | undefined) => void;
  pause?: (signal: AbortSignal) => Promise<void>;
}) {
  // Only persisted task states end observation; an HTTP timeout says nothing
  // about whether the model is still working in the backend.
  while (!input.signal.aborted) {
    try {
      const { items } = await input.load();
      if (input.signal.aborted) return null;
      input.onConnectionError(undefined);
      input.onTasks(items);
      const tasks = selectAnalysisRunTasks(items, input.runId);
      const status = analysisOutcome(tasks);
      if (status) return { status, tasks };
    } catch (error) {
      if (input.signal.aborted) return null;
      input.onConnectionError(error instanceof Error ? error.message : "本地API暂不可用");
    }
    await (input.pause ?? nextPoll)(input.signal);
  }
  return null;
}

export function formatTaskTime(value?: string) {
  if (!value) return "尚无记录";
  // SQLite may return a naive UTC value, while explicit offsets are preserved.
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`;
  const time = new Date(normalized);
  return Number.isNaN(time.getTime()) ? value : time.toLocaleString("zh-CN", { hour12: false });
}

export function describeProcessingTask(task: ProcessingTask, now = Date.now()) {
  const result = task.result ?? {};
  const parts: string[] = [];
  const transcription = result.transcription && typeof result.transcription === "object"
    ? result.transcription as Record<string, unknown> : undefined;
  const total = transcription?.jobs ?? result.total;
  if (typeof total === "number") {
    parts.push(`已识别 ${transcription?.ok ?? result.transcribed ?? 0}/${total} 段`);
    const skipped = Number(transcription?.skipped ?? result.skipped ?? 0);
    const failed = Number(transcription?.failed ?? result.failed ?? 0);
    const reused = Number(transcription?.reused ?? result.reused ?? 0);
    if (skipped > 0) parts.push(`未识别置空 ${skipped} 段，不影响后续分析`);
    else if (Number(result.empty) > 0) parts.push(`空文本 ${result.empty} 段`);
    if (failed > 0) parts.push(`失败 ${failed} 段`);
    if (reused > 0) parts.push(`复用缓存 ${reused} 段`);
  }
  if (typeof result.batches === "number") {
    parts.push(`已完成 ${result.completed_batches ?? 0}/${result.batches} 批`);
    if (result.batch) parts.push(`${task.status === "处理失败" ? "失败于" : "最近处理"}第 ${result.batch} 批`);
  }
  if (result.current_file) parts.push(`文件：${result.current_file}`);
  if (Number(result.retry) > 0) parts.push(`重试 ${result.retry} 次`);
  const started = result.current_started_at ?? result.started_at ?? task.startedAt;
  if (task.status === "处理中" && typeof started === "string") {
    const stamp = new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(started) ? started : `${started}Z`).getTime();
    if (Number.isFinite(stamp)) parts.push(`本次已等待 ${Math.max(0, Math.floor((now - stamp) / 1000))} 秒`);
  }
  return parts.join(" · ");
}

export function unrecognizedAsrSegments(task: ProcessingTask) {
  const rows = task.result?.skipped_segments;
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((row) => {
    if (!row || typeof row !== "object") return [];
    const { start_ms: start, end_ms: end, material_name: name, error } = row;
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end < start) return [];
    const stamp = (value: number) => {
      const seconds = Math.floor(value / 1000);
      return `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}.${String(value % 1000).padStart(3, "0")}`;
    };
    return [{
      location: `${typeof name === "string" ? `${name} · ` : ""}${stamp(start)}–${stamp(end)}`,
      reason: typeof error === "string" ? error : "未识别出文字",
    }];
  });
}

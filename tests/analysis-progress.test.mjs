import assert from "node:assert/strict";
import test from "node:test";
import { analysisOutcome, describeProcessingTask, selectAnalysisRunTasks, unrecognizedAsrSegments, watchAnalysisRun } from "../app/analysis-progress.ts";

const task = (run, status, extras = {}) => ({
  id: `${run}-stage`, packageId: "meeting-1", analysisRunId: run,
  stage: "意向抽取", stageOrder: 6, status, progress: 40,
  attemptCount: 1, version: 1, updatedAt: "2026-09-10T00:00:00Z", ...extras,
});

test("completed ASR keeps unrecognized counts and timestamps without failing the run", () => {
  const completed = task("current", "已完成", { stage: "音轨与转写", progress: 100,
    result: {
      transcription: { jobs: 682, ok: 658, skipped: 24, empty: 24, failed: 0, reused: 658 },
      skipped_segments: [{ material_name: "meeting.mp3", start_ms: 239970, end_ms: 240500, error: "ASR未返回任何可用的转写片段" }],
    },
  });
  assert.equal(analysisOutcome([completed]), "completed");
  const description = describeProcessingTask(completed);
  assert.match(description, /已识别 658\/682 段/);
  assert.match(description, /未识别置空 24 段，不影响后续分析/);
  assert.match(description, /复用缓存 658 段/);
  assert.doesNotMatch(description, /失败/);
  assert.deepEqual(unrecognizedAsrSegments(completed), [{
    location: "meeting.mp3 · 00:03:59.970–00:04:00.500", reason: "ASR未返回任何可用的转写片段",
  }]);
  assert.match(describeProcessingTask(task("current", "处理中", {
    result: { total: 682, transcribed: 30, skipped: 2, failed: 0 },
  })), /未识别置空 2 段/);
  assert.deepEqual(unrecognizedAsrSegments(task("current", "处理中", {
    result: { skipped_segments: [null, {}, { start_ms: -1, end_ms: 5 }] },
  })), []);
});

test("latest queued run wins over older active or recently cancelled tasks and stages stay ordered", () => {
  const old = task("old", "处理中", { analysisRunCreatedAt: "2026-09-09T00:00:00Z", updatedAt: "2026-09-12T00:00:00Z" });
  const newest = task("new", "待处理", { analysisRunCreatedAt: "2026-09-10T00:00:00Z" });
  const early = { ...newest, id: "new-first", stageOrder: 1 };
  assert.deepEqual(selectAnalysisRunTasks([old, newest, early]).map((item) => item.id), ["new-first", "new-stage"]);
  assert.deepEqual(selectAnalysisRunTasks([newest, old], "old"), [old]);
  assert.deepEqual(selectAnalysisRunTasks([old], "missing"), []);
});

test("request failures retry, successful polls persist state, and model timeout remains a failed task", async () => {
  const errors = [];
  const snapshots = [];
  let calls = 0;
  const pending = task("current", "处理中", { startedAt: "2026-09-01T00:00:00Z" });
  const failed = { ...pending, status: "处理失败", errorMessage: "模型请求超时", finishedAt: "2026-09-10T02:00:00Z" };
  const result = await watchAnalysisRun({
    runId: "current", signal: new AbortController().signal,
    load: async () => {
      calls += 1;
      if (calls === 1) throw new Error("状态请求超时");
      return { items: [task("old", "已完成"), calls < 4 ? pending : failed] };
    },
    onTasks: (items) => snapshots.push(items),
    onConnectionError: (reason) => errors.push(reason),
    pause: async () => {},
  });
  assert.equal(calls, 4);
  assert.equal(result.status, "failed");
  assert.deepEqual(result.tasks, [failed]);
  assert.equal(snapshots.length, 3);
  assert.deepEqual(errors, ["状态请求超时", undefined, undefined, undefined]);
});

test("leaving the page aborts local observation, and a fresh observer can resume the same backend run", async () => {
  const controller = new AbortController();
  let calls = 0;
  const options = {
    runId: "current", signal: controller.signal,
    load: async () => { calls += 1; return { items: [task("current", "处理中")] }; },
    onTasks() {}, onConnectionError() {},
    pause: async () => controller.abort(),
  };
  assert.equal(await watchAnalysisRun(options), null);
  assert.equal(calls, 1);
  const resumed = await watchAnalysisRun({ ...options, signal: new AbortController().signal, load: async () => ({ items: [task("current", "已取消")] }) });
  assert.equal(resumed.status, "cancelled");
  assert.equal(analysisOutcome([task("current", "已取消")]), "cancelled");
  assert.equal(analysisOutcome([task("current", "已完成"), task("current", "待处理")]), null);
  assert.equal(analysisOutcome([task("current", "已完成", { analysisRunStatus: "运行中" })]), null);
  assert.equal(analysisOutcome([task("current", "已完成", { analysisRunStatus: "已取消" })]), "cancelled");
});

test("long-running tasks have no frontend deadline and display durable batch, file and retry details", async () => {
  const controller = new AbortController();
  let polls = 0;
  let clock = Date.now();
  const actualNow = Date.now;
  Date.now = () => clock;
  try {
    const result = await watchAnalysisRun({
      runId: "current", signal: controller.signal,
      load: async () => ({ items: [task("current", ++polls > 3 ? "已完成" : "处理中")] }),
      onTasks() {}, onConnectionError() {},
      pause: async () => { clock += 121 * 60_000; },
    });
    assert.equal(result.status, "completed");
    assert.equal(polls, 4);
  } finally { Date.now = actualNow; }
  const progress = describeProcessingTask(task("current", "处理失败", { result: { batch: 2, batches: 5, completed_batches: 1, current_file: "meeting.wav", retry: 2 } }));
  assert.match(progress, /已完成 1\/5 批/);
  assert.match(progress, /meeting.wav/);
  assert.match(progress, /重试 2 次/);
});

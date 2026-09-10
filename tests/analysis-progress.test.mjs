import assert from "node:assert/strict";
import test from "node:test";
import { analysisOutcome, analysisRunStatusLabel, describeProcessingTask, hasUnalignedMaterials, processingTaskStatusLabel, selectAnalysisRunTasks, unalignedMaterialBatches, unrecognizedAsrSegments, watchAnalysisRun } from "../app/analysis-progress.ts";

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

test("alignment warnings retain completed status, partial success and material details", () => {
  const alignment = task("current", "已完成", { stage: "资料对齐", progress: 100,
    analysisRunStatus: "已完成",
    result: {
      batches: 24, processed_batches: 24, completed_batches: 23, skipped_batches: 1,
      retry_count: 6, has_warnings: true, unmatched_count: 2,
      unaligned_batches: [{ batch: 2, segment_ids: ["segment-1", "segment-2"],
        start_ms: 239970, end_ms: 250500, material_names: ["产品介绍.pdf", "屏幕录制.mp4"],
        target_ids: ["page-1"], error: "输出达到上限，拆分重试后仍未完成", category: "output_limit" }],
    },
  });
  const stages = [alignment, task("current", "已完成", { stage: "意向抽取" })];
  assert.equal(analysisOutcome(stages), "completed");
  assert.equal(analysisRunStatusLabel(stages), "已完成，有未对齐项");
  assert.equal(processingTaskStatusLabel(alignment), "已完成，有未对齐项");
  assert.equal(processingTaskStatusLabel(stages[1]), "已完成");
  assert.match(describeProcessingTask(alignment), /已处理 24\/24 批 · 成功处理 23 批/);
  assert.match(describeProcessingTask(alignment), /部分资料未对齐，涉及 2 段/);
  assert.match(describeProcessingTask(alignment), /拆批重试 6 次/);
  assert.doesNotMatch(describeProcessingTask(alignment), /已完成 24\/24 批/);
  assert.deepEqual(unalignedMaterialBatches(alignment), [{
    location: "第 2 批 · 产品介绍.pdf、屏幕录制.mp4 · 00:03:59.970–00:04:10.500",
    reason: "输出达到上限，拆分重试后仍未完成",
  }]);
});

test("processed alignment batches are not counted as successful and warnings never hide failure or cancellation", () => {
  const alignment = task("current", "处理中", { stage: "资料对齐", analysisRunStatus: "运行中",
    result: { batches: 24, processed_batches: 2, completed_batches: 1, has_warnings: true, unmatched_count: 1 },
  });
  assert.match(describeProcessingTask(alignment), /已处理 2\/24 批 · 成功处理 1 批/);
  assert.equal(processingTaskStatusLabel(alignment), "处理中");
  assert.equal(analysisRunStatusLabel([alignment]), "运行中");
  for (const [status, outcome] of [["处理失败", "failed"], ["已取消", "cancelled"]]) {
    const stopped = { ...alignment, status, analysisRunStatus: status };
    assert.equal(analysisOutcome([stopped]), outcome);
    assert.equal(processingTaskStatusLabel(stopped), status);
    assert.equal(analysisRunStatusLabel([stopped]), status);
  }
});

test("a successful empty alignment has no warning and prior runs cannot leak warning labels", () => {
  const old = task("old", "已完成", { stage: "资料对齐", analysisRunStatus: "已完成",
    analysisRunCreatedAt: "2026-09-09T00:00:00Z", result: { has_warnings: true, unmatched_count: 4 },
  });
  const current = task("current", "已完成", { stage: "资料对齐", analysisRunStatus: "已完成",
    analysisRunCreatedAt: "2026-09-10T00:00:00Z",
    result: { batches: 2, processed_batches: 2, completed_batches: 2, alignments: 0,
      has_warnings: false, unmatched_count: 0, unaligned_batches: [] },
  });
  assert.equal(hasUnalignedMaterials(current), false);
  assert.equal(processingTaskStatusLabel(current), "已完成");
  assert.equal(analysisRunStatusLabel(selectAnalysisRunTasks([old, current])), "已完成");
  assert.deepEqual(unalignedMaterialBatches(current), []);
  assert.doesNotMatch(describeProcessingTask(current), /未对齐/);
  assert.equal(hasUnalignedMaterials({ ...old, stage: "音轨与转写" }), false);
});

test("unaligned material details tolerate incomplete stored diagnostics without inventing time ranges", () => {
  const alignment = task("current", "已完成", { stage: "资料对齐",
    result: { unaligned_batches: [null, "bad", {
      batch: 3, start_ms: -1, end_ms: 500, material_names: [null, "", "材料.pdf"], error: "连接超时",
    }] },
  });
  assert.equal(hasUnalignedMaterials(alignment), true);
  assert.deepEqual(unalignedMaterialBatches(alignment), [{ location: "第 3 批 · 材料.pdf", reason: "连接超时" }]);
  assert.deepEqual(unalignedMaterialBatches({ ...alignment, result: { unaligned_batches: "bad" } }), []);
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

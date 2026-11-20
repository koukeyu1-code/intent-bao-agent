import assert from "node:assert/strict";
import test from "node:test";

import {
  assertIntentReadyForSave,
  intentConfirmationRisks,
  pendingUploadIds,
  prepareIntentConfirmation,
  preparePlatformDraftForSave,
  prepareSpeakerIdentity,
  resolveRequestedItems,
  summarizeSettledWrites,
} from "../app/workflow-guards.ts";
import { buildIntentConfirmPayload } from "../app/intent-view-models.ts";

const taxonomy = {
  technologies: [
    {
      id: "tech-trimode",
      name: "三模",
      status: "active",
      levels: [
        {
          id: "level-size",
          level: 2,
          name: "尺寸",
          values: [
            { id: "size-27", name: "27", status: "active" },
            { id: "size-315", name: "31.5", status: "active" },
          ],
        },
        {
          id: "level-resolution",
          level: 3,
          name: "分辨率",
          values: [
            { id: "resolution-qhd", name: "QHD", status: "active" },
            { id: "resolution-uhd", name: "UHD", status: "active" },
          ],
        },
      ],
      leaves: [
        {
          id: "platform-27-qhd",
          displayName: "27 QHD",
          level2ValueId: "size-27",
          level3ValueId: "resolution-qhd",
          status: "active",
        },
        {
          id: "platform-315-uhd",
          displayName: "31.5 UHD",
          level2ValueId: "size-315",
          level3ValueId: "resolution-uhd",
          status: "active",
        },
      ],
    },
  ],
};

test("speaker identity must be reviewed as complete data and is trimmed", () => {
  assert.deepEqual(
    prepareSpeakerIdentity({
      alias: " 赵总 ",
      role: " 产品规划负责人 ",
      organization: " 客户A产品中心 ",
      functionName: " 产品 ",
    }),
    {
      alias: "赵总",
      role: "产品规划负责人",
      organization: "客户A产品中心",
      functionName: "产品",
    },
  );
  assert.throws(
    () =>
      prepareSpeakerIdentity({
        alias: " ",
        role: "产品规划负责人",
        organization: "客户A产品中心",
        functionName: "产品",
      }),
    /请补全姓名／脱敏称呼/,
  );
});

test("intent and platform validation finishes before persistence", () => {
  assert.doesNotThrow(() =>
    assertIntentReadyForSave({
      id: "intent-1",
      version: 2,
      adoptionStage: "内部评估",
    }),
  );
  assert.throws(
    () =>
      assertIntentReadyForSave({
        id: "intent-1",
        version: 0,
        adoptionStage: "内部评估",
      }),
    /意向版本无效/,
  );
  assert.throws(
    () =>
      preparePlatformDraftForSave(taxonomy, {
        technologyId: "tech-trimode",
        level2ValueId: "size-27",
      }),
    /三级内容必须且只能/,
  );
  assert.throws(
    () =>
      preparePlatformDraftForSave(taxonomy, {
        technologyId: "tech-trimode",
        level2ValueId: "size-27",
        level3ValueId: "resolution-uhd",
      }),
    /不能形成有效产品平台/,
  );
  assert.deepEqual(
    preparePlatformDraftForSave(taxonomy, {
      technologyId: "tech-trimode",
      level2NewValue: " 34 ",
      level3ValueId: "resolution-uhd",
    }),
    {
      technologyId: "tech-trimode",
      level2NewValue: "34",
      level3ValueId: "resolution-uhd",
    },
  );
});

test("bulk write counts only records still present in the current scope", () => {
  const resolved = resolveRequestedItems(
    [{ id: "intent-1" }, { id: "intent-2" }],
    ["intent-2", "stale-intent", "intent-2"],
  );
  assert.deepEqual(resolved.targets, [{ id: "intent-2" }]);
  assert.deepEqual(resolved.missingIds, ["stale-intent"]);

  assert.deepEqual(
    summarizeSettledWrites([
      { status: "fulfilled", value: undefined },
      { status: "rejected", reason: new Error("conflict") },
    ]),
    { attemptedCount: 2, failedCount: 1, successCount: 1 },
  );
});

test("meeting retry skips materials already uploaded to the created package", () => {
  assert.deepEqual(
    pendingUploadIds(
      ["audio", "pdf", "minutes"],
      { audio: "已上传", pdf: "失败", minutes: "待上传" },
    ),
    ["pdf", "minutes"],
  );
});

const confirmableIntent = {
  id: "intent-reviewed",
  version: 2,
  meetingId: "meeting-1",
  adoptionStage: "内部评估",
  attitude: 4,
  identityConfirmed: true,
  confidence: 0.92,
  conflict: false,
  evidenceId: "evidence-1",
  decisionImpact: "评估",
  blocker: "暂无明确卡点",
};
const confirmationEvidence = {
  id: "evidence-1",
  meetingId: "meeting-1",
  quote: "我们先安排内部评估，再讨论是否进入样品验证。",
  timecode: "01:05–01:15",
};

test("low confidence and conflict stay blocked in batch, including a single selected record", () => {
  assert.equal(
    prepareIntentConfirmation(confirmableIntent, confirmationEvidence, { mode: "batch" }),
    undefined,
  );
  for (const risk of [
    { confidence: 0.74 },
    { conflict: true },
    { confidence: 0.3, conflict: true },
  ]) {
    const intent = { ...confirmableIntent, ...risk };
    assert.ok(intentConfirmationRisks(intent).length);
    assert.throws(
      () => prepareIntentConfirmation(intent, confirmationEvidence, { mode: "batch" }),
      /逐条人工复核/,
    );
    const reviewedNote = prepareIntentConfirmation(intent, confirmationEvidence, {
      mode: "manual",
      note: "  已核对原文，仅确认内部评估；历史的样品验证结论不适用于本项目。  ",
    });
    const payload = buildIntentConfirmPayload(intent, "confirm", reviewedNote);
    assert.equal(payload.note, "已核对原文，仅确认内部评估；历史的样品验证结论不适用于本项目。");
    assert.equal(payload.version, 2);
    assert.equal(payload.attitude_level, 4);
    assert.equal(payload.adoption_stage, "内部评估");
  }
});

test("manual review requires a meaningful reason and never bypasses identity, evidence or unknown attitude gates", () => {
  const risky = { ...confirmableIntent, confidence: 0.4, conflict: true };
  for (const note of ["", " \n\t ", "原".repeat(2001)]) {
    assert.throws(
      () => prepareIntentConfirmation(risky, confirmationEvidence, { mode: "manual", note }),
      /复核理由/,
    );
  }
  for (const review of [{ mode: "batch" }, { mode: "manual", note: "已仔细核对原文" }]) {
    for (const [intent, evidence, message] of [
      [{ ...confirmableIntent, identityConfirmed: false }, confirmationEvidence, /说话人身份/],
      [{ ...confirmableIntent, attitude: null }, confirmationEvidence, /态度未知/],
      [{ ...confirmableIntent, evidenceId: "" }, confirmationEvidence, /可追溯证据/],
      [confirmableIntent, undefined, /可追溯证据/],
      [confirmableIntent, { ...confirmationEvidence, quote: " " }, /可追溯证据/],
      [confirmableIntent, { ...confirmationEvidence, meetingId: "another-meeting" }, /可追溯证据/],
    ]) {
      assert.throws(() => prepareIntentConfirmation(intent, evidence, review), message);
    }
  }
  assert.equal(
    buildIntentConfirmPayload(confirmableIntent, "unknown", "不能变成确认理由").note,
    "当前证据不足，保留未知；不改写采用阶段",
  );
});

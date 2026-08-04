import assert from "node:assert/strict";
import test from "node:test";

import {
  assertIntentReadyForSave,
  pendingUploadIds,
  preparePlatformDraftForSave,
  prepareSpeakerIdentity,
  resolveRequestedItems,
  summarizeSettledWrites,
} from "../app/workflow-guards.ts";

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

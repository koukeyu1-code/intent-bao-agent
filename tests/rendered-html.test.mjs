import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";
import {
  ADOPTION_STAGE_ORDER,
  assertConfirmedStage,
  buildIntentConfirmPayload,
  computeOverviewMetrics,
  groupFormalIntentsByScope,
} from "../app/intent-view-models.ts";

const projectRoot = new URL("../", import.meta.url);

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const handler =
    typeof worker === "function" ? worker : worker.fetch.bind(worker);

  return handler(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the intent-bao application shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>意向宝｜让客户声音照进产品规划<\/title>/i);
  assert.match(html, /让客户声音照进产品规划/);
  assert.match(html, /数据加载中/);
  assert.match(html, /连接结果明确前不展示内置演示业务数据/);
  assert.match(html, /技术视角/);
  assert.match(html, /客户视角/);
  assert.match(html, /AI洞察确认/);
  assert.match(html, /模型设置/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
  assert.doesNotMatch(html, /Your site is taking shape|Building your site/i);
});

test("removes starter assets and keeps product metadata", async () => {
  const [layout, packageJson] = await Promise.all([
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(layout, /意向宝｜让客户声音照进产品规划/);
  assert.match(layout, /\/og\.png/);
  assert.doesNotMatch(layout, /codex-preview|Starter Project|favicon\.svg/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);

  await access(new URL("../public/og.png", import.meta.url));
  await assert.rejects(access(new URL("../app/_sites-preview", projectRoot)));
});

test("keeps P0 planning views on formal statuses and persists workflow actions", async () => {
  const [appSource, apiSource, typesSource] = await Promise.all([
    readFile(new URL("../app/intent-bao-app.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api-client.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/types.ts", import.meta.url), "utf8"),
  ]);

  assert.match(typesSource, /formalIntents:\s*FormalIntentStatus\[\]/);
  assert.match(
    apiSource,
    /\/formal-intent-statuses\?current_only=true/,
    "formal planning state must come from the formal-status endpoint",
  );

  const formalViewBindings =
    appSource.match(/intents=\{dataset\.formalIntents\}/g) ?? [];
  assert.equal(
    formalViewBindings.length,
    3,
    "technology, customer, and history views must use formal statuses",
  );
  assert.doesNotMatch(
    appSource,
    /<TechnologyPage[\s\S]{0,160}intents=\{dataset\.intents\}/,
  );
  assert.doesNotMatch(
    appSource,
    /<CustomerPage[\s\S]{0,160}intents=\{dataset\.intents\}/,
  );
  assert.doesNotMatch(
    appSource,
    /平均职能态度/,
    "planning views must not turn function-level evidence into a customer score",
  );
  assert.match(appSource, /不做客户平均分，也不形成客户总分/);

  const createIndex = appSource.indexOf("createMeetingPackage(values)");
  const uploadIndex = appSource.indexOf(
    "uploadMeetingMaterial(created.id, selected as File)",
  );
  const analyzeIndex = appSource.indexOf(
    "analyzeMeetingPackage(created.id)",
  );
  assert.ok(
    createIndex >= 0 && uploadIndex > createIndex && analyzeIndex > uploadIndex,
    "meeting workflow must create, upload with the returned id, then analyze",
  );
  assert.doesNotMatch(appSource, /MTG-DEMO/);
  assert.match(
    apiSource,
    /\/meeting-packages\/\$\{meetingId\}\/files/,
  );
  assert.match(
    apiSource,
    /\/meeting-packages\/\$\{meetingId\}\/analyze/,
  );
  assert.doesNotMatch(
    apiSource,
    /adapter:\s*"mock"/,
    "analysis must use the server-side active model policy",
  );
  assert.match(
    apiSource,
    /\/model-settings/,
    "the frontend must expose the server-side model settings contract",
  );
  assert.match(
    apiSource,
    /180_000/,
    "real local model analysis needs a longer client timeout",
  );
  assert.match(apiSource, /attendees:\s*payload\.attendeeName/);
  assert.match(appSource, /name="attendeeName"/);
  assert.match(appSource, /name="attendeeRole"/);
  assert.match(appSource, /name="attendeeOrganization"/);
  assert.match(appSource, /name="attendeeFunction"/);
  assert.match(
    appSource,
    /okButtonProps=\{\{\s*disabled:\s*!fileList\.length\s*\|\|\s*submitting/,
  );
  assert.match(
    appSource,
    /disabled=\{!detail\.materials\.length\}/,
    "the frontend only checks that material exists; the server owns model policy",
  );
  assert.doesNotMatch(
    appSource,
    /disabled=\{[\s\S]{0,120}detail\.dataClass !== "模拟数据"/,
    "non-mock data must reach the server-side model policy instead of being blocked in the UI",
  );
  assert.match(appSource, /materialTypeForFile\(file\.name\).*sizeLabel/s);

  assert.match(appSource, /updateIntentSuggestion\(editing\)/);
  assert.match(
    appSource,
    /const refreshed = await reloadDataset\(\)[\s\S]{0,220}persisted\.adoptionStage !== expectedStage/,
  );
  assert.match(
    appSource,
    /updateIntentStatus\(item, action\)[\s\S]{0,700}await reloadDataset\(\)/,
  );
  assert.match(
    appSource,
    /confirmSpeakerIdentity[\s\S]{0,500}await reloadDataset\(\)/,
  );
  assert.match(apiSource, /status:\s*"退回补充"/);
  assert.match(
    appSource,
    /intents=\{dataset\.formalIntents\}[\s\S]{0,180}evidence=\{dataset\.evidence\}/,
    "history must resolve stable formal status keys",
  );
  assert.match(appSource, /item\.statusKey === intentId/);
});

const formalStatus = (overrides = {}) => ({
  id: "formal-1",
  statusKey:
    "customer-a|tech-mini-led|platform-27|project-a|function-product",
  customer: "客户A",
  technology: "Mini LED",
  platform: "27英寸5K",
  customerProject: "客户A专业显示器",
  functionName: "产品",
  attitude: 4,
  adoptionStage: "产品项目规划",
  isSupporter: true,
  isResistance: false,
  conditions: [],
  blocker: "暂无明确卡点",
  evidenceId: "evidence-1",
  evidenceCount: 1,
  effectiveAt: "2026-07-30",
  confirmedAt: "2026-07-30T09:00:00",
  confirmedBy: "产品规划负责人",
  version: 1,
  isCurrent: true,
  ...overrides,
});

const observation = (overrides = {}) => ({
  id: "observation-1",
  version: 2,
  meetingId: "meeting-a",
  customer: "客户A",
  technology: "Mini LED",
  platform: "27英寸5K",
  customerProject: "客户A专业显示器",
  functionName: "产品",
  person: "赵总",
  attitude: 4,
  adoptionStage: "产品项目规划",
  confidence: 0.92,
  decisionImpact: "推动",
  supportReason: "愿意推动样机",
  blocker: "暂无明确卡点",
  status: "待确认",
  evidenceId: "evidence-1",
  conflict: false,
  updatedAt: "2026-07-30T09:00:00",
  ...overrides,
});

test("groups formal states by customer, technology, platform, and customer project", () => {
  const secondScope = formalStatus({
    id: "formal-2",
    statusKey:
      "customer-a|tech-mini-led|platform-31|project-b|function-product",
    platform: "31.5英寸6K",
    customerProject: "客户A旗舰显示器",
    evidenceId: "evidence-2",
  });
  const rows = groupFormalIntentsByScope(
    [formalStatus(), secondScope],
    {
      technology: "Mini LED",
      customerOrder: ["客户A", "客户B"],
      includeEmptyCustomers: true,
    },
  );

  assert.equal(rows.length, 3);
  assert.deepEqual(
    rows
      .filter((row) => row.customer === "客户A")
      .map((row) => row.byFunction.get("产品")?.evidenceId),
    ["evidence-1", "evidence-2"],
  );
  const emptyCustomer = rows.find((row) => row.customer === "客户B");
  assert.ok(emptyCustomer);
  assert.equal(emptyCustomer.items.length, 0);
});

test("computes dynamic evidence, current risks, and all seven deduplicated stages", () => {
  const stageStatuses = ADOPTION_STAGE_ORDER.flatMap((stage, index) => {
    const base = formalStatus({
      id: `formal-${index}`,
      statusKey: `scope-${index}|function-product`,
      customer: `客户${index}`,
      platform: `平台${index}`,
      customerProject: `项目${index}`,
      adoptionStage: stage,
      confirmedAt: `2026-07-${String(index + 10).padStart(2, "0")}T09:00:00`,
    });
    return index === 4
      ? [
          base,
          formalStatus({
            ...base,
            id: "same-scope-other-function",
            statusKey: `scope-${index}|function-purchase`,
            functionName: "采购",
          }),
        ]
      : [base];
  });
  const data = {
    formalIntents: stageStatuses,
    intents: [
      observation({ id: "pending-risk", attitude: null }),
      observation({
        id: "old-unknown",
        attitude: null,
        status: "已确认",
      }),
    ],
    evidence: [{ id: "e-1" }, { id: "e-2" }],
  };
  const metrics = computeOverviewMetrics(data);

  assert.equal(metrics.evidenceCount, 2);
  assert.equal(metrics.pendingCount, 1);
  assert.equal(metrics.riskCount, 1);
  assert.deepEqual(
    ADOPTION_STAGE_ORDER.map((stage) => metrics.stageCounts[stage]),
    [1, 1, 1, 1, 1, 1, 1],
  );
});

test("keeps unknown actions from overwriting adoption stage and validates confirm response", () => {
  const current = observation({ adoptionStage: "产品项目规划" });
  const unknownPayload = buildIntentConfirmPayload(current, "unknown");
  const confirmPayload = buildIntentConfirmPayload(current, "confirm");

  assert.equal("adoption_stage" in unknownPayload, false);
  assert.equal(confirmPayload.adoption_stage, "产品项目规划");
  assert.doesNotThrow(() =>
    assertConfirmedStage("产品项目规划", "产品项目规划"),
  );
  assert.throws(
    () => assertConfirmedStage("产品项目规划", "内部评估"),
    /确认结果采用阶段不一致/,
  );
});

test("keeps package scope, stable evidence keys, safe empty states, and loading shell in source contract", async () => {
  const [appSource, apiSource] = await Promise.all([
    readFile(new URL("../app/intent-bao-app.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api-client.ts", import.meta.url), "utf8"),
  ]);

  assert.match(
    appSource,
    /onNavigate\("speakers",\s*record\.id\)/,
    "meeting entry must carry its package id",
  );
  assert.match(
    appSource,
    /speakers\.filter\(\(item\) => item\.meetingId === meetingId\)/,
    "speaker workbench must stay inside the selected package",
  );
  assert.match(
    appSource,
    /onNavigate\("insights",\s*meetingId\)/,
    "speaker-to-insight navigation must retain package context",
  );
  assert.match(
    appSource,
    /onEvidence\(item\.evidenceId,\s*item\.intentId\)/,
    "history evidence must carry its stable status key",
  );
  assert.match(
    appSource,
    /target\.platform[\s\S]{0,120}target\.customerProject[\s\S]{0,120}target\.functionName/,
    "history labels must expose the complete planning scope",
  );
  assert.match(appSource, /customer \? customer\.slice\(-1\) : "—"/);
  assert.match(
    appSource,
    /useState<MvpDataset \| null>\(null\)/,
    "initial render must not show the mock dataset",
  );
  assert.doesNotMatch(
    appSource,
    /useState<MvpDataset>\(mockDataset\)/,
  );
  assert.match(
    apiSource,
    /assertConfirmedStage\([\s\S]{0,140}response\.observation\.suggested_adoption_stage/,
  );
  assert.match(
    apiSource,
    /response\.formal_status\.is_current[\s\S]{0,180}response\.formal_status\.adoption_stage/,
  );
});

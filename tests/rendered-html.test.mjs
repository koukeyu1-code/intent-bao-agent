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

test("server-renders the authenticated application gate", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>意向宝｜让客户声音照进产品规划<\/title>/i);
  assert.match(html, /让客户声音照进产品规划/);
  assert.match(html, /正在检查登录状态/);
  assert.match(html, /role="status"/);
  assert.doesNotMatch(
    html,
    /连接结果明确前不展示内置演示业务数据/,
    "protected application content must not be server-rendered before authentication",
  );
  assert.doesNotMatch(html, /关键意向结构化准确率 85%/);
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

test("keeps the login card free of the redundant English brand line", async () => {
  const loginGateSource = await readFile(
    new URL("../app/login-gate.tsx", import.meta.url),
    "utf8",
  );

  assert.doesNotMatch(loginGateSource, /INTENT BAO|INTENT 包/);
  assert.match(loginGateSource, /登录意向宝/);
});

test("serializes model-setting operations and locks the form while one request is active", async () => {
  const source = await readFile(
    new URL("../app/model-settings-page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /activeOperationRef = useRef<ModelOperation \| undefined>/);
  assert.equal(
    source.match(/if \(!beginOperation\("(?:models|test|save|clear-key)"\)\) return;/g)
      ?.length,
    4,
    "model list, connection test, save, and key clearing must share one synchronous operation lock",
  );
  assert.equal(
    source.match(/finishOperation\("(?:models|test|save|clear-key)"\)/g)?.length,
    4,
    "every model operation must release the shared lock in its finally block",
  );
  assert.match(source, /<Form[\s\S]{0,180}disabled=\{loading \|\| operationBusy\}/);
  assert.match(source, /loading=\{activeOperation === "clear-key"\}/);
  assert.match(
    source,
    /disabled=\{[\s\S]{0,100}operationBusy[\s\S]{0,100}!settings\.apiKeyConfigured/,
    "clearing the key must be unavailable while another operation is active",
  );
});

test("groups navigation and keeps competitive intelligence separate from formal states", async () => {
  const [appSource, apiSource, mockSource, typesSource, stylesSource] = await Promise.all([
    readFile(new URL("../app/intent-bao-app.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api-client.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/mock-data.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/types.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(typesSource, /\| "competition"/);
  assert.match(appSource, /label: "业务流程"[\s\S]*label: "视角模块"[\s\S]*label: "基础配置"/);
  assert.match(appSource, /navigationSections\s*\.map\(\(section\) =>/);
  assert.match(
    appSource,
    /items: section\.items\.filter\(\(item\) =>[\s\S]{0,100}canAccessView\(user\.role, item\.key\)/,
    "desktop and mobile navigation must derive visibility from the authenticated role",
  );
  assert.match(appSource, /items=\{navItems\}/);
  assert.match(appSource, /visibleNavigationSections\.map\(\(section\) =>/);
  assert.match(
    appSource,
    /const currentView = resolveAuthorizedView\(user\.role, activeView\)/,
    "rendering must sanitize stale administrator-only selections",
  );
  assert.match(appSource, /switch \(currentView\)/);
  assert.doesNotMatch(
    appSource,
    /switch \(activeView\)/,
    "protected content must never render directly from unchecked navigation state",
  );
  assert.doesNotMatch(appSource, /className="mobile-nav__group-label"/);
  assert.match(stylesSource, /\.app-menu \.ant-menu-item-group-title \{\s*display: none;/);
  assert.match(appSource, /aria-current=\{currentView === item\.key \? "page" : undefined\}/);
  assert.match(appSource, /case "competition":/);
  assert.match(appSource, /competitiveIntents=\{dataset\.competitiveIntents\}/);

  assert.match(typesSource, /mappedTechnology:\s*string/);
  assert.match(mockSource, /dataClass: "模拟数据"/);
  assert.match(mockSource, /mockCompetitiveIntents/);
  assert.match(apiSource, /data\.competitiveIntents \?\? mockCompetitiveIntents/);
  assert.match(
    appSource,
    /const key = `\$\{customer\}\|\$\{platform\}\|\$\{customerProject\}`/,
    "competitive rows must align on customer, platform, and project",
  );
  assert.match(
    appSource,
    /const mappedTechnologyCompetitors = useMemo[\s\S]{0,900}requireConcretePlatform: true/,
    "competition statistics must first exclude records without a concrete leaf mapping",
  );
  assert.match(
    appSource,
    /const competitorScoped = useMemo[\s\S]{0,500}cascadeModel\.selection[\s\S]{0,160}requireConcretePlatform: true/,
    "competition results must use the same three-level cascade selection as the technology view",
  );
  assert.match(
    appSource,
    /technologyCompetitorCount - mappedTechnologyCompetitors\.length/,
    "the unmapped count must be calculated at technology scope rather than from the active leaf filter",
  );
  assert.ok(
    (appSource.match(/`\$\{item\.customer\}\|\$\{item\.platformLeafId\}`/g) ?? [])
      .length >= 2,
    "own and competitor records must associate by stable platform leaf id",
  );
  assert.match(appSource, /竞品区为脱敏模拟情报/);
  assert.match(appSource, /item\?\.attitude \?\? null/);
  assert.doesNotMatch(
    appSource,
    /竞品[^\n]{0,40}(平均分|总评分)/,
    "competitive view must compare function signals instead of scoring a customer",
  );
});

test("keeps the overview promise and navigation actions aligned", async () => {
  const [appSource, stylesSource] = await Promise.all([
    readFile(new URL("../app/intent-bao-app.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(appSource, /把客户会议自动整理成/);
  assert.match(appSource, /“客户 × 技术 × 产品 × 职能”/);
  assert.match(appSource, /助力业务突破与产品规划/);
  assert.match(appSource, /待确认意向\{pendingCount\}条/);
  assert.match(
    appSource,
    /onClick=\{\(\) => onNavigate\("customer"\)\}[\s\S]*查看意向地图/,
  );
  assert.match(stylesSource, /\.hero-kicker \{[\s\S]*font-size: 15px;/);
});

test("orders dictionary categories and exposes parent relationship selectors", async () => {
  const [appSource, mockSource, typesSource] = await Promise.all([
    readFile(new URL("../app/intent-bao-app.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/mock-data.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/types.ts", import.meta.url), "utf8"),
  ]);

  assert.match(
    appSource,
    /统一技术、产品平台、客户、项目、职能内容与别名，减少语义抽取结果歧义。/,
  );
  assert.match(
    appSource,
    /const DICTIONARY_TABS:[\s\S]*"technologies",[\s\S]*"platforms",[\s\S]*"customers",[\s\S]*"projects",[\s\S]*"functions"/,
  );
  assert.match(appSource, /platforms: \{ source: "technologies", label: "所属技术"/);
  assert.match(appSource, /projects: \{ source: "customers", label: "所属客户"/);
  assert.match(appSource, /functions: \{ source: "customers", label: "所属客户"/);
  assert.match(appSource, /className="dictionary-parent-filter"/);
  assert.match(appSource, /name="parentIds"/);
  assert.match(typesSource, /parentIds\?: string\[\]/);
  assert.match(mockSource, /parentIds: string\[\] = \[\]/);
  assert.doesNotMatch(appSource, /关键意向结构化准确率 85%/);
});

test("keeps speaker labels and intent actions inside their responsive containers", async () => {
  const [appSource, stylesSource] = await Promise.all([
    readFile(new URL("../app/intent-bao-app.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(appSource, /className="speaker-list-item__meta"/);
  assert.match(appSource, /aria-pressed=\{activeSpeaker\?\.id === speaker\.id\}/);
  assert.match(
    stylesSource,
    /\.speaker-list-item__meta strong,[\s\S]{0,180}white-space: nowrap;/,
  );

  assert.match(appSource, /className="intent-row-actions"/);
  assert.match(appSource, /tableLayout="fixed"/);
  assert.match(appSource, /columnWidth: 48/);
  assert.match(appSource, /scroll=\{\{ x: 1220 \}\}/);
  assert.match(
    stylesSource,
    /\.intent-row-actions \{[\s\S]{0,180}grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/,
  );
  assert.match(
    stylesSource,
    /\.intent-row-actions \.ant-btn \{[\s\S]{0,180}padding-inline: 5px;/,
  );
});

test("keeps P0 planning views on formal statuses and persists workflow actions", async () => {
  const [appSource, apiSource, typesSource] = await Promise.all([
    readFile(new URL("../app/intent-bao-app.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api-client.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/types.ts", import.meta.url), "utf8"),
  ]);

  assert.match(typesSource, /formalIntents:\s*FormalIntentStatus\[\]/);
  assert.match(
    typesSource,
    /competitiveIntents:\s*CompetitiveIntentStatus\[\]/,
  );
  assert.match(
    apiSource,
    /\/formal-intent-statuses\?current_only=true/,
    "formal planning state must come from the formal-status endpoint",
  );

  const formalViewBindings =
    appSource.match(/intents=\{dataset\.formalIntents\}/g) ?? [];
  assert.equal(
    formalViewBindings.length,
    4,
    "technology, competition, customer, and history views must use formal statuses",
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
    /<CompetitionPage[\s\S]{0,220}intents=\{dataset\.intents\}/,
  );
  assert.doesNotMatch(
    appSource,
    /平均职能态度/,
    "planning views must not turn function-level evidence into a customer score",
  );
  assert.match(appSource, /不做客户平均分，也不形成客户总分/);
  assert.match(
    appSource,
    /未映射具体平台的竞品记录未计入/,
    "formal competitor comparisons must exclude records without a concrete platform mapping",
  );

  const createIndex = appSource.indexOf("createMeetingPackage(values)");
  const uploadIndex = appSource.indexOf(
    "uploadMeetingMaterial(createdId, selected as File)",
  );
  const analyzeIndex = appSource.indexOf(
    "analyzeMeetingPackage(createdId)",
  );
  assert.ok(
    createIndex >= 0 && uploadIndex > createIndex && analyzeIndex > uploadIndex,
    "meeting workflow must create, upload with the returned id, then analyze",
  );
  assert.match(
    appSource,
    /createdPackageId[\s\S]{0,1200}pendingUploadIds/,
    "meeting retry must reuse the created package and skip uploaded files",
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
    /loadPackageTasks/,
    "analysis must switch to background polling (202 + task progress)",
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

  assert.match(
    appSource,
    /updateIntentReviewDraft\(editing, preparedPlatformDraft\)/,
  );
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

test("wires human review and client guards before workflow persistence", async () => {
  const appSource = await readFile(
    new URL("../app/intent-bao-app.tsx", import.meta.url),
    "utf8",
  );

  const identityValidationIndex = appSource.indexOf(
    "const identity = prepareSpeakerIdentity",
  );
  const identityWriteIndex = appSource.indexOf(
    "await confirmSpeakerIdentity(speaker.id, speaker.version, identity)",
  );
  assert.ok(
    identityValidationIndex >= 0 && identityWriteIndex > identityValidationIndex,
    "speaker fields must be validated and normalized before confirmation",
  );
  assert.match(appSource, /reviewedSpeakerIds\.has\(activeSpeaker\.id\)/);

  const platformValidationIndex = appSource.indexOf(
    "const preparedPlatformDraft = platformDraftDirty",
  );
  const suggestionWriteIndex = appSource.indexOf(
    "await updateIntentReviewDraft(editing, preparedPlatformDraft)",
  );
  const reviewValidationIndex = appSource.indexOf(
    "assertIntentReadyForSave(editing)",
  );
  assert.ok(
    reviewValidationIndex >= 0 &&
      platformValidationIndex > reviewValidationIndex &&
      suggestionWriteIndex > platformValidationIndex,
    "review fields and the full platform path must be validated before the atomic edit request",
  );
  assert.match(appSource, /resolveRequestedItems\(scopedIntents, ids\)/);
  assert.match(appSource, /summarizeSettledWrites\(persistenceResults\)/);
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
  const [appSource, apiSource, loginGateSource] = await Promise.all([
    readFile(new URL("../app/intent-bao-app.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api-client.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/login-gate.tsx", import.meta.url), "utf8"),
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
  assert.doesNotMatch(
    appSource,
    /setDataset\(mockDataset\)/,
    "expired sessions and API failures must not silently expose fallback data",
  );
  assert.match(
    apiSource,
    /export const updateIntentReviewDraft[\s\S]{0,700}version: intent\.version \?\? 1,[\s\S]{0,900}platform:/,
    "the atomic review request must carry the observation optimistic-lock version with its platform draft",
  );
  assert.doesNotMatch(
    appSource,
    /saveObservationPlatformDraft\(/,
    "the UI must not split one review into a second platform write",
  );
  assert.match(
    appSource,
    /新增内容将在本条意向正式确认后生效/,
    "pending menu content must state its publication boundary",
  );
  assert.match(
    apiSource,
    /dispatchEvent\(new Event\("intent-bao:auth-expired"\)\)/,
  );
  assert.match(
    loginGateSource,
    /addEventListener\("intent-bao:auth-expired"/,
    "any protected API 401 must return the UI to the login gate",
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

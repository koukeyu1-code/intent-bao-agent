import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import * as authApi from "../app/auth-api.ts";
import * as intentViewModels from "../app/intent-view-models.ts";
import { componentHarness, elements, loadModule, renderedText } from "./helpers/component-harness.mjs";

const client = await loadModule("../../app/api-client.ts", {
  "./auth-api": authApi,
  "./intent-view-models": intentViewModels,
});

async function withClientFetch(fetch, callback) {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  globalThis.window = {
    setTimeout, clearTimeout,
    sessionStorage: { getItem: () => "test-only-token" },
  };
  globalThis.fetch = fetch;
  try { await callback(); } finally {
    globalThis.fetch = originalFetch;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
}


test("business API shares the auth base and sends Bearer without cookies", async () => {
  const [source, launcher] = await Promise.all([
    readFile(new URL("../app/api-client.ts", import.meta.url), "utf8"),
    readFile(new URL("../scripts/start-local.ps1", import.meta.url), "utf8"),
  ]);
  assert.match(source, /export const API_BASE = resolveAuthApiBase\(\)/);
  assert.doesNotMatch(source, /credentials:\s*"include"/);
  assert.equal(
    source.match(/credentials:\s*"omit"/g)?.length,
    4,
    "JSON, material upload, Excel preflight, and result export must all reject cookies",
  );
  assert.ok(
    (source.match(/bearerHeaders\(\)/g)?.length ?? 0) >= 3,
    "every business fetch path must attach the session Bearer token",
  );
  assert.match(
    source,
    /status === 401[\s\S]{0,160}clearAccessToken\(\)[\s\S]{0,160}intent-bao:auth-expired/,
  );
  assert.match(
    launcher,
    /NEXT_PUBLIC_INTENT_BAO_API_BASE = "http:\/\/127\.0\.0\.1:\$BackendPort\/api"/,
  );
  assert.doesNotMatch(launcher, /HttpOnly|SameSite|login cookie/i);
});

test("intent review fields and platform path use one atomic API request", async () => {
  const [clientSource, appSource] = await Promise.all([
    readFile(new URL("../app/api-client.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/intent-bao-app.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(
    clientSource,
    /export const updateIntentReviewDraft[\s\S]*\/review-draft/,
  );
  assert.match(
    appSource,
    /await updateIntentReviewDraft\(editing, preparedPlatformDraft\)/,
  );
  assert.doesNotMatch(appSource, /saveObservationPlatformDraft\(/);
});

test("review-draft writes preserve highlight specifications when adding, editing, and clearing before reload", async () => {
  const intent = { id: "observation-test", version: 1, attitude: 4, adoptionStage: "内部评估", supportReason: "原文支持", blocker: "暂无明确卡点" };
  let saved = { ...intent };
  const requests = [];
  await withClientFetch(async (url, init) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/review-draft")) {
      const body = JSON.parse(init.body);
      requests.push(body);
      saved = { ...saved, highlightSpec: body.highlight_spec ?? "", version: saved.version + 1 };
      return Response.json({ observation: { version: saved.version }, platform_draft: null, message: "已保存" });
    }
    if (path.endsWith("/bootstrap")) return Response.json({ meetings: [], speakers: [], intents: [saved], evidence: [], history: [], dictionaries: {} });
    if (path.endsWith("/formal-intent-statuses")) return Response.json({ items: [], total: 0 });
    if (path.endsWith("/platform-taxonomy")) return Response.json({ technologies: [] });
    throw new Error(`Unexpected request: ${path}`);
  }, async () => {
    for (const highlightSpec of ["180Hz低拖影", "240Hz低拖影", ""]) {
      await client.updateIntentReviewDraft({ ...saved, highlightSpec });
      const reloaded = await client.loadBootstrapDataset();
      assert.equal(reloaded.intents[0].highlightSpec, highlightSpec);
    }
  });
  assert.deepEqual(requests.map((body) => body.highlight_spec), ["180Hz低拖影", "240Hz低拖影", null]);
  assert.deepEqual(requests.map((body) => body.version), [1, 2, 3]);
});

test("key clearing sends only an atomic DELETE without model, ASR, or secret form fields", async () => {
  let captured;
  await withClientFetch(async (url, init) => {
    captured = { url, init };
    return Response.json({ provider: "ollama", base_url: "http://127.0.0.1:11434", model_name: "saved-model", asr: { provider: "llamacpp", base_url: "http://127.0.0.1:8099", model_name: "saved-asr" }, api_key_configured: false });
  }, async () => {
    const next = await client.clearModelApiKey();
    assert.equal(next.modelName, "saved-model");
    assert.equal(next.asrModelName, "saved-asr");
    assert.equal(next.apiKeyConfigured, false);
  });
  assert.match(captured.url, /\/model-settings\/api-key$/);
  assert.equal(captured.init.method, "DELETE");
  assert.equal(captured.init.body, undefined);
});

test("clear-key interaction preserves invalid unsaved model and ASR drafts without saving or validating them", async () => {
  for (const draftProvider of ["ollama", "mock"]) {
    const harness = componentHarness();
    let values = {};
    let validations = 0;
    let allowValidation = false;
    let saves = 0;
    let clears = 0;
    const notices = [];
    const form = {
      setFieldsValue: (next) => { values = { ...values, ...next }; },
      getFieldValue: (name) => values[name],
      getFieldsValue: () => values,
      validateFields: async () => {
        validations += 1;
        if (!allowValidation) throw new Error("草稿未完成");
        return values;
      },
    };
    const placeholder = () => null;
    const Form = Object.assign(() => null, { Item: placeholder, useForm: () => [form], useWatch: (name) => values[name] });
    const Input = Object.assign(() => null, { Password: placeholder });
    const Descriptions = Object.assign(() => null, { Item: placeholder });
    const Popconfirm = () => null;
    const antd = new Proxy({ Form, Input, Descriptions, Popconfirm, Typography: { Paragraph: placeholder, Title: placeholder }, message: { success: (value) => notices.push(value), error: (value) => notices.push(value) } }, { get: (target, name) => target[name] ?? placeholder });
    const saved = { provider: "openai-compatible", baseUrl: "http://127.0.0.1:1234", modelName: "saved-model", asrProvider: "mock", asrBaseUrl: "", asrModelName: "", apiKeyConfigured: true, connectionStatus: "unknown" };
    const { default: ModelSettingsPage } = await loadModule("../../app/model-settings-page.tsx", {
      react: harness.hooks,
      antd,
      "@ant-design/icons": new Proxy({}, { get: () => placeholder }),
      "./api-client": {
        loadEnvironmentCheck: async () => undefined,
        loadModelSettings: async () => saved,
        clearModelApiKey: async () => { clears += 1; return { ...saved, apiKeyConfigured: false }; },
        saveModelSettings: async () => { saves += 1; throw new Error("不应保存草稿"); },
        testModelConnection: async (input) => ({ ...input, ok: true, models: [], message: "测试连接成功", checkedAt: "2026-09-09T00:00:00Z" }),
      },
    });
    harness.render(ModelSettingsPage);
    await harness.settle();
    values = { provider: draftProvider, baseUrl: "unfinished-address", modelName: "", asrProvider: "llamacpp", asrBaseUrl: "unfinished-asr", asrModelName: "draft-asr", apiKey: "draft-key-only" };
    const draft = { ...values };
    const assertSavedCard = (tree, status = "尚未检测") => {
      const card = elements(tree).find((item) => item.props.className === "content-card model-status-card");
      const field = (label) => renderedText(elements(card).find((item) => item.props.label === label));
      assert.equal(field("当前模型"), saved.modelName);
      assert.equal(field("服务地址"), saved.baseUrl);
      assert.equal(field("ASR 服务"), "模拟转写（不调用外部服务）");
      assert.equal(field("连接状态"), status);
      assert.match(renderedText(elements(card).find((item) => item.props.className === "model-status-card__head")), /LM Studio/);
    };
    const beforeClear = harness.render(ModelSettingsPage);
    assertSavedCard(beforeClear);
    const clear = elements(beforeClear).find((item) => item.type === Popconfirm);
    assert.equal(clear.props.disabled, false, "a draft provider change must not disable clearing a saved key");
    clear.props.onConfirm();
    await harness.settle();
    assert.equal(clears, 1);
    assert.equal(saves, 0);
    assert.equal(validations, 0);
    assert.deepEqual(values, draft, "draft values must stay on the form");
    const afterClear = harness.render(ModelSettingsPage);
    assertSavedCard(afterClear);
    const after = elements(afterClear).find((item) => item.type === Popconfirm);
    assert.equal(after.props.disabled, true, "the returned saved-key state must update immediately");
    assert.deepEqual(notices, ["已清除服务端保存的 API Key"]);

    // Testing a draft connection must also leave the active configuration alone.
    allowValidation = true;
    values = { ...values, baseUrl: "http://127.0.0.1:19999", modelName: "draft-model" };
    const testConnection = (tree) => elements(tree).find((item) => typeof item.props.onClick === "function" && renderedText(item) === "测试连接");
    testConnection(harness.render(ModelSettingsPage)).props.onClick();
    await harness.settle();
    const afterDraftTest = harness.render(ModelSettingsPage);
    assertSavedCard(afterDraftTest);
    assert.match(renderedText(afterDraftTest), /表单配置测试：测试连接成功/);

    values = { ...saved, apiKey: "" };
    testConnection(harness.render(ModelSettingsPage)).props.onClick();
    await harness.settle();
    assertSavedCard(harness.render(ModelSettingsPage), "已连接");
  }
});

test("evaluation can select one analysis run while omitted selection stays server-resolved", async () => {
  const requests = [];
  await withClientFetch(async (_url, init) => {
    requests.push(JSON.parse(init.body));
    return Response.json({ evaluation: {}, message: "评测完成" });
  }, async () => {
    await client.runEvaluation("package-test", [{ quote: "原文" }], "run-test");
    await client.runEvaluation("package-test", [{ quote: "原文" }]);
  });
  assert.equal(requests[0].analysis_run_id, "run-test");
  assert.equal(Object.hasOwn(requests[1], "analysis_run_id"), false);
});

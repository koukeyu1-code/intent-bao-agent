import type {
  AdoptionStage,
  DictionaryCollection,
  FormalIntentStatus,
  IntentObservation,
  MeetingPackage,
  ModelConnectionResult,
  ModelProvider,
  ModelSettings,
  ModelSettingsInput,
  MvpDataset,
  ProcessingTask,
} from "./types";
import type {
  PlatformCascadeSelection,
  PlatformTaxonomy,
} from "./platform-taxonomy";
import {
  assertConfirmedStage,
  buildIntentConfirmPayload,
} from "./intent-view-models";
import {
  bearerHeaders,
  clearAccessToken,
  resolveAuthApiBase,
} from "./auth-api";

export const API_BASE = resolveAuthApiBase();

export class ApiClientError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
  }
}

const notifyUnauthorized = (status: number) => {
  if (status === 401 && typeof window !== "undefined") {
    clearAccessToken();
    window.dispatchEvent(new Event("intent-bao:auth-expired"));
  }
};

const functionIdByName: Record<string, string> = {
  产品: "function-product",
  采购: "function-purchase",
  开发: "function-development",
  PM: "function-pm",
  品质: "function-quality",
};

const customerIdByName: Record<string, string> = {
  客户A: "customer-a",
  客户B: "customer-b",
  客户C: "customer-c",
  客户D: "customer-d",
  客户E: "customer-e",
};

// 读请求默认超时；写请求（POST/PATCH/PUT/DELETE）默认放宽到 20 秒——
// 本机模型分析期间后端响应变慢，3.5 秒会在服务端已写入的情况下提示
// "创建失败"，诱发重复建包或版本冲突。
const DEFAULT_READ_TIMEOUT_MS = 8000;
const DEFAULT_WRITE_TIMEOUT_MS = 20_000;

const defaultTimeoutFor = (init?: RequestInit): number => {
  const method = (init?.method ?? "GET").toUpperCase();
  return method === "GET" || method === "HEAD"
    ? DEFAULT_READ_TIMEOUT_MS
    : DEFAULT_WRITE_TIMEOUT_MS;
};

const request = async <T>(
  path: string,
  init?: RequestInit,
  timeoutMs?: number,
): Promise<T> => {
  const method = (init?.method ?? "GET").toUpperCase();
  const effectiveTimeout = timeoutMs ?? defaultTimeoutFor(init);
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), effectiveTimeout);

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      cache: "no-store",
      credentials: "omit",
      headers: {
        "Content-Type": "application/json",
        ...bearerHeaders(),
        ...(init?.headers ?? {}),
      },
      signal: controller.signal,
    });
    if (!response.ok) {
      notifyUnauthorized(response.status);
      const errorBody = (await response.json().catch(() => null)) as
        | { detail?: string | unknown[] | Record<string, unknown> }
        | null;
      const rawDetail = errorBody?.detail;
      const reason =
        typeof rawDetail === "string"
          ? rawDetail
          : rawDetail !== undefined && rawDetail !== null
            ? JSON.stringify(rawDetail)
            : `API ${response.status}`;
      throw new ApiClientError(reason, response.status);
    }
    return (await response.json()) as T;
  } catch (error) {
    // 超时中止：写请求的结果未知（服务端可能已写入），必须提示用户
    // 刷新核对而不是引导盲目重试；读请求重试是安全的。
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiClientError(
        method === "GET" || method === "HEAD"
          ? `请求超时（${Math.round(effectiveTimeout / 1000)}秒），请稍后刷新重试`
          : `请求超时（${Math.round(effectiveTimeout / 1000)}秒）：服务可能已处理，结果未知，请刷新核对后再操作`,
        0,
      );
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
};

interface ModelSettingsApiResponse {
  provider: ModelProvider;
  base_url?: string | null;
  baseUrl?: string | null;
  model_name?: string | null;
  model?: string | null;
  modelName?: string | null;
  asr?: {
    provider?: "mock" | "llamacpp" | string | null;
    base_url?: string | null;
    model_name?: string | null;
    enabled?: boolean | null;
  } | null;
  api_key_configured?: boolean;
  apiKeyConfigured?: boolean;
  connection_status?: ModelSettings["connectionStatus"];
  connectionStatus?: ModelSettings["connectionStatus"];
  last_checked_at?: string | null;
  lastCheckedAt?: string | null;
  last_error?: string | null;
  lastError?: string | null;
}

interface ModelConnectionApiResponse {
  ok: boolean;
  provider?: ModelProvider;
  base_url?: string | null;
  baseUrl?: string | null;
  model_name?: string | null;
  model?: string | null;
  modelName?: string | null;
  models?: Array<string | { name?: string; model?: string }>;
  message: string;
  latency_ms?: number | null;
  latencyMs?: number | null;
  checked_at?: string | null;
  checkedAt?: string | null;
}

const modelSettingsRequestBody = (input: ModelSettingsInput) => ({
  provider: input.provider,
  base_url: input.baseUrl.trim(),
  model_name: input.modelName.trim(),
  asr_provider: input.asrProvider ?? "mock",
  asr_base_url: (input.asrBaseUrl ?? "").trim(),
  asr_model_name: (input.asrModelName ?? "").trim(),
  ...(input.apiKey ? { api_key: input.apiKey } : {}),
  ...(input.clearApiKey ? { clear_api_key: true } : {}),
});

const modelConnectionRequestBody = (input: ModelSettingsInput) => ({
  provider: input.provider,
  base_url: input.baseUrl.trim(),
  ...(input.modelName.trim()
    ? { model_name: input.modelName.trim() }
    : {}),
  ...(input.apiKey ? { api_key: input.apiKey } : {}),
});

const normalizeModelSettings = (
  item: ModelSettingsApiResponse,
): ModelSettings => {
  const asr = item.asr;
  const asrProvider = asr?.provider === "llamacpp" ? "llamacpp" : "mock";
  return {
    provider: item.provider,
    baseUrl: item.base_url ?? item.baseUrl ?? "",
    modelName: item.model_name ?? item.model ?? item.modelName ?? "",
    asrProvider,
    asrBaseUrl: asr?.base_url ?? "",
    asrModelName: asr?.model_name ?? "",
    apiKeyConfigured:
      item.api_key_configured ?? item.apiKeyConfigured ?? false,
    connectionStatus:
      item.connection_status ?? item.connectionStatus ?? "unknown",
    ...(item.last_checked_at ?? item.lastCheckedAt
      ? { lastCheckedAt: item.last_checked_at ?? item.lastCheckedAt ?? undefined }
      : {}),
    ...(item.last_error ?? item.lastError
      ? { lastError: item.last_error ?? item.lastError ?? undefined }
      : {}),
  };
};

const normalizeModelConnection = (
  item: ModelConnectionApiResponse,
  fallback: ModelSettingsInput,
): ModelConnectionResult => ({
  ok: item.ok,
  provider: item.provider ?? fallback.provider,
  baseUrl: item.base_url ?? item.baseUrl ?? fallback.baseUrl,
  modelName:
    item.model_name ?? item.model ?? item.modelName ?? fallback.modelName,
  models: (item.models ?? [])
    .map((model) =>
      typeof model === "string" ? model : (model.name ?? model.model ?? ""),
    )
    .filter(Boolean),
  message: item.message,
  ...(item.latency_ms ?? item.latencyMs
    ? { latencyMs: item.latency_ms ?? item.latencyMs ?? undefined }
    : {}),
  ...(item.checked_at ?? item.checkedAt
    ? { checkedAt: item.checked_at ?? item.checkedAt ?? undefined }
    : {}),
});

export const loadModelSettings = async (): Promise<ModelSettings> =>
  normalizeModelSettings(
    await request<ModelSettingsApiResponse>(
      "/model-settings",
      undefined,
      10_000,
    ),
  );

export const saveModelSettings = async (
  input: ModelSettingsInput,
): Promise<ModelSettings> =>
  normalizeModelSettings(
    await request<ModelSettingsApiResponse>(
      "/model-settings",
      {
        method: "PUT",
        body: JSON.stringify(modelSettingsRequestBody(input)),
      },
      15_000,
    ),
  );

export const clearModelApiKey = async (): Promise<ModelSettings> =>
  normalizeModelSettings(
    await request<ModelSettingsApiResponse>(
      "/model-settings/api-key",
      { method: "DELETE" },
      15_000,
    ),
  );

export const testModelConnection = async (
  input: ModelSettingsInput,
): Promise<ModelConnectionResult> => {
  const result = await request<ModelConnectionApiResponse>(
    "/model-settings/test",
    {
      method: "POST",
      body: JSON.stringify(modelConnectionRequestBody(input)),
    },
    30_000,
  );
  return normalizeModelConnection(result, input);
};

interface ModelListItem {
  name?: string;
  model?: string;
}

export const listAvailableModels = async (
  input: ModelSettingsInput,
): Promise<string[]> => {
  const result = await request<
    | {
        items?: Array<string | ModelListItem>;
        models?: Array<string | ModelListItem>;
      }
    | Array<string | ModelListItem>
  >(
    "/model-settings/models",
    {
      method: "POST",
      body: JSON.stringify(modelConnectionRequestBody(input)),
    },
    30_000,
  );
  const models = Array.isArray(result)
    ? result
    : (result.models ?? result.items ?? []);
  return models
    .map((model) =>
      typeof model === "string" ? model : (model.name ?? model.model ?? ""),
    )
    .filter(Boolean);
};

interface EntityRef {
  id: string;
  name: string;
  display_name?: string;
}

interface FormalStatusApiItem {
  id: string;
  status_key: string;
  customer: EntityRef;
  technology: EntityRef;
  generic_platform?: EntityRef | null;
  platform_path?: {
    mappingStatus?: "mapped" | "legacy_pending" | "unmapped";
    platformVariantId?: string | null;
    path?: {
      technology?: { id?: string; name?: string } | null;
      level2?: { valueId?: string; value?: string } | null;
      level3?: { valueId?: string; value?: string } | null;
    };
  } | null;
  customer_project?: EntityRef | null;
  function: EntityRef;
  attitude: { level: 1 | 2 | 3 | 4 | 5 | null; label: string };
  adoption_stage?: AdoptionStage | null;
  is_supporter: boolean;
  is_resistance: boolean;
  conditions: string[];
  bottlenecks: string[];
  evidence?: Array<{ id: string }>;
  evidence_count: number;
  effective_at: string;
  confirmed_at: string;
  confirmed_by: string;
  version_no: number;
  is_current: boolean;
}

interface IntentConfirmResponse {
  observation: {
    id: string;
    suggested_adoption_stage?: AdoptionStage | null;
  };
  formal_status: FormalStatusApiItem;
  message: string;
}

const normalizeFormalStatus = (
  item: FormalStatusApiItem,
): FormalIntentStatus => ({
  id: item.id,
  statusKey: item.status_key,
  customer: item.customer.display_name ?? item.customer.name,
  technology: item.technology.name,
  technologyId:
    item.platform_path?.path?.technology?.id ?? item.technology.id,
  platform: item.generic_platform?.name ?? "平台待确认",
  ...(item.platform_path?.path?.level2?.valueId
    ? { level2ValueId: item.platform_path.path.level2.valueId }
    : {}),
  ...(item.platform_path?.path?.level3?.valueId
    ? { level3ValueId: item.platform_path.path.level3.valueId }
    : {}),
  ...(item.platform_path?.platformVariantId
    ? { platformLeafId: item.platform_path.platformVariantId }
    : {}),
  platformMappingStatus:
    item.platform_path?.mappingStatus ?? "legacy_pending",
  customerProject: item.customer_project?.name ?? "客户项目待确认",
  functionName: item.function.name,
  attitude: item.attitude.level,
  adoptionStage: item.adoption_stage ?? "未明确／未进入",
  isSupporter: item.is_supporter,
  isResistance: item.is_resistance,
  conditions: item.conditions ?? [],
  blocker: item.bottlenecks?.join("、") || "暂无明确卡点",
  evidenceId: item.evidence?.[0]?.id ?? "",
  evidenceCount: item.evidence_count,
  effectiveAt: item.effective_at,
  confirmedAt: item.confirmed_at,
  confirmedBy: item.confirmed_by,
  version: item.version_no,
  isCurrent: item.is_current,
});

export const loadBootstrapDataset = async (): Promise<MvpDataset> => {
  const [data, formalResponse, platformTaxonomy] = await Promise.all([
    request<
      Partial<MvpDataset> & {
        meeting_packages?: { items?: MeetingPackage[] };
        master_data?: DictionaryCollection;
      }
    >("/bootstrap", undefined, 30_000),
    request<{ items: FormalStatusApiItem[]; total: number }>(
      "/formal-intent-statuses?current_only=true",
    ),
    loadPlatformTaxonomy(),
  ]);
  const normalized: Partial<MvpDataset> = {
    ...data,
    meetings: data.meetings ?? data.meeting_packages?.items,
    dictionaries: data.dictionaries ?? data.master_data,
    // API mode must never silently mix the built-in demo competitor records
    // into customer data.  Until the backend exposes a persisted competitor
    // dataset, an absent field is an honest empty state.
    competitiveIntents: data.competitiveIntents ?? [],
    formalIntents: formalResponse.items
      .filter((item) => item.is_current)
      .map(normalizeFormalStatus),
    platformTaxonomy,
  };
  if (Array.isArray(data.intents) && Array.isArray(data.evidence)) {
    const meetingByEvidence = new Map(
      data.evidence.map((item) => [item.id, item.meetingId]),
    );
    normalized.intents = data.intents.map((item) => ({
      ...item,
      meetingId: item.meetingId ?? meetingByEvidence.get(item.evidenceId),
    }));
  }
  if (
    !Array.isArray(normalized.meetings) ||
    !Array.isArray(normalized.speakers) ||
    !Array.isArray(normalized.intents) ||
    !Array.isArray(normalized.formalIntents) ||
    !Array.isArray(normalized.competitiveIntents) ||
    !Array.isArray(normalized.evidence) ||
    !Array.isArray(normalized.history) ||
    !normalized.dictionaries ||
    !normalized.platformTaxonomy
  ) {
    throw new Error("API数据结构不完整");
  }
  return normalized as MvpDataset;
};

export const updateIntentStatus = async (
  intent: IntentObservation,
  action: "confirm" | "return" | "unknown",
  reviewNote?: string,
) => {
  if (action === "return") {
    return request(`/intent-observations/${intent.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        version: intent.version ?? 1,
        suggested_attitude: intent.attitude,
        suggested_adoption_stage: intent.adoptionStage,
        bottlenecks:
          intent.blocker && intent.blocker !== "暂无明确卡点"
            ? [intent.blocker]
            : [],
        uncertainty_reason: "退回补充：需要补齐材料或修正AI建议后再确认",
        status: "退回补充",
      }),
    });
  }
  const response = await request<IntentConfirmResponse>(
    `/intent-observations/${intent.id}/confirm`,
    {
      method: "POST",
      body: JSON.stringify(buildIntentConfirmPayload(intent, action, reviewNote)),
    },
  );
  if (action === "confirm") {
    assertConfirmedStage(
      intent.adoptionStage,
      response.observation.suggested_adoption_stage,
    );
    if (response.formal_status.is_current) {
      assertConfirmedStage(
        intent.adoptionStage,
        response.formal_status.adoption_stage,
      );
    }
  }
  return response;
};

export const updateIntentSuggestion = async (
  intent: IntentObservation,
) =>
  request<{ version: number }>(`/intent-observations/${intent.id}`, {
    method: "PATCH",
    body: JSON.stringify({
      version: intent.version ?? 1,
      suggested_attitude: intent.attitude,
      suggested_adoption_stage: intent.adoptionStage,
      support_reason: intent.supportReason,
      highlight_spec: intent.highlightSpec ?? null,
      bottlenecks:
        intent.blocker && intent.blocker !== "暂无明确卡点"
          ? [intent.blocker]
          : [],
      uncertainty_reason:
        intent.attitude === null ? "人工修改为未知，等待补充证据" : null,
    }),
  });

export const updateIntentReviewDraft = async (
  intent: IntentObservation,
  selection?: PlatformCascadeSelection & {
    level2NewValue?: string;
    level3NewValue?: string;
  },
) =>
  request<{
    observation: { version: number };
    platform_draft: { id: string; status: "pending" | "confirmed" } | null;
    message: string;
  }>(`/intent-observations/${intent.id}/review-draft`, {
    method: "PUT",
    body: JSON.stringify({
      version: intent.version ?? 1,
      suggested_attitude: intent.attitude,
      suggested_adoption_stage: intent.adoptionStage,
      support_reason: intent.supportReason,
      highlight_spec: intent.highlightSpec?.trim() || null,
      bottlenecks:
        intent.blocker && intent.blocker !== "暂无明确卡点"
          ? [intent.blocker]
          : [],
      uncertainty_reason:
        intent.attitude === null ? "人工修改为未知，等待补充证据" : null,
      ...(selection
        ? {
            platform: {
              technology_id: selection.technologyId,
              ...(selection.level2NewValue
                ? { level2_new_value: selection.level2NewValue }
                : { level2_value_id: selection.level2ValueId }),
              ...(selection.level3NewValue
                ? { level3_new_value: selection.level3NewValue }
                : selection.level3ValueId
                  ? { level3_value_id: selection.level3ValueId }
                  : {}),
            },
          }
        : {}),
    }),
  });

export const confirmSpeakerIdentity = async (
  speakerId: string,
  version: number,
  identity: {
    alias: string;
    role: string;
    organization: string;
    functionName: string;
  },
) =>
  request(`/speakers/${speakerId}/confirm`, {
    method: "PATCH",
    body: JSON.stringify({
      version,
      display_name: identity.alias,
      role: identity.role,
      organization: identity.organization,
      function_id:
        functionIdByName[identity.functionName] ?? identity.functionName,
      identity_granularity: "完整",
      actor: "本地产品规划负责人",
    }),
  });

export const createMeetingPackage = async (
  payload: {
    title: string;
    customer: string;
    date: string;
    dataClass: string;
    attendeeName?: string;
    attendeeRole?: string;
    attendeeOrganization?: string;
    attendeeFunction?: string;
  },
  clientRequestId?: string,
): Promise<{ id: string }> =>
  request<{ id: string }>("/meeting-packages", {
    method: "POST",
    body: JSON.stringify({
      // 幂等键：超时后用户重试同一逻辑提交时复用同一个键，服务端
      // 返回已创建的资料包而不是重复建包。
      client_request_id: clientRequestId ?? null,
      project_id: "project-mnt",
      customer_id: customerIdByName[payload.customer] ?? payload.customer,
      title: payload.title,
      meeting_date: payload.date,
      data_scope: payload.dataClass,
      attendees: payload.attendeeName
        ? [
            {
              display_name: payload.attendeeName,
              role: payload.attendeeRole || null,
              organization: payload.attendeeOrganization || null,
              function_id: payload.attendeeFunction
                ? (functionIdByName[payload.attendeeFunction] ??
                  payload.attendeeFunction)
                : null,
            },
          ]
        : [],
    }),
  });

export const materialTypeForFile = (fileName: string): string => {
  const lowerName = fileName.toLowerCase();
  if (lowerName.endsWith(".mp4")) return "录屏";
  if (/\.(mp3|wav|m4a)$/.test(lowerName)) return "录音";
  if (lowerName.endsWith(".pdf")) return "会议PDF";
  if (lowerName.includes("纪要")) return "人工纪要";
  return "转写稿";
};

export const uploadMeetingMaterial = async (
  meetingId: string,
  file: File,
) => {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("material_type", materialTypeForFile(file.name));

  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 120_000);
  try {
    const response = await fetch(
      `${API_BASE}/meeting-packages/${meetingId}/files`,
      {
        method: "POST",
        body: formData,
        cache: "no-store",
        credentials: "omit",
        headers: bearerHeaders(),
        signal: controller.signal,
      },
    );
    if (!response.ok) {
      notifyUnauthorized(response.status);
      const errorBody = (await response.json().catch(() => null)) as
        | { detail?: string }
        | null;
      throw new Error(
        errorBody?.detail ?? `文件上传失败（API ${response.status}）`,
      );
    }
    return response.json();
  } finally {
    window.clearTimeout(timer);
  }
};

export interface AnalyzeMeetingResult {
  task_id: string;
  cancelled_runs?: number;
  analysis_run: {
    id: string;
    package_id: string;
    adapter: string;
    model_name: string;
    status: string;
    result_summary?: Record<string, unknown> | null;
    started_at?: string;
    finished_at?: string;
  };
  stages: Array<{ stage: string; status: string; progress: number }>;
  message: string;
}

export const analyzeMeetingPackage = async (
  meetingId: string,
  force = false,
): Promise<AnalyzeMeetingResult> =>
  request<AnalyzeMeetingResult>(
    `/meeting-packages/${meetingId}/analyze`,
    {
      method: "POST",
      body: JSON.stringify({ force }),
    },
    30_000,
  );

export const cancelMeetingPackageAnalysis = async (
  meetingId: string,
): Promise<{ package_id: string; cancelled_runs: number; message: string }> =>
  request(`/meeting-packages/${meetingId}/analyze/cancel`, {
    method: "POST",
  });

type ProcessingTaskApiResponse = Partial<ProcessingTask> & {
  id: string;
  package_id?: string;
  analysis_run_id?: string;
  analysis_run_created_at?: string;
  analysis_run_status?: string;
  created_at?: string;
  stage_order?: number;
  attempt_count?: number;
  started_at?: string;
  finished_at?: string;
  error_message?: string;
  updated_at?: string;
};

export const loadPackageTasks = async (
  meetingId: string,
): Promise<{ items: ProcessingTask[]; total: number }> => {
  const response = await request<{ items: ProcessingTaskApiResponse[]; total: number }>(
    `/meeting-packages/${meetingId}/tasks`, undefined, 10_000,
  );
  return {
    total: response.total,
    items: response.items.map((item) => ({
      id: item.id,
      packageId: item.package_id ?? item.packageId ?? meetingId,
      analysisRunId: item.analysis_run_id ?? item.analysisRunId ?? "",
      analysisRunCreatedAt: item.analysis_run_created_at ?? item.analysisRunCreatedAt,
      analysisRunStatus: item.analysis_run_status ?? item.analysisRunStatus,
      createdAt: item.created_at ?? item.createdAt,
      stage: item.stage ?? "未知阶段",
      stageOrder: item.stage_order ?? item.stageOrder ?? 0,
      status: item.status ?? "待处理",
      progress: item.progress ?? 0,
      attemptCount: item.attempt_count ?? item.attemptCount ?? 0,
      startedAt: item.started_at ?? item.startedAt,
      finishedAt: item.finished_at ?? item.finishedAt,
      errorMessage: item.error_message ?? item.errorMessage,
      updatedAt: item.updated_at ?? item.updatedAt ?? "",
      version: item.version ?? 1,
      result: item.result,
    })),
  };
};

export const exportMeetingPackage = async (
  meetingId: string,
  format: "csv" | "xlsx" | "md",
): Promise<Blob> => {
  const response = await fetch(
    `${API_BASE}/meeting-packages/${meetingId}/export?format=${format}`,
    {
      cache: "no-store",
      credentials: "omit",
      headers: { ...bearerHeaders() },
    },
  );
  if (!response.ok) {
    notifyUnauthorized(response.status);
    const errorBody = (await response.json().catch(() => null)) as
      | { detail?: string }
      | null;
    throw new ApiClientError(
      errorBody?.detail ?? `导出失败（API ${response.status}）`,
      response.status,
    );
  }
  return response.blob();
};

export const mergeSpeakers = async (payload: {
  packageId: string;
  keepSpeakerId: string;
  mergeSpeakerIds: string[];
}) =>
  request(
    "/speakers/merge",
    {
      method: "PATCH",
      body: JSON.stringify({
        package_id: payload.packageId,
        keep_speaker_id: payload.keepSpeakerId,
        merge_speaker_ids: payload.mergeSpeakerIds,
      }),
    },
    15_000,
  );

export const splitSpeaker = async (payload: {
  packageId: string;
  speakerId: string;
  segmentIds: string[];
}) =>
  request(
    `/speakers/${payload.speakerId}/split`,
    {
      method: "POST",
      body: JSON.stringify({
        package_id: payload.packageId,
        segment_ids: payload.segmentIds,
      }),
    },
    15_000,
  );

export interface SpeakerSegmentDetail {
  id: string;
  start_ms: number;
  end_ms: number;
  transcript: string;
}

export const loadSpeakerDetail = async (
  speakerId: string,
): Promise<{ segments: SpeakerSegmentDetail[] }> =>
  request(`/speakers/${speakerId}`, undefined, 10_000);

export interface GoldenIntentRecord {
  quote: string;
  technology_id?: string | null;
  function_id?: string | null;
  attitude_level?: number | null;
  adoption_stage?: string | null;
}

export interface EvaluationReport {
  package_id: string;
  analysis_run_id: string | null;
  generated_at: string;
  total: number;
  correct: number;
  missed: number;
  structured_accuracy: number | null;
  traceable: number;
  traceable_rate: number | null;
  unknown_total: number;
  unknown_missed: number;
  unknown_misjudged: number;
  unknown_misjudge_rate: number | null;
  field_accuracy: {
    technology: number | null;
    function: number | null;
    attitude: number | null;
    stage: number | null;
  };
  errors: Array<Record<string, unknown>>;
  error_count: number;
  notice: string;
}

export const runEvaluation = async (
  packageId: string,
  golden: GoldenIntentRecord[],
  analysisRunId?: string,
): Promise<{ evaluation: EvaluationReport; message: string }> =>
  request(
    "/eval/runs",
    {
      method: "POST",
      body: JSON.stringify({
        package_id: packageId,
        golden,
        ...(analysisRunId ? { analysis_run_id: analysisRunId } : {}),
      }),
    },
    60_000,
  );

export interface EnvironmentCheck {
  ffmpeg: boolean;
  ffprobe: boolean;
  intent_provider: string;
  intent_model: string;
  asr: {
    provider: string;
    base_url: string;
    model_name: string;
    enabled: boolean;
  };
  asr_healthy: boolean;
  disk_free_gb: number;
  ok: boolean;
}

export const loadEnvironmentCheck = async (): Promise<EnvironmentCheck> =>
  request("/env-check", undefined, 10_000);

export interface TaxonomyImportIssue {
  code: string;
  message: string;
  sheet?: string;
  row?: number;
  column?: string;
  value?: string;
}

export interface TaxonomyImportPreview {
  valid: boolean;
  file: { name: string; sizeBytes: number; sha256: string };
  preview?: {
    sheetName: string;
    headerRow: number;
    counts: {
      technologies: number;
      level2Values: number;
      level3Values: number;
      platforms: number;
    };
    technologies: Array<{
      name: string;
      level2?: { name: string; values: Array<{ name: string }> } | null;
      level3?: { name: string; values: Array<{ name: string }> } | null;
      platforms: Array<{ name: string }>;
    }>;
  } | null;
  diff?: Record<string, unknown> | null;
  errors: TaxonomyImportIssue[];
  warnings: TaxonomyImportIssue[];
  previewToken?: string;
}

interface TaxonomyImportPreviewApi
  extends Omit<TaxonomyImportPreview, "previewToken"> {
  preview_token?: string;
}

export const loadPlatformTaxonomy = async (): Promise<PlatformTaxonomy> =>
  request<PlatformTaxonomy>("/platform-taxonomy", undefined, 10_000);

export const preflightTaxonomyExcel = async (
  file: File,
): Promise<TaxonomyImportPreview> => {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`${API_BASE}/taxonomy/import/preflight`, {
    method: "POST",
    body: formData,
    cache: "no-store",
    credentials: "omit",
    headers: bearerHeaders(),
  });
  const body = (await response.json().catch(() => null)) as
    | TaxonomyImportPreviewApi
    | { detail?: string }
    | null;
  if (!response.ok) {
    notifyUnauthorized(response.status);
    throw new Error(
      body && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : `Excel预检失败（API ${response.status}）`,
    );
  }
  const item = body as TaxonomyImportPreviewApi;
  return { ...item, previewToken: item.preview_token };
};

export const commitTaxonomyImport = async (
  previewToken: string,
  fileSha256: string,
) =>
  request<{
    message: string;
    idempotent: boolean;
    result: Record<string, unknown>;
    taxonomy: PlatformTaxonomy;
  }>(
    "/taxonomy/import/commit",
    {
      method: "POST",
      body: JSON.stringify({
        preview_token: previewToken,
        file_sha256: fileSha256,
      }),
    },
    30_000,
  );

export const saveObservationPlatformDraft = async (
  observationId: string,
  version: number,
  selection: PlatformCascadeSelection & {
    level2NewValue?: string;
    level3NewValue?: string;
  },
) =>
  request<{
    observation: { version: number };
    platform_draft: {
      id: string;
      status: "pending" | "confirmed";
    };
    message: string;
  }>(`/intent-observations/${observationId}/platform-draft`, {
    method: "POST",
    body: JSON.stringify({
      version,
      technology_id: selection.technologyId,
      ...(selection.level2NewValue
        ? { level2_new_value: selection.level2NewValue }
        : { level2_value_id: selection.level2ValueId }),
      ...(selection.level3NewValue
        ? { level3_new_value: selection.level3NewValue }
        : selection.level3ValueId
          ? { level3_value_id: selection.level3ValueId }
          : {}),
    }),
  });

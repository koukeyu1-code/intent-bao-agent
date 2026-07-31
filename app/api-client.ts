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
} from "./types";
import {
  assertConfirmedStage,
  buildIntentConfirmPayload,
} from "./intent-view-models";

export const API_BASE =
  process.env.NEXT_PUBLIC_INTENT_BAO_API_BASE ??
  "http://127.0.0.1:8000/api";

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

const request = async <T>(
  path: string,
  init?: RequestInit,
  timeoutMs = 3500,
): Promise<T> => {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
      signal: controller.signal,
    });
    if (!response.ok) {
      const errorBody = (await response.json().catch(() => null)) as
        | { detail?: string }
        | null;
      throw new Error(errorBody?.detail ?? `API ${response.status}`);
    }
    return (await response.json()) as T;
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
): ModelSettings => ({
  provider: item.provider,
  baseUrl: item.base_url ?? item.baseUrl ?? "",
  modelName: item.model_name ?? item.model ?? item.modelName ?? "",
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
});

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
  platform: item.generic_platform?.name ?? "平台待确认",
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
  const [data, formalResponse] = await Promise.all([
    request<
      Partial<MvpDataset> & {
        meeting_packages?: { items?: MeetingPackage[] };
        master_data?: DictionaryCollection;
      }
    >("/bootstrap"),
    request<{ items: FormalStatusApiItem[]; total: number }>(
      "/formal-intent-statuses?current_only=true",
    ),
  ]);
  const normalized: Partial<MvpDataset> = {
    ...data,
    meetings: data.meetings ?? data.meeting_packages?.items,
    dictionaries: data.dictionaries ?? data.master_data,
    formalIntents: formalResponse.items
      .filter((item) => item.is_current)
      .map(normalizeFormalStatus),
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
    !Array.isArray(normalized.evidence) ||
    !Array.isArray(normalized.history) ||
    !normalized.dictionaries
  ) {
    throw new Error("API数据结构不完整");
  }
  return normalized as MvpDataset;
};

export const updateIntentStatus = async (
  intent: IntentObservation,
  action: "confirm" | "return" | "unknown",
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
      body: JSON.stringify(buildIntentConfirmPayload(intent, action)),
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
  request(`/intent-observations/${intent.id}`, {
    method: "PATCH",
    body: JSON.stringify({
      version: intent.version ?? 1,
      suggested_attitude: intent.attitude,
      suggested_adoption_stage: intent.adoptionStage,
      support_reason: intent.supportReason,
      bottlenecks:
        intent.blocker && intent.blocker !== "暂无明确卡点"
          ? [intent.blocker]
          : [],
      uncertainty_reason:
        intent.attitude === null ? "人工修改为未知，等待补充证据" : null,
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

export const createMeetingPackage = async (payload: {
  title: string;
  customer: string;
  date: string;
  dataClass: string;
  attendeeName?: string;
  attendeeRole?: string;
  attendeeOrganization?: string;
  attendeeFunction?: string;
}): Promise<{ id: string }> =>
  request<{ id: string }>("/meeting-packages", {
    method: "POST",
    body: JSON.stringify({
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
        signal: controller.signal,
      },
    );
    if (!response.ok) {
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

export const analyzeMeetingPackage = async (
  meetingId: string,
  force = false,
) =>
  request<{
    task_id: string;
    message: string;
  }>(
    `/meeting-packages/${meetingId}/analyze`,
    {
      method: "POST",
      body: JSON.stringify({ force }),
    },
    180_000,
  );

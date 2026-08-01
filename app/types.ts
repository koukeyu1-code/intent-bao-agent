import type { PlatformTaxonomy } from "./platform-taxonomy";

export type ViewKey =
  | "overview"
  | "meetings"
  | "speakers"
  | "insights"
  | "customer"
  | "technology"
  | "competition"
  | "history"
  | "dictionary"
  | "settings";

export type DataSourceMode = "loading" | "api" | "mock";

export type ModelProvider = "mock" | "ollama";

export type ModelConnectionStatus =
  | "unknown"
  | "checking"
  | "connected"
  | "error";

export interface ModelSettings {
  provider: ModelProvider;
  baseUrl: string;
  modelName: string;
  apiKeyConfigured: boolean;
  connectionStatus: ModelConnectionStatus;
  lastCheckedAt?: string;
  lastError?: string;
}

export interface ModelSettingsInput {
  provider: ModelProvider;
  baseUrl: string;
  modelName: string;
  apiKey?: string;
  clearApiKey?: boolean;
}

export interface ModelConnectionResult {
  ok: boolean;
  provider: ModelProvider;
  baseUrl: string;
  modelName: string;
  models: string[];
  message: string;
  latencyMs?: number;
  checkedAt?: string;
}

export type AttitudeLevel = 1 | 2 | 3 | 4 | 5 | null;

export type AdoptionStage =
  | "未明确／未进入"
  | "初步了解"
  | "内部评估"
  | "样品／技术验证"
  | "产品项目规划"
  | "确认导入"
  | "暂停／放弃";

export type MaterialType =
  | "录音"
  | "录屏"
  | "转写稿"
  | "人工纪要"
  | "会议PDF";

export type MeetingStatus =
  | "草稿"
  | "待预检"
  | "待补资料"
  | "待分析"
  | "分析中"
  | "待身份校验"
  | "待意向确认"
  | "已确认"
  | "处理失败";

export type IntentStatus =
  | "AI草稿"
  | "待确认"
  | "已确认"
  | "退回补充"
  | "无法判断";

export interface MeetingPackage {
  id: string;
  title: string;
  customer: string;
  date: string;
  owner: string;
  duration: string;
  materials: MaterialType[];
  materialFiles?: Array<{
    id: string;
    name: string;
    type: MaterialType;
    sizeBytes: number;
    parseStatus: string;
  }>;
  materialCompleteness?: "待检查" | "完整" | "资料不完整";
  technologies: string[];
  platforms: string[];
  dataClass: "模拟数据" | "授权脱敏数据" | "企业真实敏感数据";
  status: MeetingStatus;
  progress: number;
  currentStep: string;
  updatedAt: string;
  warning?: string;
}

export interface SpeakerSegment {
  id: string;
  version: number;
  meetingId: string;
  speakerCode: string;
  alias: string;
  role: string;
  organization: string;
  functionName: string;
  confidence: number;
  confirmed: boolean;
  timeRange: string;
  excerpt: string;
  recommendationBasis: string;
}

export interface Evidence {
  id: string;
  meetingId: string;
  meetingTitle: string;
  meetingDate: string;
  speaker: string;
  speakerFunction: string;
  quote: string;
  timecode: string;
  pdfPage?: number;
  frameLabel?: string;
  sourceType: "原始录音" | "人工纪要" | "会议PDF" | "录屏画面";
  aiReason: string;
}

export interface IntentObservation {
  id: string;
  version: number;
  meetingId?: string;
  customer: string;
  technology: string;
  technologyId?: string;
  platform: string;
  level2ValueId?: string;
  level3ValueId?: string;
  platformLeafId?: string;
  platformMappingStatus?: "mapped" | "legacy_pending" | "unmapped";
  platformDraft?: {
    id: string;
    status: "pending";
    technologyId: string;
    technologyName?: string;
    level2ValueId: string;
    level2Value: string;
    level2IsNew: boolean;
    level3ValueId: string;
    level3Value: string;
    level3IsNew: boolean;
    displayName: string;
  } | null;
  customerProject: string;
  functionName: string;
  person: string;
  attitude: AttitudeLevel;
  adoptionStage: AdoptionStage;
  confidence: number;
  decisionImpact: "推动" | "评估" | "把关" | "否决" | "使用" | "未知";
  supportReason: string;
  blocker: string;
  status: IntentStatus;
  evidenceId: string;
  conflict: boolean;
  updatedAt: string;
  confirmedBy?: string;
}

export interface FormalIntentStatus {
  id: string;
  statusKey: string;
  customer: string;
  technology: string;
  technologyId?: string;
  platform: string;
  level2ValueId?: string;
  level3ValueId?: string;
  platformLeafId?: string;
  platformMappingStatus?: "mapped" | "legacy_pending" | "unmapped";
  customerProject: string;
  functionName: string;
  attitude: AttitudeLevel;
  adoptionStage: AdoptionStage;
  isSupporter: boolean;
  isResistance: boolean;
  conditions: string[];
  blocker: string;
  evidenceId: string;
  evidenceCount: number;
  effectiveAt: string;
  confirmedAt: string;
  confirmedBy: string;
  version: number;
  isCurrent: boolean;
}

export interface CompetitiveIntentStatus {
  id: string;
  dataClass: "模拟数据";
  competitor: string;
  product: string;
  customer: string;
  mappedTechnology: string;
  technologyId?: string;
  platform: string;
  level2ValueId?: string;
  level3ValueId?: string;
  platformLeafId?: string;
  platformMappingStatus?: "mapped" | "legacy_pending" | "unmapped";
  customerProject: string;
  functionName: string;
  attitude: AttitudeLevel;
  adoptionStage: AdoptionStage;
  blocker: string;
  evidenceSummary: string;
  evidenceCount: number;
  updatedAt: string;
}

export interface HistoryEvent {
  id: string;
  intentId: string;
  date: string;
  title: string;
  description: string;
  attitude: AttitudeLevel;
  adoptionStage: AdoptionStage;
  actor: string;
  evidenceId: string;
  version: string;
}

export interface DictionaryItem {
  id: string;
  name: string;
  code: string;
  aliases: string[];
  parentIds?: string[];
  status: "启用" | "停用";
  updatedAt: string;
  note: string;
}

export interface DictionaryCollection {
  technologies: DictionaryItem[];
  customers: DictionaryItem[];
  platforms: DictionaryItem[];
  projects: DictionaryItem[];
  functions: DictionaryItem[];
}

export interface MvpDataset {
  meetings: MeetingPackage[];
  speakers: SpeakerSegment[];
  intents: IntentObservation[];
  formalIntents: FormalIntentStatus[];
  competitiveIntents: CompetitiveIntentStatus[];
  evidence: Evidence[];
  history: HistoryEvent[];
  dictionaries: DictionaryCollection;
  platformTaxonomy: PlatformTaxonomy;
}

export const attitudeMeta: Record<
  number | "unknown",
  { label: string; shortLabel: string; color: string; background: string }
> = {
  5: {
    label: "主动推动",
    shortLabel: "5 · 主动推动",
    color: "#0c6b54",
    background: "#dff5ed",
  },
  4: {
    label: "明确支持",
    shortLabel: "4 · 明确支持",
    color: "#1b6da8",
    background: "#e3f2fc",
  },
  3: {
    label: "有条件／观望",
    shortLabel: "3 · 有条件",
    color: "#9a6700",
    background: "#fff4d7",
  },
  2: {
    label: "暂不支持",
    shortLabel: "2 · 暂不支持",
    color: "#b45309",
    background: "#ffedd7",
  },
  1: {
    label: "明确反对／阻断",
    shortLabel: "1 · 反对／阻断",
    color: "#b42318",
    background: "#fee8e6",
  },
  unknown: {
    label: "未知",
    shortLabel: "— · 未知",
    color: "#667085",
    background: "#edf0f4",
  },
};

export const adoptionStageColor: Record<AdoptionStage, string> = {
  "未明确／未进入": "default",
  初步了解: "cyan",
  内部评估: "blue",
  "样品／技术验证": "purple",
  产品项目规划: "geekblue",
  确认导入: "green",
  "暂停／放弃": "red",
};

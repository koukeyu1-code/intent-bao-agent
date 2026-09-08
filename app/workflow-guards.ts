import type {
  PlatformCascadeSelection,
  PlatformTaxonomy,
} from "./platform-taxonomy";
import type { AdoptionStage, Evidence, IntentObservation } from "./types";

export interface SpeakerIdentityDraft {
  alias: string;
  role: string;
  organization: string;
  functionName: string;
}

export interface PlatformDraftInput extends PlatformCascadeSelection {
  level2NewValue?: string;
  level3NewValue?: string;
}

export type UploadState = "待上传" | "上传中" | "已上传" | "失败";

const ADOPTION_STAGES = new Set<AdoptionStage>([
  "未明确／未进入",
  "初步了解",
  "内部评估",
  "样品／技术验证",
  "产品项目规划",
  "确认导入",
  "暂停／放弃",
]);

const cleanRequired = (value: string, label: string) => {
  const cleaned = value.trim();
  if (!cleaned) {
    throw new Error(`请补全${label}`);
  }
  return cleaned;
};

export function prepareSpeakerIdentity(
  input: SpeakerIdentityDraft,
): SpeakerIdentityDraft {
  return {
    alias: cleanRequired(input.alias, "姓名／脱敏称呼"),
    role: cleanRequired(input.role, "角色／职务"),
    organization: cleanRequired(input.organization, "组织"),
    functionName: cleanRequired(input.functionName, "客户职能"),
  };
}

export function assertIntentReadyForSave(input: {
  id: string;
  version?: number;
  adoptionStage: string;
}) {
  if (!input.id.trim()) {
    throw new Error("意向编号缺失，请刷新后重试");
  }
  if (!Number.isInteger(input.version) || (input.version ?? 0) < 1) {
    throw new Error("意向版本无效，请刷新后重试");
  }
  if (!ADOPTION_STAGES.has(input.adoptionStage as AdoptionStage)) {
    throw new Error("采用阶段无效，请重新选择");
  }
}

export type IntentConfirmationReview =
  | { mode: "batch" }
  | { mode: "manual"; note: string };

export function intentConfirmationRisks(
  intent: Pick<IntentObservation, "confidence" | "conflict">,
) {
  return [
    ...(intent.confidence < 0.75 ? ["AI置信度低于75%"] : []),
    ...(intent.conflict ? ["存在历史／内部意见冲突"] : []),
  ];
}

export function assertIntentConfirmationEvidence(
  intent: IntentObservation,
  evidence: Evidence | undefined,
) {
  assertIntentReadyForSave(intent);
  if (!intent.identityConfirmed) {
    throw new Error("请先确认说话人身份与客户职能，再确认意向");
  }
  if (intent.attitude === null) {
    throw new Error("当前态度未知，请先核对证据并修改态度，或标记为无法判断");
  }
  if (
    !intent.evidenceId ||
    evidence?.id !== intent.evidenceId ||
    !evidence.quote?.trim() ||
    (intent.meetingId && evidence.meetingId !== intent.meetingId)
  ) {
    throw new Error("缺少本会议的可追溯证据原文，请补齐证据后再确认");
  }
}

export function prepareIntentConfirmation(
  intent: IntentObservation,
  evidence: Evidence | undefined,
  review: IntentConfirmationReview,
): string | undefined {
  assertIntentConfirmationEvidence(intent, evidence);
  if (review.mode === "batch") {
    if (intentConfirmationRisks(intent).length) {
      throw new Error("低置信度或存在冲突的结果需点击行内“确认”，逐条人工复核");
    }
    return undefined;
  }
  const note = cleanRequired(review.note, "复核理由");
  if (note.length > 2000) {
    throw new Error("复核理由不能超过2000个字符");
  }
  return note;
}

const cleanOptional = (value?: string) => value?.trim() || undefined;

const activeValues = (
  technology: PlatformTaxonomy["technologies"][number],
  levelNumber: 2 | 3,
) =>
  technology.levels
    .find((level) => level.level === levelNumber)
    ?.values.filter((item) => item.status !== "inactive") ?? [];

const activeLeafPairs = (
  technology: PlatformTaxonomy["technologies"][number],
) => {
  const persisted = (technology.leaves ?? []).filter(
    (leaf) => leaf.status !== "inactive",
  );
  if (persisted.length) return persisted;
  return activeValues(technology, 2).flatMap((level2) =>
    activeValues(technology, 3).map((level3) => ({
      level2ValueId: level2.id,
      level3ValueId: level3.id,
    })),
  );
};

export function preparePlatformDraftForSave(
  taxonomy: PlatformTaxonomy,
  input: PlatformDraftInput,
): PlatformDraftInput {
  const technologyId = cleanOptional(input.technologyId);
  const level2ValueId = cleanOptional(input.level2ValueId);
  const level3ValueId = cleanOptional(input.level3ValueId);
  const level2NewValue = cleanOptional(input.level2NewValue);
  const level3NewValue = cleanOptional(input.level3NewValue);

  if (!technologyId) {
    throw new Error("产品平台路径必须选择技术");
  }
  if (Boolean(level2ValueId) === Boolean(level2NewValue)) {
    throw new Error("二级内容必须且只能选择现有值或新增一个值");
  }
  if (Boolean(level3ValueId) === Boolean(level3NewValue)) {
    throw new Error("三级内容必须且只能选择现有值或新增一个值");
  }
  if ((level2NewValue?.length ?? 0) > 200 || (level3NewValue?.length ?? 0) > 200) {
    throw new Error("新增平台内容不能超过200个字符");
  }

  const technology = taxonomy.technologies.find(
    (item) => item.id === technologyId && item.status !== "inactive",
  );
  if (!technology) {
    throw new Error("所选技术已停用或不存在，请重新选择");
  }

  const level2Exists = level2ValueId
    ? activeValues(technology, 2).some((item) => item.id === level2ValueId)
    : true;
  const level3Exists = level3ValueId
    ? activeValues(technology, 3).some((item) => item.id === level3ValueId)
    : true;
  if (!level2Exists || !level3Exists) {
    throw new Error("所选平台内容已停用或不存在，请重新选择");
  }
  if (
    level2ValueId &&
    level3ValueId &&
    !activeLeafPairs(technology).some(
      (leaf) =>
        leaf.level2ValueId === level2ValueId &&
        leaf.level3ValueId === level3ValueId,
    )
  ) {
    throw new Error("所选二级与三级内容不能形成有效产品平台");
  }

  return {
    technologyId,
    ...(level2NewValue ? { level2NewValue } : { level2ValueId }),
    ...(level3NewValue ? { level3NewValue } : { level3ValueId }),
  };
}

export function resolveRequestedItems<T extends { id: string }>(
  items: T[],
  requestedIds: string[],
) {
  const uniqueIds = Array.from(new Set(requestedIds));
  const itemById = new Map(items.map((item) => [item.id, item]));
  const targets = uniqueIds.flatMap((id) => {
    const item = itemById.get(id);
    return item ? [item] : [];
  });
  const targetIds = new Set(targets.map((item) => item.id));
  return {
    targets,
    missingIds: uniqueIds.filter((id) => !targetIds.has(id)),
  };
}

export function summarizeSettledWrites(
  results: PromiseSettledResult<unknown>[],
) {
  const failedCount = results.filter(
    (result) => result.status === "rejected",
  ).length;
  return {
    attemptedCount: results.length,
    failedCount,
    successCount: results.length - failedCount,
  };
}

export function pendingUploadIds(
  fileIds: string[],
  statuses: Record<string, UploadState>,
) {
  return fileIds.filter((id) => statuses[id] !== "已上传");
}

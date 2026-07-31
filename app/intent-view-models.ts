import type {
  AdoptionStage,
  FormalIntentStatus,
  IntentObservation,
  MvpDataset,
} from "./types.ts";

export const ADOPTION_STAGE_ORDER: AdoptionStage[] = [
  "未明确／未进入",
  "初步了解",
  "内部评估",
  "样品／技术验证",
  "产品项目规划",
  "确认导入",
  "暂停／放弃",
];

export interface FormalScopeGroup {
  key: string;
  customer: string;
  technology: string;
  platform: string;
  customerProject: string;
  items: FormalIntentStatus[];
  byFunction: Map<string, FormalIntentStatus>;
}

export interface IntentConfirmPayload {
  version: number;
  attitude_level: number | null;
  adoption_stage?: AdoptionStage;
  is_supporter: boolean;
  is_resistance: boolean;
  bottlenecks: string[];
  note: string;
}

export const adoptionScopeKey = (
  item: Pick<
    FormalIntentStatus,
    "customer" | "technology" | "platform" | "customerProject"
  >,
) =>
  JSON.stringify([
    item.customer,
    item.technology,
    item.platform,
    item.customerProject,
  ]);

export const formalScopeKey = adoptionScopeKey;

const isNewerFormalStatus = (
  candidate: FormalIntentStatus,
  current: FormalIntentStatus,
) =>
  candidate.confirmedAt > current.confirmedAt ||
  (candidate.confirmedAt === current.confirmedAt &&
    candidate.version > current.version);

export const latestFormalStatus = (
  items: FormalIntentStatus[],
): FormalIntentStatus | undefined => {
  let latest: FormalIntentStatus | undefined;
  items.forEach((item) => {
    if (!latest || isNewerFormalStatus(item, latest)) latest = item;
  });
  return latest;
};

export const groupFormalIntentsByScope = (
  intents: FormalIntentStatus[],
  options: {
    technology?: string;
    customer?: string;
    customerOrder?: string[];
    includeEmptyCustomers?: boolean;
  } = {},
): FormalScopeGroup[] => {
  const scoped = intents.filter(
    (item) =>
      (!options.technology || item.technology === options.technology) &&
      (!options.customer || item.customer === options.customer),
  );
  const groups = new Map<string, FormalScopeGroup>();

  scoped.forEach((item) => {
    const key = formalScopeKey(item);
    const group = groups.get(key) ?? {
      key,
      customer: item.customer,
      technology: item.technology,
      platform: item.platform,
      customerProject: item.customerProject,
      items: [],
      byFunction: new Map<string, FormalIntentStatus>(),
    };
    group.items.push(item);
    const current = group.byFunction.get(item.functionName);
    if (!current || isNewerFormalStatus(item, current)) {
      group.byFunction.set(item.functionName, item);
    }
    groups.set(key, group);
  });

  if (options.includeEmptyCustomers) {
    (options.customerOrder ?? []).forEach((customer) => {
      if ([...groups.values()].some((group) => group.customer === customer)) {
        return;
      }
      const placeholder = {
        customer,
        technology: options.technology ?? "",
        platform: "尚无平台状态",
        customerProject: "尚无客户项目状态",
      };
      const key = formalScopeKey(placeholder);
      groups.set(key, {
        key,
        ...placeholder,
        items: [],
        byFunction: new Map<string, FormalIntentStatus>(),
      });
    });
  }

  const customerRank = new Map(
    (options.customerOrder ?? []).map((customer, index) => [customer, index]),
  );
  return [...groups.values()].sort((left, right) => {
    const leftRank = customerRank.get(left.customer) ?? Number.MAX_SAFE_INTEGER;
    const rightRank =
      customerRank.get(right.customer) ?? Number.MAX_SAFE_INTEGER;
    return (
      leftRank - rightRank ||
      left.customer.localeCompare(right.customer, "zh-CN") ||
      left.platform.localeCompare(right.platform, "zh-CN") ||
      left.customerProject.localeCompare(right.customerProject, "zh-CN")
    );
  });
};

export const getCurrentAdoptionStates = (
  intents: FormalIntentStatus[],
): FormalIntentStatus[] => {
  const latestByScope = new Map<string, FormalIntentStatus>();
  intents.forEach((item) => {
    const key = adoptionScopeKey(item);
    const current = latestByScope.get(key);
    if (!current || isNewerFormalStatus(item, current)) {
      latestByScope.set(key, item);
    }
  });
  return [...latestByScope.values()];
};

export const computeOverviewMetrics = (
  data: Pick<MvpDataset, "intents" | "formalIntents" | "evidence">,
) => {
  const pendingIntents = data.intents.filter((item) =>
    ["AI草稿", "待确认"].includes(item.status),
  );
  const currentRisks = data.formalIntents.filter(
    (item) =>
      item.attitude === null ||
      item.attitude === 1 ||
      item.attitude === 2 ||
      item.isResistance,
  );
  const pendingRisks = pendingIntents.filter(
    (item) =>
      item.attitude === null ||
      item.attitude === 1 ||
      item.attitude === 2 ||
      item.conflict,
  );
  const stageCounts = Object.fromEntries(
    ADOPTION_STAGE_ORDER.map((stage) => [stage, 0]),
  ) as Record<AdoptionStage, number>;
  getCurrentAdoptionStates(data.formalIntents).forEach((item) => {
    stageCounts[item.adoptionStage] += 1;
  });

  return {
    pendingCount: pendingIntents.length,
    evidenceCount: data.evidence.length,
    confirmedCount: data.formalIntents.length,
    activeTechnologyCount: new Set(
      data.formalIntents.map((item) => item.technology),
    ).size,
    riskCount: currentRisks.length + pendingRisks.length,
    stageCounts,
  };
};

export const buildIntentConfirmPayload = (
  intent: IntentObservation,
  action: "confirm" | "unknown",
): IntentConfirmPayload => ({
  version: intent.version ?? 1,
  attitude_level: action === "unknown" ? null : intent.attitude,
  ...(action === "confirm"
    ? { adoption_stage: intent.adoptionStage }
    : {}),
  is_supporter:
    action === "confirm" &&
    (intent.attitude === 4 || intent.attitude === 5) &&
    intent.decisionImpact === "推动",
  is_resistance:
    action === "confirm" &&
    (intent.attitude === 1 || intent.attitude === 2),
  bottlenecks:
    intent.blocker && intent.blocker !== "暂无明确卡点"
      ? [intent.blocker]
      : [],
  note:
    action === "unknown"
      ? "当前证据不足，保留未知；不改写采用阶段"
      : "产品规划负责人确认",
});

export const assertConfirmedStage = (
  submittedStage: AdoptionStage,
  responseStage: AdoptionStage | null | undefined,
) => {
  if (responseStage !== submittedStage) {
    throw new Error(
      `确认结果采用阶段不一致：提交“${submittedStage}”，服务端返回“${
        responseStage ?? "未返回"
      }”`,
    );
  }
};

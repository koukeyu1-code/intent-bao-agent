export type PlatformTaxonomyStatus = "active" | "pending" | "inactive";

export interface PlatformTaxonomyValue {
  id: string;
  name: string;
  code?: string;
  status?: PlatformTaxonomyStatus;
  sortOrder?: number;
}

export interface PlatformTaxonomyLevel {
  id: string;
  level: 2 | 3;
  name: string;
  values: PlatformTaxonomyValue[];
}

export interface PlatformTaxonomyLeaf {
  id: string;
  displayName: string;
  level2ValueId: string;
  level3ValueId: string;
  status?: PlatformTaxonomyStatus;
}

export interface TechnologyPlatformTaxonomy {
  id: string;
  name: string;
  code?: string;
  status?: PlatformTaxonomyStatus;
  levels: PlatformTaxonomyLevel[];
  leaves?: PlatformTaxonomyLeaf[];
}

export interface PlatformTaxonomy {
  revision?: string | number;
  technologies: TechnologyPlatformTaxonomy[];
}

export interface PlatformCascadeSelection {
  technologyId?: string;
  level2ValueId?: string;
  level3ValueId?: string;
}

export interface PlatformCascadeOption {
  value: string;
  label: string;
}

export interface PlatformCascadeModel {
  selection: PlatformCascadeSelection;
  technology?: TechnologyPlatformTaxonomy;
  level2?: PlatformTaxonomyLevel;
  level3?: PlatformTaxonomyLevel;
  technologyOptions: PlatformCascadeOption[];
  level2Options: PlatformCascadeOption[];
  level3Options: PlatformCascadeOption[];
  matchingLeaves: PlatformTaxonomyLeaf[];
  isExactLeaf: boolean;
}

/**
 * The minimum path fields needed to apply one cascade selection to either the
 * technology view or the competition view. IDs are deliberately used instead
 * of display names so renamed menu content does not change historical matches.
 */
export interface PlatformPathRecord {
  technologyId?: string | null;
  level2ValueId?: string | null;
  level3ValueId?: string | null;
  platformLeafId?: string | null;
}

export interface PlatformMatchOptions {
  /** Excludes competitor rows that have not been mapped to a concrete leaf. */
  requireConcretePlatform?: boolean;
}

const DEFAULT_STATUS: PlatformTaxonomyStatus = "active";

function isActive(status?: PlatformTaxonomyStatus) {
  return (status ?? DEFAULT_STATUS) === "active";
}

function compareTaxonomyItems(
  left: { name: string; sortOrder?: number },
  right: { name: string; sortOrder?: number },
) {
  const orderDifference =
    (left.sortOrder ?? Number.MAX_SAFE_INTEGER) -
    (right.sortOrder ?? Number.MAX_SAFE_INTEGER);
  return orderDifference || left.name.localeCompare(right.name, "zh-CN");
}

function activeValues(level?: PlatformTaxonomyLevel) {
  return (level?.values ?? [])
    .filter((value) => isActive(value.status))
    .toSorted(compareTaxonomyItems);
}

export function getPlatformLevel(
  technology: TechnologyPlatformTaxonomy | undefined,
  level: 2 | 3,
) {
  return technology?.levels.find((item) => item.level === level);
}

/**
 * Returns persisted leaves when supplied by the API. During a preview, where
 * leaves may not have been materialized yet, it deterministically derives the
 * Cartesian product of active level-2 and level-3 values.
 */
export function getActivePlatformLeaves(
  technology: TechnologyPlatformTaxonomy | undefined,
): PlatformTaxonomyLeaf[] {
  if (!technology) return [];

  const persistedLeaves = (technology.leaves ?? []).filter((leaf) =>
    isActive(leaf.status),
  );
  if (persistedLeaves.length > 0) return persistedLeaves;

  const level2Values = activeValues(getPlatformLevel(technology, 2));
  const level3Values = activeValues(getPlatformLevel(technology, 3));
  return level2Values.flatMap((level2Value) =>
    level3Values.map((level3Value) => ({
      id: `${technology.id}:${level2Value.id}:${level3Value.id}`,
      displayName: `${level2Value.name} ${level3Value.name}`,
      level2ValueId: level2Value.id,
      level3ValueId: level3Value.id,
      status: "active" as const,
    })),
  );
}

export function normalizePlatformSelection(
  taxonomy: PlatformTaxonomy,
  selection: PlatformCascadeSelection,
): PlatformCascadeSelection {
  const technology = taxonomy.technologies.find(
    (item) => item.id === selection.technologyId && isActive(item.status),
  );
  if (!technology) return {};

  const level2 = getPlatformLevel(technology, 2);
  const level2Value = activeValues(level2).find(
    (item) => item.id === selection.level2ValueId,
  );
  if (!level2Value) return { technologyId: technology.id };

  const level3 = getPlatformLevel(technology, 3);
  const candidateLeaves = getActivePlatformLeaves(technology).filter(
    (leaf) => leaf.level2ValueId === level2Value.id,
  );
  const allowedLevel3Ids = new Set(
    candidateLeaves.map((leaf) => leaf.level3ValueId),
  );
  const level3Value = activeValues(level3).find(
    (item) =>
      item.id === selection.level3ValueId && allowedLevel3Ids.has(item.id),
  );
  if (!level3Value) {
    return {
      technologyId: technology.id,
      level2ValueId: level2Value.id,
    };
  }

  return {
    technologyId: technology.id,
    level2ValueId: level2Value.id,
    level3ValueId: level3Value.id,
  };
}

export function getPlatformCascadeModel(
  taxonomy: PlatformTaxonomy,
  requestedSelection: PlatformCascadeSelection,
): PlatformCascadeModel {
  const selection = normalizePlatformSelection(taxonomy, requestedSelection);
  const technology = taxonomy.technologies.find(
    (item) => item.id === selection.technologyId,
  );
  const level2 = getPlatformLevel(technology, 2);
  const level3 = getPlatformLevel(technology, 3);
  const leaves = getActivePlatformLeaves(technology);
  const matchingLeaves = leaves.filter(
    (leaf) =>
      (!selection.level2ValueId ||
        leaf.level2ValueId === selection.level2ValueId) &&
      (!selection.level3ValueId ||
        leaf.level3ValueId === selection.level3ValueId),
  );
  const allowedLevel3Ids = new Set(
    matchingLeaves.map((leaf) => leaf.level3ValueId),
  );

  return {
    selection,
    technology,
    level2,
    level3,
    technologyOptions: taxonomy.technologies
      .filter((item) => isActive(item.status))
      .map((item) => ({ value: item.id, label: item.name })),
    level2Options: activeValues(level2).map((item) => ({
      value: item.id,
      label: item.name,
    })),
    level3Options: selection.level2ValueId
      ? activeValues(level3)
          .filter((item) => allowedLevel3Ids.has(item.id))
          .map((item) => ({ value: item.id, label: item.name }))
      : [],
    matchingLeaves,
    isExactLeaf:
      Boolean(selection.level3ValueId) && matchingLeaves.length === 1,
  };
}

export function matchesPlatformSelection(
  record: PlatformPathRecord,
  taxonomy: PlatformTaxonomy,
  requestedSelection: PlatformCascadeSelection,
  options: PlatformMatchOptions = {},
) {
  const model = getPlatformCascadeModel(taxonomy, requestedSelection);
  const { selection } = model;
  if (!selection.technologyId) return false;
  if (record.technologyId !== selection.technologyId) return false;

  const leafIds = new Set(model.matchingLeaves.map((leaf) => leaf.id));
  const hasConcreteMapping = Boolean(
    record.platformLeafId && leafIds.has(record.platformLeafId),
  );
  if (options.requireConcretePlatform && !hasConcreteMapping) return false;

  if (
    selection.level2ValueId &&
    record.level2ValueId !== selection.level2ValueId
  ) {
    return false;
  }
  if (
    selection.level3ValueId &&
    record.level3ValueId !== selection.level3ValueId
  ) {
    return false;
  }
  return true;
}

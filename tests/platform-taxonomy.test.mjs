import assert from "node:assert/strict";
import test from "node:test";

import {
  getActivePlatformLeaves,
  getPlatformCascadeModel,
  matchesPlatformSelection,
  normalizePlatformSelection,
} from "../app/platform-taxonomy.ts";

const taxonomy = {
  revision: 7,
  technologies: [
    {
      id: "tech-triple-mode",
      name: "三模",
      levels: [
        {
          id: "menu-size",
          level: 2,
          name: "尺寸",
          values: [
            { id: "size-27", name: "27", sortOrder: 1 },
            { id: "size-315", name: "31.5", sortOrder: 2 },
            { id: "size-34-pending", name: "34", status: "pending" },
          ],
        },
        {
          id: "menu-resolution",
          level: 3,
          name: "分辨率",
          values: [
            { id: "resolution-qhd", name: "QHD", sortOrder: 1 },
            { id: "resolution-uhd", name: "UHD", sortOrder: 2 },
          ],
        },
      ],
    },
    {
      id: "tech-inactive",
      name: "停用技术",
      status: "inactive",
      levels: [],
    },
  ],
};

test("三模按两个独立集合生成 4 个具体平台，而不是按 Excel 行配对", () => {
  const leaves = getActivePlatformLeaves(taxonomy.technologies[0]);
  assert.equal(leaves.length, 4);
  assert.deepEqual(
    leaves.map((leaf) => leaf.displayName),
    ["27 QHD", "27 UHD", "31.5 QHD", "31.5 UHD"],
  );
});

test("技术、二级、三级选项按层级联动且使用动态菜单名称", () => {
  const model = getPlatformCascadeModel(taxonomy, {
    technologyId: "tech-triple-mode",
    level2ValueId: "size-27",
  });
  assert.equal(model.level2.name, "尺寸");
  assert.equal(model.level3.name, "分辨率");
  assert.deepEqual(model.level2Options.map((item) => item.label), ["27", "31.5"]);
  assert.deepEqual(model.level3Options.map((item) => item.label), ["QHD", "UHD"]);
  assert.equal(model.matchingLeaves.length, 2);
  assert.equal(model.isExactLeaf, false);
});

test("只选二级聚合全部三级，选择三级后精确命中一个叶子", () => {
  const aggregate = getPlatformCascadeModel(taxonomy, {
    technologyId: "tech-triple-mode",
    level2ValueId: "size-315",
  });
  assert.deepEqual(
    aggregate.matchingLeaves.map((leaf) => leaf.displayName),
    ["31.5 QHD", "31.5 UHD"],
  );

  const exact = getPlatformCascadeModel(taxonomy, {
    technologyId: "tech-triple-mode",
    level2ValueId: "size-315",
    level3ValueId: "resolution-uhd",
  });
  assert.equal(exact.isExactLeaf, true);
  assert.deepEqual(exact.matchingLeaves.map((leaf) => leaf.displayName), ["31.5 UHD"]);
});

test("技术变化或非法子选项会清理失效的下级选择", () => {
  assert.deepEqual(
    normalizePlatformSelection(taxonomy, {
      technologyId: "tech-triple-mode",
      level2ValueId: "missing",
      level3ValueId: "resolution-uhd",
    }),
    { technologyId: "tech-triple-mode" },
  );
  assert.deepEqual(
    normalizePlatformSelection(taxonomy, {
      technologyId: "tech-inactive",
      level2ValueId: "size-27",
    }),
    {},
  );
});

test("同一匹配函数可供技术与竞争视角复用，竞品可强制要求具体平台映射", () => {
  const selection = {
    technologyId: "tech-triple-mode",
    level2ValueId: "size-27",
  };
  const mapped = {
    technologyId: "tech-triple-mode",
    level2ValueId: "size-27",
    level3ValueId: "resolution-qhd",
    platformLeafId: "tech-triple-mode:size-27:resolution-qhd",
  };
  const unmappedCompetitor = {
    technologyId: "tech-triple-mode",
    level2ValueId: "size-27",
  };

  assert.equal(matchesPlatformSelection(mapped, taxonomy, selection), true);
  assert.equal(
    matchesPlatformSelection(mapped, taxonomy, selection, {
      requireConcretePlatform: true,
    }),
    true,
  );
  assert.equal(
    matchesPlatformSelection(unmappedCompetitor, taxonomy, selection, {
      requireConcretePlatform: true,
    }),
    false,
  );
});

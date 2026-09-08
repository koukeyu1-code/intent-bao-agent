# 意向宝 Agent 指引

> 面向显示器产品企划团队的客户意向智能体：把会议材料整理成"技术 × 客户 × 平台 × 职能"的证据化意向地图。本文件是下次会话恢复上下文的入口，不是第二份 README。

## 怎么跑起来（Windows）

```powershell
首次安装依赖.bat          # 装前端 pnpm 依赖 + 后端 venv
启动意向宝.bat            # 起前端(5173) + 后端(8000)，自动开浏览器（3000 落在本机 winnat 端口排除范围 2912-3011 内，监听会被 EACCES 拒绝，故固定用 5173）
停止意向宝.bat            # 停服务
重置意向宝登录密码.bat     # 重置 admin/planner 密码（随机或手动）
```

当前账号：admin（管理员）、planner（产品规划）。密码不写入仓库；首次启动时到
`server/data/config/initial-login-credentials/` 查看本机生成的临时凭据，随后请通过
`重置意向宝登录密码.bat` 轮换并删除临时凭据文件。

## 技术栈

- 前端：Next.js 16 + React 19 + TypeScript + Ant Design 6 + ECharts，构建用 vinext
- 后端：FastAPI + SQLAlchemy + SQLite + Pydantic（`server/`，venv 在 `server/.venv`）
- 模型：适配器模式，默认 `mock`，可切本地 Ollama；真实多模态（ASR/说话人分离/OCR）仅接口预留

## 目录与约定

- `app/` — 前端（主程序 `intent-bao-app.tsx`，约 4500 行，改动需谨慎）
- `server/app/` — 后端业务（`seed.py` 模拟数据源、`services.py` 业务逻辑、`taxonomy_service.py` 平台菜单树）
- `server/tests/` 后端测试（124 项）、`tests/` 前端测试（35 项）
- `server/data/` 运行时数据（已被 git 忽略，含数据库、上传文件、初始凭据）

## 不可触碰的红线（违反会破坏数据一致性或安全）

1. **密码不进源码**：重置走 `app.auth_admin --password-env` 或 `--generate`，绝不硬编码；凭据文件在 `server/data/config/initial-login-credentials/`（git 忽略）。
2. **mock 门禁**：非"模拟数据"属性的资料包禁止用 mock 适配器（`services.py` 已强制，违反返回 422）。
3. **改 seed.py 必须同步测试**：seed 是演示数据唯一源头，改技术/平台/会议后，`server/tests/` 里引用旧 ID 的断言和 `app/mock-data.ts` 都要同步，否则回归挂。
4. **真实敏感数据不得调公共模型**：方案硬约束，只允许本地模型或企业内网模型。
5. **taxonomy 树是 3×3 矩阵**：8 项技术各配 尺寸&分辨率(23.8FHD/27QHD/31.5UHD) × 刷新率(180Hz/240Hz/300Hz)，改结构要同步级联选择器逻辑。

## 当前状态和下一步

- **A档（演示）**：已完成。模拟数据核心闭环可演示，8 技术维度 + 规格树 + 说话人代称已生效。录视频用 admin 登录，主线演示"客户A·MiniLED背光"。
- **B档（8/6 真实试点）**：待办。独立 Worker、深度预检、一条非 mock 真实链路、评测框架。详见 `docs/技术维度更新数据蓝图_20260802.md`。
- **C档**：合规评审（真实敏感数据/声纹）需提前启动；不走公网故暂不需要 ICP 备案。

变更记录见 `docs/CHANGELOG_20260802.md`，验收边界见 `docs/P0_ACCEPTANCE.md`。

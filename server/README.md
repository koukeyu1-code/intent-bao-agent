# 意向宝智能体 MVP 后端

本目录实现《05-意向宝智能体软件开发方案》的首版后端闭环：

- 本地多角色权限 FastAPI + SQLite；
- 会议资料包、材料上传、持久化阶段任务；
- 说话人推荐与人工校验；
- AI 原始意向观察与人工确认后的正式职能状态分层；
- 客户技术采用阶段与正式状态追加版本；
- 技术视角、客户视角、历史和证据下钻接口；
- 自动初始化清晰标识的真实感模拟数据；
- 模型供应商适配器边界，默认 `mock`，可切换到回环地址上的本地 Ollama；
- 本地 Ollama 模型读取、连接测试、结构化输出校验和可追溯文本意向草稿。
- 管理员 Excel 预检／发布技术所属的平台二、三级菜单；
- 意向字段与平台草稿在同一事务内保存，避免部分更新；
- 分类型上传限额、资料包总量／磁盘余量保护和 DOCX 解压安全校验。

本版本不声称真实模型达到 85% 准确率。正式状态仍必须由产品规划负责人确认。

## 安装与启动

在 `server` 目录中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

打开：

- API 健康检查：<http://127.0.0.1:8000/api/health>
- Swagger：<http://127.0.0.1:8000/docs>
- OpenAPI：<http://127.0.0.1:8000/api/openapi.json>

默认数据写入：

```text
server/data/
  config/model-settings.json
  database/intent_bao.db
  projects/<project-id>/<package-id>/source/
  logs/
  temp/
```

上传接口不会把物理文件路径返回前端；证据通过受控 API 访问。
`model-settings.json` 只保存提供方、地址和模型名。API Key 只保存在当前后端
进程内存中，不写入配置文件或业务数据库。

## 主要 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST/GET | `/api/auth/login`、`/api/auth/me` | 获取 Bearer 会话并读取当前用户 |
| GET/PUT | `/api/model-settings` | 查询或保存当前模型配置；密钥只写不读 |
| POST | `/api/model-settings/test` | 测试本地 Ollama 和所选模型 |
| POST | `/api/model-settings/models` | 读取本地 Ollama 已安装模型 |
| GET | `/api/bootstrap` | 首页一次性初始化数据 |
| GET/POST | `/api/meeting-packages` | 查询或创建会议资料包 |
| POST | `/api/meeting-packages/{id}/files` | 上传材料 |
| POST | `/api/meeting-packages/{id}/analyze` | 使用当前活动适配器同步生成 AI 草稿 |
| GET | `/api/meeting-packages/{id}/tasks` | 查询分阶段任务进度 |
| GET | `/api/speakers?package_id=...` | 查询说话人 |
| PATCH/POST | `/api/speakers/{id}/confirm` | 带版本号校验说话人 |
| GET | `/api/intent-observations` | 查询 AI 原始观察 |
| PUT | `/api/intent-observations/{id}/review-draft` | 原子保存人工建议和平台草稿 |
| POST | `/api/intent-observations/{id}/confirm` | 人工确认并生成正式状态版本 |
| GET | `/api/formal-intent-statuses` | 查询正式职能状态 |
| GET | `/api/dashboard/summary` | 看板摘要 |
| GET | `/api/dashboard/technology-matrix` | 技术视角矩阵 |
| GET | `/api/dashboard/customer-matrix` | 客户视角矩阵 |
| GET | `/api/history` | 正式状态历史 |
| GET | `/api/evidence/{id}` | 证据详情 |
| GET | `/api/master-data` | 客户、技术、平台、项目、职能和别名 |
| GET | `/api/platform-taxonomy` | 查询技术及平台二、三级菜单和具体平台 |
| POST | `/api/taxonomy/import/preflight` | 管理员预检 Excel，不写数据库 |
| POST | `/api/taxonomy/import/commit` | 管理员发布预检通过的菜单版本 |

所有列表返回 `{"items": [...], "total": n}`。更新和确认接口携带 `version`，
版本过期时返回 HTTP 409，避免旧页面覆盖新结果。

当前 Ollama 分支能读取 TXT、MD、SRT、VTT、DOCX 和资料包备注。录音、录屏
与 PDF 仍需后续 ASR/OCR/视觉组件；系统不会把未解析材料伪装成模型结果。

## 测试

```powershell
python -m pytest -q
```

测试使用临时 SQLite 数据库和临时文件目录，不修改正式演示数据。

## 数据库迁移

应用启动和密码重置前会自动执行 Alembic 升级。已有 SQLite 数据库如需升级，
系统会先在同级 `backups/` 目录创建一致性备份；迁移或结构校验失败时恢复备份并
停止启动，避免带着不完整结构继续运行。仍可在维护窗口手动执行：

```powershell
python -m alembic upgrade head
```

进入多人/生产部署前，仍应冻结初始元数据迁移、补齐逐版升级演练，并评审是否
切换 PostgreSQL。

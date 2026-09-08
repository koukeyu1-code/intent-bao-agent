# 意向宝智能体（Intent-Bao Agent）

> 让客户声音照进产品规划

意向宝把客户会议材料（录音、录屏、PDF、纪要）组织成可追溯、可确认的产品规划意向地图：AI 负责"听懂、看懂并建议"，业务负责人负责"确认结论"。全部模型在本地运行，企业真实敏感数据不出本机。

## 架构与双模型分工

后端（FastAPI）按 8 个阶段编排分析任务：文件预检 → 文档解析 → 音轨与转写 → 说话人分离 → 画面与PDF解析 → 资料对齐 → 身份辅助匹配 → 意向抽取。分析任务异步执行：接口立即返回，后台单 Worker 串行处理，前端轮询进度。

| 模型 | 位置 | 用途 |
| --- | --- | --- |
| Qwen3-ASR-1.7B（llama.cpp） | models/qwen3-asr/ + models/llama.cpp/ | 语音转写 |
| FunASR（fsmn-vad + cam++） | models/funasr/ | 说话人分离（谁说了什么） |
| Qwen3.6-27B（Ollama） | 自行导入（见下） | 意向抽取、资料对齐、身份推荐 |

## 目录结构

```text
app/                前端（React / Next.js）
server/            后端（FastAPI + SQLite），依赖清单 server/requirements.txt
models/             本地模型与运行库（随仓库分发，建议 Git LFS）
  qwen3-asr/       Qwen3-ASR GGUF + mmproj
  llama.cpp/       llama-server 运行时
  funasr/          FunASR 说话人分离模型
  ollama/          27B 大模型导入文件 Modelfile（GGUF 需自行下载）
samples/           演示素材（唐诗朗诵、三人会议录音、演示录屏、样例 PDF）
scripts/           启动/停止/安装/ASR 服务脚本
runtime/           运行期目录（后端入口、日志、pid，自动生成）
docs/              开发方案、开题报告、开发文档、验收清单
```

## 环境要求

- Windows 10/11（脚本为 PowerShell）
- Python 3.11+（安装时勾选 Add to PATH）
- Node.js 22+ 与 pnpm（npm install -g pnpm）
- 推荐 NVIDIA 显卡（CUDA）；无显卡也能跑，只是慢
- 硬盘剩余空间：约 8GB（依赖 + 内置模型）；若再导入 27B 大模型需额外约 20GB

## 第一步：准备模型

### 内置模型（随仓库分发）

models/qwen3-asr/、models/llama.cpp/、models/funasr/ 已包含运行所需文件。注意这些文件较大（GGUF 2GB、CUDA 运行库 512MB 等），从代码仓库获取时请启用 **Git LFS**（仓库已配置 .gitattributes），或从网盘/内网下载后按目录结构放回。

### 27B 意向抽取大模型（需自行下载，不在仓库内）

该模型原始 GGUF 约 17GB，不适合直接进代码仓库，请单独分发：

1. 下载 Qwen3.6-27B-Fable-Fus-711-UnHeretic-NM-DAU-NEO-MAX-NEO-Q4_K_M.gguf（Q4_K_M 量化版）；
2. 放入 models/ollama/ 目录；
3. 在该目录执行：

```powershell
ollama create qwen3.6-27b -f Modelfile-qwen3.6-27b
```

4. 启动项目后，在页面「模型设置」中选"本地 Ollama"，模型名填 qwen3.6-27b:latest 并保存。

> 未导入 27B 模型也不影响体验：系统仍可用内置 ASR/说话人分离跑转写与分离；意向抽取会回退到模拟结果（明确标注）。

## 第二步：安装依赖（新电脑）

双击 首次安装依赖.bat，脚本会自动：

1. 创建后端 Python 虚拟环境 server/.venv；
2. 安装 PyTorch（优先 CUDA 版，失败自动回退 CPU 版）与后端依赖（server/requirements.txt）；
3. 安装前端依赖（pnpm install）；
4. 通过 winget 自动安装 FFmpeg（缺失时）；
5. 检查 Tesseract OCR（中文包 chi_sim）、Ollama、内置模型，缺失时给出黄色警告与指引。

也可以手动执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-local.ps1
```

## 第三步：启动与停止

```text
启动意向宝.bat    一键启动（自动拉起 ASR 语音服务 8099、后端 8000、前端 5173）
停止意向宝.bat    一键停止
```

启动后浏览器打开 http://localhost:5173，后端接口文档在 http://127.0.0.1:8000/docs。也可手动执行 scripts/start-local.ps1 / scripts/stop-local.ps1。前端默认 5173 而非 3000：本机 Windows 的 winnat 端口排除范围（2912-3011）覆盖 3000，监听会被拒绝（EACCES）；其他机器如需改回 3000，可去掉 bat 中的 `-FrontendPort 5173` 参数，先用 `netsh interface ipv4 show excludedportrange protocol=tcp` 确认 3000 未被保留。

### 首次登录

- 本地账号为 admin（管理员）与 planner（普通用户），源码不提供默认密码。
- 可设置环境变量 INTENT_BAO_ADMIN_PASSWORD / INTENT_BAO_USER_PASSWORD 指定初始密码；未设置时后端生成强随机密码，启动窗口会提示凭据文件位置（server/data/config/initial-login-credentials/，该目录已被 Git 忽略）。
- 凭据文件保存到个人密码管理器后请删除；账号创建后环境变量不再影响其密码。

## 使用流程

1. 模型设置：确认 Ollama 与 ASR（llama.cpp http://127.0.0.1:8099，模型名 qwen3-asr）已保存，健康检测"已连接"；
2. 资料上传：新建会议资料包，上传录音/录屏/PDF/纪要；无 PDF 时填写例外原因；
3. 启动分析：立即返回列表，任务后台执行，列表实时显示 8 阶段进度，完成后自动提示；
4. 身份校验：对自动分离出的 Speaker A/B/C 逐个人工标注姓名、角色、组织、职能；
5. 意向确认：行内“确认”展示原文、风险、态度与采用阶段，填写复核理由后形成正式职能状态；低置信度（低于 75%）或存在冲突的结果必须逐条复核，不能批量确认。身份未确认、证据缺失或态度未知时仍需先补齐；
6. 看板与导出：客情总览/技术/客户视角查看正式状态；资料包详情可导出 CSV/Excel/Markdown；
7. 模型评测（可选）：默认评测最近成功的分析批次，使用不可变 AI 快照比较金标准；报告包含实际批次、漏检数和未知样本漏检数。未知误判率以全部未知金标准样本为分母，漏检不算正确，也不计作明确态度误判。已人工修改但缺少完整原始快照的旧记录需重新分析后评测，不能用当前人工结论补作 AI 原值。API `POST /api/eval/runs` 可传 `analysis_run_id` 指定本资料包的成功批次。

演示素材在 samples/：three_speakers.wav（三说话人录音，体验分离）、test_audio_tangpoems.mp3（单人唐诗朗诵）、test_screen_recording.mp4（含语音的演示录屏）、test_real_chinese_doc.pdf（中文样例 PDF）。

启用真实 ASR 后，同包全部录音和录屏音轨会逐文件转写，保留各自的来源与时间轴；
不同文件、不同分析批次中的同名说话人标签不会自动合并，需人工核对身份。所有录屏分别抽帧，
图片按分析批次和材料隔离保存。分段切片或转写失败最多尝试两次，仍失败时任务会
标记“处理失败”，记录材料及失败时间段，并阻止本次分析发布意向。修复服务或材料后
通过资料包重试全量分析；不会把缺片结果标成 100% 完成。

录屏关键帧保存与播放器时间轴一致的真实帧时间，旧产物的空时间戳需重新分析后补齐。
服务重启后，总分析和未完成阶段会一起转为失败，保留已完成阶段与中断原因；通过资料包重试。
人工拆分说话人后，新身份可继续校验；仅将原文和时间证据明确属于所选片段的意向迁移过去，
职能重新确认。来源跨批次且无法确定、或旧说话人可能混入失败分析片段时，拆分会明确拒绝。

意向编辑支持亮点规格新增、修改与清空。模型设置的“清除密钥”使用独立管理员接口
`DELETE /api/model-settings/api-key`，只清除当前后端进程中的密钥，不应用表单草稿；
环境变量提供的密钥仍会在后端重启时重新加载。注销请求失败也会清除本地身份并返回登录页。
CSV 导出对可能被表格软件解释为公式的文本添加文本前缀，XLSX 将字符串明确写为文本单元格；
原始证据保留在数据库中，导出不会执行证据中的公式。

## 测试

```powershell
# 后端（在 server 目录）
.venv\Scripts\python.exe -m pytest -q

# 前端（在项目根目录，需 Node）
node --test tests/*.test.mjs

# 类型检查（在项目根目录，同时检查本地前端和 Cloudflare 模板）
pnpm run typecheck
# 也可分别运行 pnpm run typecheck:local / pnpm run typecheck:cloudflare
```

本地前端使用 `tsconfig.json`，Cloudflare 模板使用 `tsconfig.cloudflare.json`，后者覆盖
`worker/`、`db/` 和 `examples/d1/`。两套检查隔离浏览器与 Workers 的全局类型。
`worker/worker-configuration.d.ts` 由现有 Wrangler 生成并纳入源码管理，不要手工编辑；
调整 `wrangler.types.jsonc` 中的绑定、兼容日期/标志或升级 Wrangler 后，运行
`pnpm run types:cloudflare` 更新声明，再运行 `pnpm run typecheck`。Cloudflare 检查会先验证声明是否过期。
`wrangler.types.jsonc` 仅用于生成 DB、ASSETS、IMAGES 的类型，无需 Cloudflare 账号或真实资源，
不作为部署配置；实际部署仍由 Sites/Vite 提供绑定，届时需核对绑定及兼容设置与类型配置一致。

## 上传代码仓库时的大文件说明

- models/ 内含超过 100MB 的文件（GGUF、CUDA DLL），GitHub/Gitee 直接 push 会失败；仓库已提供 .gitattributes，推荐用 Git LFS 托管（git lfs install 后 git add 会自动走 LFS）。
- 17GB 的 27B GGUF 不在仓库内，按 models/ollama/Modelfile-qwen3.6-27b 说明单独分发。
- server/data/（本地数据库、上传文件、凭据、模型设置）、node_modules/、.venv/、runtime/ 均已加入 .gitignore，不会提交。

## 边界与已知缺口

- 说话人分离为声纹聚类（FunASR），不建立跨会议声纹库；同一说话人被停顿切成多段可正确归组。
- 关键帧 OCR 对演示级字号效果良好，小字号正文识别率有限。
- 分析失败重试为全量重跑（阶段产物已落库，但断点续跑未实现）。
- retention_days 保存期限已建模，但尚无自动清理任务。
- 前端暂不展示转写全文与音频播放（数据在后端可查）。
- 结构化准确率不低于85% 与 60-90 分钟会议小于等于原时长 为后续真实样本验证目标，当前未宣称达标。

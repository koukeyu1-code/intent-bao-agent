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
启动意向宝.bat    一键启动（自动拉起 ASR 语音服务 8099、后端 8000、前端 3000）
停止意向宝.bat    一键停止
```

启动后浏览器打开 http://localhost:3000，后端接口文档在 http://127.0.0.1:8000/docs。也可手动执行 scripts/start-local.ps1 / scripts/stop-local.ps1。

### 首次登录

- 本地账号为 admin（管理员）与 planner（普通用户），源码不提供默认密码。
- 可设置环境变量 INTENT_BAO_ADMIN_PASSWORD / INTENT_BAO_USER_PASSWORD 指定初始密码；未设置时后端生成强随机密码，启动窗口会提示凭据文件位置（server/data/config/initial-login-credentials/，该目录已被 Git 忽略）。
- 凭据文件保存到个人密码管理器后请删除；账号创建后环境变量不再影响其密码。

## 使用流程

1. 模型设置：确认 Ollama 与 ASR（llama.cpp http://127.0.0.1:8099，模型名 qwen3-asr）已保存，健康检测"已连接"；
2. 资料上传：新建会议资料包，上传录音/录屏/PDF/纪要；无 PDF 时填写例外原因；
3. 启动分析：立即返回列表，任务后台执行，列表实时显示 8 阶段进度，完成后自动提示；
4. 身份校验：对自动分离出的 Speaker A/B/C 逐个人工标注姓名、角色、组织、职能；
5. 意向确认：逐条核对 AI 草稿的原文、证据、五档态度与采用阶段，确认后形成正式职能状态；
6. 看板与导出：客情总览/技术/客户视角查看正式状态；资料包详情可导出 CSV/Excel/Markdown；
7. 模型评测（可选）：用金标准对比 AI 原始输出，查看结构化准确率、证据可追溯率、未知误判率。

演示素材在 samples/：three_speakers.wav（三说话人录音，体验分离）、test_audio_tangpoems.mp3（单人唐诗朗诵）、test_screen_recording.mp4（含语音的演示录屏）、test_real_chinese_doc.pdf（中文样例 PDF）。

## 测试

```powershell
# 后端（在 server 目录）
.venv\Scripts\python.exe -m pytest -q

# 前端（在项目根目录，需 Node）
node --test tests/*.test.mjs
```

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

# 意向宝智能体（Intent-Bao Agent）

> 让客户声音照进产品规划

意向宝把客户会议材料（录音、录屏、PDF、纪要）组织成可追溯、可确认的产品规划意向地图：AI 负责"听懂、看懂并建议"，业务负责人负责"确认结论"。全部模型默认在本机运行，企业真实敏感数据不出本机。

> **给新电脑/新成员的最短路径**：先看 [新电脑运行指南.md](新电脑运行指南.md)，本文是完整说明。

## 一、项目能做什么

上传一次会议的音频/录屏和 PPT/PDF 等材料后，系统在后台自动执行 8 个阶段：

文件预检 → 文档解析 → 音轨与转写 → 说话人分离 → 画面与 PDF 解析 → 资料对齐 → 身份辅助匹配 → 意向抽取

最终产出：带原文证据、说话人、五档态度、采用阶段的可确认意向记录，以及客户/技术视角看板与 CSV/Excel/Markdown 导出。

## 二、模型构成（当前推荐配置）

| 用途 | 模型 | 运行方式 | 文件大小 | 从哪来 |
| --- | --- | --- | --- | --- |
| 意向抽取 / 资料对齐 / 身份推荐 / PDF 理解 | Qwen3.5-9B（`qwen3.5:9b`） | Ollama | 下载约 6.6 GB | `ollama pull qwen3.5:9b`，仓库不含 |
| 语音转写（主模型） | Qwen3-ASR-1.7B `Qwen3-ASR-1.7B.Q8_0.gguf` | llama.cpp server | 约 2.1 GB | Hugging Face 手动下载，仓库不含 |
| 语音转写（多模态 mmproj） | `mmproj-Qwen3-ASR-1.7B-Q8_0.gguf` | llama.cpp server | 约 340 MB | 随 `models` 模型包分发（GitHub Releases / 网盘） |
| llama.cpp 运行库 | `llama-server.exe` + CUDA 运行库 | 本地进程 | 约 557 MB | 随 `models` 模型包分发（GitHub Releases / 网盘） |
| 说话人分离（谁说了什么） | FunASR：fsmn-vad + cam++ | Python（FunASR） | 约 31 MB | 随 `models` 模型包分发；缺失时也可从 ModelScope 自动下载 |

> **仓库分工**：GitHub 代码仓库只放代码，不含任何模型文件。本地运行所需的
> llama.cpp / mmproj / FunASR（共约 900 MB）统一打成 `models-<版本>.zip`，
> 通过 [GitHub Releases](https://github.com/koukeyu1-code/intent-bao-agent/releases)
> 分发（当前版本见下方 5.1）；`qwen3.5:9b` 与 2.1 GB 的 ASR 主模型仍需各自
> 单独安装（见 5.2）。

可选：Tesseract OCR（含简体中文 `chi_sim` 语言包），用于录屏关键帧文字识别；没有它视频画面 OCR 会跳过，不影响其他链路。

> **为什么用 9B 而不是 27B**：9B（Q4）占用约 6.6 GB 显存，16 GB 显卡可完整放下，实测生成速度约 90+ token/s；27B 模型文件约 18 GB，16 GB 显存放不下会被挤到 CPU，速度大幅下降。意向抽取质量经真实长会议验证可满足使用。更早文档中的 `qwen3.6-27b` 方案已弃用，无需再导入。

## 三、目录结构

```text
app/                前端（React / Next.js + TypeScript）
server/             后端（FastAPI + SQLite + SQLAlchemy），依赖 server/requirements.txt
models/             本地模型与运行库（仓库只保留说明文件，模型本体随发布包分发）
  qwen3-asr/        Qwen3-ASR：放置说明 + 主 GGUF / mmproj（模型包外部分发）
  llama.cpp/        llama-server 运行时与 CUDA 库（模型包外部分发）
  funasr/           FunASR 说话人分离模型（模型包外部分发）
  ffmpeg/bin/      可选的本地 FFmpeg / FFprobe；也可安装到系统 PATH
  ollama/           旧 27B Modelfile（已弃用）
samples/            演示素材
scripts/            安装/启动/停止/ASR 服务脚本
runtime/            运行期目录（日志、pid，自动生成，Git 忽略）
docs/               开发方案、开发文档、测试报告、开题报告
```

## 四、环境要求

- Windows 10/11（脚本为 PowerShell）或 macOS 12+ / Apple Silicon（脚本为 Bash）
- Python 3.11+（建议 3.12，Windows 安装时勾选 **Add to PATH**）
- Node.js 22.13+ 与 pnpm（`npm install -g pnpm`）
- FFmpeg / FFprobe（安装脚本可自动用 winget 安装）
- 意向模型服务：Ollama（[下载](https://ollama.com/download)）或本地 OpenAI 兼容服务（例如 LM Studio，二选一）
- Git（用于克隆代码仓库）
- NVIDIA 显卡建议 8 GB 显存以上，16 GB 体验最佳；无显卡可运行但速度慢
- 硬盘剩余空间建议 ≥ 20 GB（依赖 + 全套模型）

## 五、从零跑通（新电脑）

### 5.1 获取代码与模型运行包

代码仓库不包含模型文件，先克隆代码：

```powershell
git clone --branch version-4.0 https://github.com/koukeyu1-code/intent-bao-agent.git
cd intent-bao-agent
```

再从 **GitHub Releases** 下载当前版本的
[models-0.1.0.zip](https://github.com/koukeyu1-code/intent-bao-agent/releases/download/v0.1.0/models-0.1.0.zip)
（含 llama.cpp 运行库、Qwen3-ASR mmproj、FunASR 模型，约 590 MB），
解压到项目根目录，使目录结构形如：

```text
models/
  qwen3-asr/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf
  llama.cpp/llama-server.exe
  funasr/models/...
```

检查关键文件存在且大小正常（不是几百字节的空文件）：

```powershell
Get-Item models\llama.cpp\llama-server.exe | Select-Object Length
Get-Item models\qwen3-asr\mmproj-Qwen3-ASR-1.7B-Q8_0.gguf | Select-Object Length
```

`llama-server.exe` 可以是约 9 KB 的正常启动器，需要同目录的 `llama-server-impl.dll`、
`llama.dll`、`ggml*.dll`、`mtmd.dll` 等运行库；不要仅凭 EXE 较小就判断模型包损坏。
GGUF 主模型和 mmproj 则应分别约 2.17 GB、356 MB，且文件开头为 `GGUF`，不能是下载错误网页或 Git LFS 指针。

如果暂时没有模型包，代码仍可启动并体验"模拟分析"界面；真实语音转写需先补齐模型包。

### 5.2 安装两个仓库外的模型

**（1）意向抽取大模型（Ollama）**

```powershell
ollama pull qwen3.5:9b
ollama list   # 应看到 qwen3.5:9b
```

使用 LM Studio 等本地 OpenAI 兼容服务时，可跳过 Ollama 安装，在该服务中下载并加载意向模型、启动 API；配置方式见 5.5。

**（2）ASR 主模型（2.1 GB，仓库放不下）**

任选一种方式，把文件下载到 `models/qwen3-asr/`：

```powershell
# 方式 A：huggingface-cli
pip install -U "huggingface_hub[cli]"
huggingface-cli download Qwen/Qwen3-ASR-1.7B-GGUF Qwen3-ASR-1.7B.Q8_0.gguf --local-dir models\qwen3-asr
```

或浏览器打开 [Qwen/Qwen3-ASR-1.7B-GGUF](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-GGUF)，下载 `Qwen3-ASR-1.7B.Q8_0.gguf` 后放到 `models/qwen3-asr/`。

放置后应形如：

```text
models/qwen3-asr/
  Qwen3-ASR-1.7B.Q8_0.gguf            ← 手动下载
  mmproj-Qwen3-ASR-1.7B-Q8_0.gguf     ← 模型包解压后得到
  !下载模型到此处.txt
```

### 5.3 安装依赖

双击 **首次安装依赖.bat**，脚本会自动：

1. 创建后端 Python 虚拟环境 `server/.venv`；
2. 安装 PyTorch（优先 CUDA 版，失败自动回退 CPU 版）及后端依赖；
3. 安装前端依赖（pnpm install）；
4. 缺失时通过 winget 安装 FFmpeg；
5. 检查 Ollama、Tesseract、模型文件并给出警告。

也可以手动执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-local.ps1
```

**macOS（Apple Silicon）**：先安装系统组件，再运行 Bash 版脚本（行为与 Windows 版一致：
创建 `server/.venv`、安装 PyTorch（macOS 版自带 MPS 加速）与后端依赖、`pnpm install`，并检查
Ollama / llama-server / 模型文件）：

```bash
brew install python@3.12 node pnpm ffmpeg llama.cpp   # 关键帧 OCR 另需：brew install tesseract tesseract-lang
scripts/setup-local.sh
```

### 5.4 启动

先确认媒体工具可运行。未安装系统 FFmpeg 时，可从 [FFmpeg 官网列出的 Windows 构建](https://ffmpeg.org/download.html#build-windows)
下载完整包，将两个真实程序放到 `models/ffmpeg/bin/`。后端支持自动查找这个目录：

```powershell
models\ffmpeg\bin\ffmpeg.exe -version
models\ffmpeg\bin\ffprobe.exe -version
```

仅有文件名并不代表安装成功；若 `.exe` 实际是下载失败的 JSON/HTML 文本，需重新获取完整包。
FFmpeg 用于音轨提取、切片与抽帧，FFprobe 用于时长和时间轴探测，两者都需准备好。

双击 **启动意向宝.bat**，或执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1
```

脚本会依次拉起：

| 服务 | 地址 |
| --- | --- |
| ASR 语音服务（llama.cpp） | http://127.0.0.1:8099 |
| 后端（FastAPI） | http://127.0.0.1:8000/docs |
| 前端 | http://localhost:3000 |

首次启动前端需要 1~2 分钟编译页面。启动窗口若显示黄色警告"未找到 ASR 模型"，说明 5.1/5.2 的文件没放齐。

停止服务：双击 **停止意向宝.bat**（或 `scripts\stop-local.ps1`）。

macOS 上启动用 `scripts/start-local.sh`（或双击 **启动意向宝.command**），三个服务的地址与上表一致；
停止用 `scripts/stop-local.sh`（或 **停止意向宝.command**）。可选参数：`--no-browser`、
`--backend-port`、`--frontend-port`、`--asr-port`。macOS 的 ASR 运行时来自 Homebrew 的
`llama-server`（`brew install llama.cpp`），模型 GGUF 文件与 Windows 通用，放在相同位置即可。

### 5.5 首次登录与模型配置

代码仓库不包含默认密码。首次启动后，凭据生成在：

```text
server/data/config/initial-login-credentials/
  admin.txt    系统管理员
  planner.txt  产品规划用户
```

用其中账号登录 http://localhost:3000 后，进入 **模型设置** 页，填写并点击“测试并保存”：

| 配置项 | 值 |
| --- | --- |
| 模型提供方 | 本地 Ollama |
| Ollama 地址 | `http://127.0.0.1:11434` |
| 模型名称 | `qwen3.5:9b` |
| ASR 提供方 | llama.cpp |
| ASR 地址 | `http://127.0.0.1:8099` |
| ASR 模型名称 | `qwen3-asr` |

保存后请把临时凭据文件保存到密码管理器并删除。若忘记密码，双击 **重置意向宝登录密码.bat**。

也可使用已支持的**本地 OpenAI 兼容服务**（例如 LM Studio）：选择相应提供方，填写实际运行的地址
（LM Studio 常用 `http://127.0.0.1:1234`）及其 `/v1/models` 返回的模型 ID。
此模式的模型由该服务管理，不必放进本项目 `models/`，也不要求同时安装 Ollama；仍需启动对应服务并测试连接。
ASR 是独立配置：本项目启动脚本固定使用 `http://127.0.0.1:8099`，旧配置若保存了 `8081`，请在模型设置页改为实际地址后保存。
更新代码只更正新配置的默认值，不会覆盖已有模型设置。

## 六、页面使用流程

1. 模型设置中确认意向模型与 ASR 均显示"已连接"；
2. 新建会议资料包：选项目/客户，填会议标题、日期、数据级别，可先添加参会人；
3. 上传材料：录音/录屏/PDF/人工纪要可一次上传多个；
4. 点击"开始分析"：任务进入后台，资料包页实时显示 8 阶段进度，可随时取消；
5. 分析完成后先到"说话人校验台"，把 Speaker A/B/C 标注为姓名/角色/组织/职能；
6. 再到"意向确认台"逐条核对 AI 草稿的原文引用、技术、职能、五档态度、采用阶段，填写复核理由后确认。低置信度或历史冲突需逐条复核；批量确认仍受限制，身份、证据和版本校验继续生效；
7. 在客户/技术视角看板查看正式意向，资料包详情页可导出 CSV/Excel/Markdown。

演示素材在 `samples/`：`three_speakers.wav`（三说话人录音）、`test_audio_tangpoems.mp3`（单人朗读）、`test_screen_recording.mp4`（含语音的演示录屏）、`test_real_chinese_doc.pdf`（中文样例 PDF）。

同包全部录音和录屏分别处理，并保留各自来源和时间轴；不同文件、不同分析轮次不会因同名说话人标签自动合并。
人工拆分说话人仅迁移原文和时间证据能明确归属的意向，来源不确定时会拒绝拆分。
亮点规格支持新增、修改和清空。模型设置的清除密钥操作独立于未保存表单，仅清除当前后端进程的密钥；环境变量配置的密钥会在重启时重新加载。
注销请求失败也会退出本地业务页面。CSV/XLSX 导出将可能被解释为公式的证据写为文本，原始证据仍保留在数据库。

评测默认使用最近成功分析轮次的不可变 AI 原始快照，可通过评测 API 的 `analysis_run_id` 指定轮次；
报告包含漏检数、未知样本漏检数，未知误判率以未知金标准样本为分母。已人工修改且缺少原始快照的旧记录需重新分析，不能用人工结论冒充 AI 原值。

## 七、手动执行命令（不用 .bat）

```powershell
# 安装依赖
powershell -ExecutionPolicy Bypass -File scripts\setup-local.ps1

# 启动
powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1

# 停止
powershell -ExecutionPolicy Bypass -File scripts\stop-local.ps1

# 重置登录密码（admin 或 planner）
cd server
powershell -ExecutionPolicy Bypass -File "..\重置意向宝登录密码.bat"
```

macOS（Bash）等价命令：

```bash
# 安装依赖
scripts/setup-local.sh

# 启动（可加 --no-browser）
scripts/start-local.sh

# 停止
scripts/stop-local.sh

# 重置登录密码（admin 或 planner，--generate 生成强密码）
scripts/reset-password.sh admin --generate
```

## 八、测试

```powershell
# 后端（在 server 目录）
.venv\Scripts\python.exe -m pytest -q

# 前端（在项目根目录，需 Node；会先构建再跑测试）
npm test

# 本地前端和 Cloudflare 模板分别做类型检查
pnpm run typecheck
```

`tsconfig.json` 与 `tsconfig.cloudflare.json` 分离浏览器/Workers 全局类型；Cloudflare 检查覆盖 `worker/`、`db/`、`examples/d1/`。
`worker/worker-configuration.d.ts` 由现有 Wrangler 生成。改变 `wrangler.types.jsonc` 后，运行 `pnpm run types:cloudflare` 再检查；该配置仅用于类型生成，不用于部署。
隔离测试时，请将 `INTENT_BAO_DATA_DIR` 指向专用测试目录，并清除测试进程继承的 `INTENT_BAO_DATABASE_URL`，让各测试按自己的 `data_dir` 建库；固定全局数据库地址会干扰密码重置等隔离测试。并行 pytest 应指定不同的 `--basetemp`，不要使用业务数据库。

## 九、常见问题

| 现象 | 处理 |
| --- | --- |
| 启动提示未找到 ASR 模型 / ASR 服务不可用 | 检查 `models/qwen3-asr/`、`models/llama.cpp/` 文件是否齐全（5.1/5.2），补齐后重新启动 |
| 页面一直显示"模拟分析" | 意向模型服务未启动，或模型设置没保存成功；启动所选 Ollama / 本地 OpenAI 兼容服务后重测 |
| `ollama pull` 很慢或失败 | 先 `ollama pull` 小模型确认网络，再重试；也可设置 `OLLAMA_HOST` 等常规环境变量 |
| 登录无默认密码 | 查看 `server/data/config/initial-login-credentials/` 或重置密码 |
| 前端/后端端口被占用 | 停掉占用 3000/8000/8099 的进程后重启 |
| Windows 下直接 `npm start` 页面缺少脚本或样式 | 当前 vinext 0.0.50 预构建静态资源路径存在 Windows 兼容问题；本地请使用 `启动意向宝.bat`，该脚本运行已验证的 dev 服务 |
| `models/` 目录缺失或文件很小 | 未下载/解压模型包，从 GitHub Releases / 网盘获取后放回根目录 |
| 无 NVIDIA 显卡或显存 < 8 GB | 仍可运行但慢；可把上下文调小（见下方"可选调优"） |
| 说话人分离结果为空 | 确认已安装 FunASR 且网络可访问 ModelScope（缺失模型会自动下载一次） |

### 可选调优：上下文窗口

默认预算按 `num_ctx=32768` 计算。可在启动前设置环境变量：

```powershell
$env:INTENT_BAO_LLM_NUM_CTX = "16384"
启动意向宝.bat
```

Ollama 请求会携带这个值。OpenAI 兼容服务的实际上下文必须在模型服务中设置，环境变量不会替 LM Studio 重新加载模型；两侧应保持一致。
程序按完整提示、材料、主数据、结构约束、格式回退提示和输出预留保守估算 token，并分批处理超长单项；估算不是服务端精确 tokenizer，服务端仍拒绝时会显示明确的上下文错误。

## 十、仓库分工与发布说明（方案 B）

- **代码仓库只放代码**：`models/llama.cpp/`、`models/funasr/`、ASR 的两个 GGUF、
  旧 `models/ollama/` 均已在 `.gitignore` 排除，任何人不得 `git add -f` 强行加入。
- **模型运行包**：发布新版本时，把本地 `models/llama.cpp`、
  `models/qwen3-asr/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf`、`models/funasr`
  打成 `models-<版本>.zip`（约 900 MB）作为 GitHub Release 附件，或放网盘并把
  地址写进 README。打包命令（在项目根目录执行，输出放到仓库外）：

  ```powershell
  tar.exe -a -c -f D:\release\models-0.1.0.zip `
    models/llama.cpp `
    models/qwen3-asr/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf `
    models/funasr
  ```
  归档内必须保留 `models/` 和各子目录层级，解压后直接得到项目根目录的 `models/`；不要把模型文件压平，也不要多套一层 `models-0.1.0/`。
- **仍需单独安装的模型**：`qwen3.5:9b`（Ollama）与
  `Qwen3-ASR-1.7B.Q8_0.gguf`（Hugging Face，约 2.1 GB）。
- `server/data/`（数据库、上传文件、初始凭据、模型设置）、`node_modules/`、`.venv/`、`runtime/` 均已被 `.gitignore` 排除，不会提交。

## 十一、当前边界与已知缺口

- 说话人分离为声纹聚类（FunASR），不建立跨会议声纹库；同一说话人被停顿切成多段可正确归组。
- ASR 长会议按不超过 30 秒的窗口处理；短片段失败时仅在不跨入相邻说话人发言的范围内补充上下文重试。重试后仍为空或被 ASR 判为音频过短的片段保留原时间范围，正文置空，继续处理其余片段与后续阶段。
- 资料包详情显示“未识别置空”数量，并可展开查看材料、时间和原因。空正文不用于身份匹配、资料对齐或意向抽取，不自动认定为静音，也不编造内容；全包没有任何可用正文时，相应模型阶段标为不适用。
- 成功转写块立即保存在 `server/data/temp/asr-checkpoints/`。重跑在文件内容、模型配置、热词和分段边界一致时复用成功块，仅重新请求缺失/未识别块；空占位不写入成功缓存，旧版本未保存的结果无法恢复。
- 原始产物 API `/api/analysis-runs/{run_id}/artifacts` 返回轮次 `status`、`artifacts_available` 和错误原因；未成功完成或请求取消的轮次返回空产物列表，避免把多文件处理中的部分结果当作完整证据。
- ASR 服务连接、超时、响应格式、切片或文件读取等基础故障仍会明确报错；连续基础故障会提前结束并记录错误类别、材料和时间范围。空正文不计入连续基础故障，不阻断整场分析。
- "资料对齐"过滤空片段，按输入上下文和预期输出规模分小批请求，初始每批最多 4 个片段、8 个页面或画面候选；上下文不足时继续切小。每次请求使用独立的 2048 token 输出预算，拆小输入不会同步缩减输出额度。仅遇到输出耗尽（`finish_reason=length`）时拆小失败批次；每个原始批次含首次最多请求 7 次，共享 480 秒耗时上限。
- 对齐重试仍失败时保留已成功的关系，记录未完成对齐的材料、片段时间和原因，继续身份辅助匹配及意向抽取。详情显示“已完成，有未对齐项”和受影响片段数，可展开核查；这些片段也可能已与部分资料成功匹配，未完成的关系不作为证据。正常返回零条关系不算错误，无可对齐材料则标记不适用。程序或数据库异常仍按失败处理，取消请求仍结束任务。
- PDF 重跑只使用当前轮解析页，保留历史页用于追溯。同一文件同一位置的重复输出去重，不同文件的相同原文保留各自来源。
- 关键帧 OCR 对演示级字号效果良好，小字号正文识别率有限。
- 重试会新建分析轮次并复用有效 ASR 缓存，其它模型阶段仍重新运行。重启会将未结束任务明确标为失败，保留已完成阶段和中断原因。
- 前端按后台终态持续观察任务，断网后恢复查询，详情关联最新会议数据；模型请求展示批次、耗时和重试状态，并有总耗时上限。转写全文浏览与片段音频回放为后续增强项。
- `retention_days` 保存期限已建模，尚无自动清理任务。

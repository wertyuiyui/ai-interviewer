# 砺面：AI 模拟面试官

面向中国本科生的「大厂后端开发实习一面」语音陪练 Web 应用。候选人上传文字版简历 PDF（也可粘贴文字），选择字节跳动 / 美团 / 腾讯 / 阿里巴巴 / 百度 / 华为、技术面 / 综合（HR）面 / 技术+综合面、后端细分方向、压力程度和面试时长后，AI 会按公司风格围绕简历连续下钻；也可以跳过完整模拟，直接用真实公开、许可可追溯的题库进行语音或文字单题练习。面试结束后生成逐题扣分、公开 rubric、改写示范和下次必练清单，并用匿名 Profile 对比历次成绩。

这是 16 小时黑客松范围内的 MVP，只做后端实习一面。明确不包含岗位推荐、面试外部实时辅助（作弊向）、视频/微表情、代码运行判题、RAG、账号系统或运行时面经爬虫。产品内的“提示”只服务于模拟练习，提供回答结构而非答案，并会如实记入最终报告。

## 已实现的闭环

- 六家公司风格卡与独立 `interview_skills`，含证据等级、环节顺序、语气、选题权重、难度阶梯、项目追问、压力策略、综合面重点和三种语言 profile；剧本会实际加载对应 skill。资料只够做题库索引、无法支撑公司风格的其它公司没有被冒充成“精准模拟”。
- 面试类型可选纯技术面、独立综合（HR）面或技术+综合面，旧请求默认保持技术面。HR 环节分别考察价值观与公司契合、人生规划和选择、协作证据与薪酬沟通；面向本科实习候选人，不套用管理岗问题，也不因薪酬数值本身扣分。
- 固定主问题只来自 108 道可追溯公共题池；每道主问题后允许一次绑定上一答关键词的个性化追问，再切到下一道。六份公司 skill 只改变题目排序、节奏、深挖维度和压力方式，不把公共题冒充为某公司的独家真题。
- 公开面经只作为公司风格与报告建议的可追溯样本，不进入可逐字提问的固定题面数组。它们未经招聘企业确认，不是官方标准，也不会复制原帖叙事、答案、图片或长题单。
- `/practice` 快速刷题定位为八股知识题练习，只从经过审核的静态授权题库选题，当前包含 108 道中英双语短题，覆盖 Java / Go、MySQL、Redis、并发、操作系统、计网、分布式系统、AI 工程和英文行为题。题目选自或经压缩、翻译自固定版本的 JavaGuide、interview-go、Tech Interview Handbook 与 ARIS-in-AI-Offer；每题内部保留来源路径、提交版本和 Apache-2.0 / MIT 许可证，但这些字段不会返回到学习端 API 或显示在作答、评分 UI。
- 快速刷题支持中文、中英双语和纯英文，按公司练习侧重、面试类型、方向与难度筛选；公司选择会按对应 skill 对同一审核公共题池重新排序，组合面稳定保留技术题与行为题。每道题可用文字或语音作答，语音经实时 ASR 进入可编辑文本框，用户确认或修正后才提交评分。同一题可以重复作答，评分失败会明确显示“不可评分”，不会补成默认 5.0。
- `/coding` 是与八股快刷、项目解读并列的手撕代码单项训练。首批 8 道题按 Grind 75 的公开策展清单选择并独立改写，覆盖数组与哈希、栈、链表、二分、区间、网格与图；作答流程为澄清约束、方案设计、编码实现、主动自测，最终按沟通、解题、技术实现、测试四维静态复盘。MVP 不执行或编译代码，也不会声称用例通过。
- CloudWeGo Hertz / Kitex、Spring PetClinic、PingCAP Talent Plan / TinyKV 等开源项目会被改写成背压、重试预算、事务边界、崩溃一致性等工程场景题，并按岗位细分方向匹配。候选人无需读过指定仓库，仓库只提供可追溯的实践背景。
- 岗位仍限定为后端实习，但支持通用、Java、Go、C++、Python、基础架构、云原生、数据库与存储、中间件、分布式系统、AI 工程后端等常见细分方向，也支持 1–80 字自定义方向。
- AI 工程后端 / LLM Infra 方向会把已审核短名单中的模型服务题控制在约三分之一，当前 12 道覆盖请求状态机、TTFT/TPOT、continuous batching、KV cache、量化、RAG 与评测；知识点经人工改写自 MIT 许可的 [ARIS-in-AI-Offer](https://wanshuiyin.github.io/ARIS-in-AI-Offer/)，不会进入默认通用后端题池。
- 这些 AI 工程题以实践、验证方法和 trade-off 为主，不要求背论文公式、实验数字或实现细节，也不会仅因没读过指定论文判定答崩。
- PyMuPDF 提取 PDF 文字层，一次百炼调用输出固定 `{教育, 实习经历[], 项目[], 技能[]}` Schema；扫描件明确提示改传文字版。
- 匿名 Profile 可长期保存多份论文/项目资料。条目可自主选择应用类、技术类或论文，支持源码/ZIP/论文 PDF、最多 5 个 GitHub/arXiv 链接；论文链接至少包含一个 arXiv 主链接。默认视为候选人负责整个项目，只有显式选择“部分负责”并填写职责，或在核心组件中勾选后，追问才收窄到所选范围。
- 论文/项目解读以 NDJSON 返回真实阶段：读取快照、组织证据、生成、校验链路、保存。应用类更关注用户问题、核心功能与设计动机；技术类关注机制、正确性、性能和取舍；论文应用内置三遍阅读 skill，关注研究问题、方法贡献、实验依据、局限与可复现性。GitHub 抽样仍优先入口、服务、数据和配置层，README 中的需求、prompt、skill 和示例题不作为已实现链路证据。
- 架构与请求链路结论必须引用已保存的实现代码或配置路径；页面会明确标出“已核对 / 部分核对 / 待核实”、缺失步骤、分析假设和待补证据，并生成一段严格受本人职责和已核实链路约束的面试项目介绍。项目深挖题同样要求代码证据，支持继续生成更多题或整组重新生成。
- 首题自我介绍只了解学校专业、学习进度、课程基础、技术方向和求职目标；听完后服务端必须另开一题，单独选择项目或实习经历，不把两段内容挤在同一道题里。
- 服务端强制七维项目/实习下钻：业务背景、个人职责、请求链路、技术选型理由、难点与故障、数据指标口径、边界与 trade-off。技术面默认强制 4 层；技术 + 综合（HR）面为给综合环节留出有效时间，默认强制 3 层，若上场项目深度较弱则扩为 4 层。
- 压力程度分为关闭 / 温和 / 标准 / 高压四档。压力主要来自更高难度、更深且会改变约束的连续追问；质疑只针对确实缺少依据的结论，实时打断只在部分转写已表现出明显跑题、反复绕圈、长时间无有效结论等表达问题时触发，不按轮次盲目抢话。标准/高压连续 2 次、关闭/温和连续 3 次答崩时，服务端强制提前结束。
- 时长提供 10 / 15 / 25 分钟、自定义 1–180 分钟，以及“不限时（手动结束）”；无限模式前后端都不会创建自动截止时间。
- 面试语言可选“全程中文”“中英双语”或“Pure English”。双语模式允许候选人使用中文、英文或中英混合回答；纯英文模式的开场、项目追问、技术题、综合题、压力转场、提示与结束语均使用英文，并优先使用带来源信息的海外英文题，不因非母语口音扣技术分。
- 语音回答会以增量字幕实时进入对话区，最终转写可在面试中手动修正；修正后按原题重新评分，报告保留“已修正”标记并使用修正版。L0 会先核对面试官完整朗读转写，发现模型改写问题时丢弃该段音频并用锁定文字精确合成，避免音文不一致。
- 每道问题同时给出建议回答时间；语音场次记录 VAD 回答时长与转写字符速率，用于报告中的时间把握、语速、措辞和转写流畅度分析。
- 面试页的“给我一点提示”只拆解回答路线，不直接给出事实答案，也不会替用户提交回答；同一道当前问题至多记录一次。面试仍可完整继续，最终报告会展示提示次数、对应问题和提示内容。
- 面中不发送任何得分或扣分；报告阶段才调用 qwen-plus，根据完整转写生成结构化结果。
- rubric 固定为项目深度 40% / 基础八股 30% / 手撕思路 20% / 表达逻辑 10%，总分由后端按本场实际覆盖维度重新计算并显示评分覆盖率；没问到或评分服务失败的维度为 `null / 不可评分`，不会补成 5.0。
- 报告另含简历内容强弱项与改写建议、排版可评估边界、面试时间/语速/措辞/流畅度、岗位契合度和多维雷达图。目标公司的公开个人面经由服务端静态精选，报告展示跨样本共性、针对性建议和可核验原帖；不把这些帖子冒充官方题库。
- “记住本场表现”可逐场开关。开启时，SQLite 会把最近 3 场归一成 MySQL / Redis / Java 并发 / 计网 / 手撕 / 项目深度等稳定知识域并按时间加权，下一场为可命中的弱项预留约三分之一题目；项目深度较弱时从 4 层扩展到 6 层下钻。关闭时报告仍可查看和保留，但该场既不读取旧弱项，也不参与后续弱项加权。
- 报告页提供“用原简历复练弱项”：同一匿名设备可一键复用原简历及公司、方向、语言、压力和时长设置，以本场报告的弱项直接创建下一场针对性面试。
- 报告中的低分题可以进入独立错题重答：既可重答整场筛出的低分题，也可只重答指定题目；练习页会带入原题、上次分数和扣分点，新回答单独保存和评分，不覆盖原报告。
- Canvas 雷达图、逐知识点前后分数、历史删除和 localStorage 报告兜底。
- 5 份带 PDF 文字层的完全虚构测试简历仅保存在本地 `testdata/fake-resume-pdfs/`，覆盖 Java、Go、云原生、AI 工程和薄弱项目早停场景；不在产品页面展示或提供公网下载。

六份 `interview_skills/{company}_backend.json` 是基于公开样本和人工归纳的练习策略，不是六家公司发布或确认的招聘标准。公司内部不同事业群、部门、岗位方向、招聘批次和面试官的流程与题目都会有差异；“公司筛选”表示题目适用范围或练习侧重，不表示该公司实际问过这道题，也不保证复刻某一场真实面试。

## 使用说明

1. 在首页上传文字版 PDF 或粘贴简历，选择公司、面试类型、岗位细分、语言、压力程度和时长。“全程中文”仍会保留 MySQL、Redis、gRPC 等通用术语；“中英双语”会在中文主流程中安排简短英文追问；“Pure English”会保持候选人可见面试内容全英文。需要时可先运行简易硬件测试，确认麦克风权限、输入电平和中英文实时转写；测试不会创建面试或保存成绩。
2. 上传项目时可选填“我负责的”；进入 `/project` 后也可编辑职责，或从解读出的架构组件中勾选合并。开始解读后页面会按服务端真实状态展示当前阶段。先查看链路核验结果，再复制面试项目介绍；深挖练习可继续追加题目或按最新项目与职责重新生成整组题目。
3. 按隐私和练习目标选择是否开启“记住本场表现”。该开关只控制本场是否读取和贡献后续弱项记忆，不影响本场报告生成。
4. 每题点击“开始回答”后服务端开始计时，点击“结束回答”才封口并提交整题；思考停顿不会自动拆成下一题。语音回答会边说边进入右侧对话，识别完成后可点“修正转写”，保存后系统按当前题重新评估。面试官语音可以随时关闭而不影响文字提问；面试中卡住时也可点击“给我一点提示”，提示只给结构化思路并会记入报告。
5. 面试结束并生成报告后，点击“用原简历复练弱项”即可直接进入下一场。系统复用原设置，把报告中的低分知识点放进新剧本，并开启该复练场次的弱项记忆。
6. 打开 `/practice` 可按公司、方向、难度、题量和语言创建八股快速练习。语音模式会把增量转写实时写入回答框；停止录音即释放麦克风，提交前仍可手动改字。报告页的“重答低分题”也会进入同一页面，并显示原扣分点供对照。
7. 打开 `/coding` 可按题型、难度和编程语言选择手撕题；依次完成澄清、方案、代码、复杂度和自拟测试后提交四维复盘。各语言草稿只保存在当前浏览器中，服务器只做静态评估。

所有题库和资料来源均为随代码发布的静态 JSON。运行时不会抓取第三方页面、不会构建 RAG 或向量索引，也不会抓取小红书；面经/工程参考目录可通过 `GET /api/resources/catalog` 查看来源 URL、来源类型、授权/使用方式、核验日期和考察信号。快速刷题的逐题来源不会出现在学习 UI 或 `/api/practice/*` 的公开响应中，开发者可在 [`resources/practice_source_manifest.json`](resources/practice_source_manifest.json) 和 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 审核固定版本与许可证。完整策展规则见 [`references/CURATED_SOURCES.md`](references/CURATED_SOURCES.md)。

`/root/workspace/weiyi/data` 不是 66 道题的平铺题库，而是一份包含知识库、面经索引、社区和项目资源的调研目录；去重口径与 108 道上线审核题的组成见 [`references/DATA_INVENTORY.md`](references/DATA_INVENTORY.md)。

## 技术栈

- 后端：Python 3.10+、FastAPI、WebSocket、SQLite、httpx、PyMuPDF。
- 文本模型：阿里云百炼 `qwen-plus`，用于简历结构化、L3 追问控制和最终报告。
- L0：`qwen3.5-omni-flash-realtime` 原生 Realtime WebSocket，16 kHz PCM 输入、24 kHz PCM 输出、内置 ASR / LLM / TTS / 服务端 VAD。
- L1：Paraformer Realtime ASR + qwen-plus + CosyVoice + Silero VAD。
- L2：Paraformer Realtime ASR + qwen-plus + edge-tts + Silero VAD。
- L3：纯文字 WebSocket，与语音模式共用同一套剧本、追问、提前结束和报告引擎。
- 前端：原生 ES Modules、AudioWorklet、Canvas；没有构建步骤、第三方 CDN 或图表依赖。
- 公网：Caddy 自动 TLS + 单 worker Uvicorn；也提供 Docker Compose。

## 目录

```text
app/                 FastAPI、状态机、报告、SQLite 与四级语音适配
cards/               6 张兼容公司风格卡
interview_skills/    6 份可版本化、运行时实际应用的公司面试 skill
questions/           公司题库、授权快速题库、面经衍生题、工程场景题、ARIS 精选题与前沿讨论题
resources/           面经/工程来源目录、快速题库来源清单与学习链接白名单
references/          策展规则、来源说明与 Apache-2.0 / MIT 许可证副本
public/              首页、面试、报告、八股快刷、手撕代码 UI，AudioWorklet 与 Canvas 雷达图
scripts/             测试简历 PDF 生成器
testdata/            虚构简历结构化源数据、文字版与本地 PDF
tests/               核心状态机和语音协议离线测试
deploy/              Caddy 与 systemd 配置
data/                SQLite（运行时生成，不进 Git）
```

开发者需要重新生成五份本地测试简历时运行：

```bash
PYTHONPATH=.deps python3 scripts/generate_fake_resumes.py
```

生成文件只写入 `testdata/fake-resume-pdfs/`，不会被 FastAPI 挂载，也不会进入 Docker 构建上下文。

## 本地运行

### 1. L3 真实百炼全链路

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```dotenv
DASHSCOPE_API_KEY=sk-...
DASHSCOPE_WORKSPACE_ID=你的北京地域Workspace-ID
VOICE_MODE=L3
MOCK_LLM=false
```

启动：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

打开 `http://localhost:8000`。localhost 属于浏览器安全上下文，可本地调试麦克风；通过服务器 IP 访问语音模式时必须使用 HTTPS。

### 2. 无 API Key 的离线演示

离线模式会用确定性规则模拟简历抽取、追问评分和报告，适合先验证页面与提前终止；它不伪装成真实模型效果。

```bash
MOCK_LLM=true VOICE_MODE=L3 uvicorn app.main:app --port 8000
```

### 3. 启用 L0 / L1 / L2

L0 只需要主依赖。L1/L2 还要安装本地 Silero VAD（会带入较大的 Torch 依赖）：

```bash
pip install -r requirements-voice.txt
```

然后只改一行并重启：

```dotenv
VOICE_MODE=L0
```

L0/L1/L2 建议填写 `DASHSCOPE_WORKSPACE_ID`，程序会自动拼接北京地域专属域名。所有上游 URL 也都可用 `.env.example` 中的显式变量覆盖。桌面 Chrome + 耳机是建议的演示环境，可减少扬声器回声触发打断。

## 降级链

| 模式 | ASR | 追问模型 | TTS | 打断 |
|---|---|---|---|---|
| L0 | Omni 内置 | Omni + 服务端追问控制 | Omni 内置 | 服务端 VAD `speech_started` |
| L1 | Paraformer Realtime | qwen-plus | CosyVoice | Silero VAD |
| L2 | Paraformer Realtime | qwen-plus | edge-tts | Silero VAD |
| L3 | 文字输入 | qwen-plus | 无 | 不适用 |

`VOICE_AUTO_FALLBACK=true` 时，从指定模式向下尝试。例如 L0 建连失败会依次尝试 L1、L2、L3；CosyVoice 单次失败也会切到 edge-tts。每次变化都会发 `mode.changed`，页面会显示真实运行模式。Silero 包不可用或推理异常时会明确报告 `vad.status` 并使用能量 VAD 保住对话，但正式 L1/L2 验收应安装 `requirements-voice.txt`。

L0 保留 Omni 的服务端 VAD、ASR、TTS 与 `interrupt_response`，但关闭供应商的自动追问；共享的服务端剧本引擎完成七维深挖、答崩计数和弱项记忆后，再显式创建对应语音响应。候选人开始说话时页面立即清空播放队列；结束语则通过 `audio.stream.done → audio.playback.done` 确认浏览器真正播完后才跳报告页。

面试官问题的显示文字是唯一事实源。L0 会暂存该轮音频，收到完整 `response.audio_transcript.done` 后核对可听内容；一致才放行，若发生翻译、改写或增删则改用 edge-tts 对锁定文字重新合成。该校验会增加一轮问题播放前的等待，但避免先播放错误内容后再改字幕。

浏览器每 100 ms 上传一帧 16 kHz PCM，只在服务端完成供应商预检并发出 `mode.changed` 后开始上行；弱网时丢弃陈旧帧，把排队限制在约 1 秒内。ASR 返回空结果或单轮转写失败时会在页面明确提示重试，不会默默卡住或拆掉整条连接。面试页会显示增量实时转写、本地输入电平和服务端确认的信号状态；若持续近静音，可直接切换输入设备，或启用“原始输入”关闭浏览器降噪、回声消除与自动增益。多声道设备由 Worklet 选择有效声道后再转成单声道，避免聚合声卡首声道静音。服务端日志只记帧数、字节数、RMS、VAD/转写计数与耗时，不记录音频或对话正文。

没有任何有效回答或可用转写的场次会生成“数据不足 / 不计分”报告，不会以 5.0 或 0.0 冒充真实成绩，也不会进入弱项记忆和成长曲线。

## 关键配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 空 | 百炼 API Key，只保存在服务器 |
| `DASHSCOPE_WORKSPACE_ID` | 空 | 北京地域业务空间 ID，推荐配置 |
| `VOICE_MODE` | `L3` | `L0` / `L1` / `L2` / `L3` |
| `VOICE_AUTO_FALLBACK` | `true` | 语音初始化失败时自动向下降级 |
| `QWEN_TEXT_MODEL` | `qwen-plus` | 剧本与追问控制 |
| `QWEN_REPORT_MODEL` | `qwen-plus` | 报告生成 |
| `QWEN_REALTIME_MODEL` | `qwen3.5-omni-flash-realtime` | L0 模型 |
| `OMNI_TRANSCRIPTION_MODEL` | `qwen3-asr-flash-realtime` | L0 输入转写模型 |
| `OMNI_VAD_THRESHOLD` | `0.2` | L0 服务端 VAD 灵敏度；噪声大时可调高 |
| `OMNI_PREFIX_PADDING_MS` | `300` | VAD 命中前保留的语音，避免吞掉句首 |
| `OMNI_SILENCE_DURATION_MS` | `1500` | 判定一轮回答结束前允许的停顿，避免技术回答被切成多题 |
| `VOICE_DIAGNOSTICS_LOG_LEVEL` | `INFO` | 无对话正文的语音帧/VAD/ASR/TTS 诊断日志级别 |
| `PARAFORMER_MODEL` | `paraformer-realtime-v2` | L1/L2 ASR |
| `COSYVOICE_MODEL` | `cosyvoice-v3-flash` | L1 TTS |
| `DB_PATH` | `./data/interviews.db` | SQLite 路径 |
| `DAILY_INTERVIEW_LIMIT` | `20` | 全站每日新开场次预算保险丝 |
| `CLIENT_DAILY_INTERVIEW_LIMIT` | `5` | 每匿名设备每日场次上限 |
| `PRESSURE_INTERRUPT_SECONDS` | `4` | 检测到明显表达问题后，持续多久才进行条件式插话 |
| `MOCK_LLM` | `false` | 仅用于离线演示和测试 |

完整列表和 URL 覆盖项见 `.env.example`。

## 公网 HTTPS 部署

浏览器 `getUserMedia` 在非 localhost 环境要求安全上下文。最短部署路径是阿里云轻量应用服务器 + 已解析域名 + Docker Compose：

1. 将域名 A/AAAA 记录指向服务器，并按服务器所在地要求完成域名合规配置。
2. 安全组只放行 TCP 22、80、443（若使用演示配置再放行 TCP 3000）和 UDP 443；不要公网暴露 8000。
3. 复制 `.env.example` 为 `.env`，填写 API Key、Workspace ID 和 `DOMAIN=interview.example.com`。
4. 启动：

```bash
docker compose up -d --build
docker compose logs -f app caddy
```

Caddy 会自动申请并续期证书，反向代理配置已把 WebSocket 读写超时放宽到 30 分钟。Compose 默认安装 Silero/Torch，以保证 L1/L2 可直接切换；若服务器磁盘或内存紧张且只演示 L0/L3，可把 `INSTALL_VOICE` 构建参数改为 `false`。

不用 Docker 时，可把代码放到 `/opt/ai-interviewer`，创建 `.venv`，将 `deploy/Caddyfile.host` 首行替换为真实域名，再使用该 Caddy 配置和 `deploy/ai-interviewer.service`。生产只运行一个 Uvicorn worker，因为活动会话保存在进程内，报告与历史持久化在 SQLite。

只做临时公网演示时，可配套使用 `deploy/ai-interviewer-3000.service` 与 `deploy/Caddyfile.3000-https`：FastAPI 单 worker 仅监听 `127.0.0.1:8000`，Caddy 使用解析到当前服务器的 `39-106-146-28.sslip.io` 自动签发证书，并同时在标准 443 和 HTTPS 3000 端口反向代理。换服务器时必须把 Caddyfile 中的演示域名替换为新 IP 对应的 sslip.io 域名；长期部署建议改用自有域名。

该主机部署的公网入口是 `https://<域名>:3000`（也可走标准 443），不是直接暴露 Uvicorn 的 8000。服务器安全组需要放行 TCP 3000；Caddy 负责 TLS 和 WebSocket 升级，FastAPI 始终只监听回环地址。替换 `/etc/caddy/Caddyfile` 后先执行 `caddy validate --config /etc/caddy/Caddyfile`，再重载 Caddy。

## API 与浏览器协议

REST：

- `GET /api/config`
- `GET /api/resources/catalog`，返回静态来源元数据，不包含第三方正文
- `GET /api/practice/catalog`，返回可筛选的公司、方向、难度、语言和审核题量，不返回逐题来源
- `POST /api/practice/sessions`，创建 `quick` 真实题库练习或 `review` 面后错题重答
- `GET /api/practice/sessions/{id}?client_id=...`
- `POST /api/practice/sessions/{id}/answers`，提交文字或语音转写，可用 `reattempt: true` 重答同题
- `POST /api/practice/sessions/{id}/hint`
- `GET /api/practice/history?client_id=...&limit=20`
- `GET /api/coding/catalog`，返回手撕题面、函数签名、题型与公开来源策略，不返回私有 rubric 或提示
- `POST /api/coding/hint`，按澄清、方案、编码、自测阶段获取当前题的递进提示
- `POST /api/coding/review`，提交方案、代码文本、复杂度和自拟用例，返回四维静态复盘；`execution_status` 固定为 `not_executed`
- `GET /api/profile`，使用 `X-Profile-Key` 读取当前匿名 Profile 的简历/项目元数据
- `POST /api/profile/resumes`、`POST /api/profile/resumes/text`，保存并结构化 PDF 或粘贴文本
- `POST /api/profile/projects`、`POST /api/profile/projects/github`，兼容保存多文件/ZIP/PDF 或单个公开 GitHub 快照；接受 `project_type`、`responsibility_scope` 和 `responsibility`
- `POST /api/profile/projects/links`，保存最多 5 个 GitHub/arXiv 链接组成的同一论文/项目快照
- `PATCH /api/profile/projects/{id}`，更新项目类型或责任范围并使旧解读缓存失效
- `PATCH /api/profile/projects/{id}/selection`、`DELETE /api/profile/projects/{id}`，选择或手动删除项目
- `POST /api/profile/projects/{id}/analysis`，生成项目架构、经证据核验的请求链路、面试介绍和项目追问
- `POST /api/profile/projects/{id}/analysis/stream`，以 `application/x-ndjson` 返回真实分析阶段和最终结果
- `POST /api/profile/projects/{id}/questions`，按项目实现与本人职责追加或重新生成带证据的深挖题
- `POST /api/resumes/parse`，multipart 的 `file` 或 `text`
- `POST /api/interviews`
- `GET /api/interviews/{id}`
- `POST /api/interviews/{id}/hint`，为当前问题返回一次回答结构提示
- `POST /api/interviews/{id}/retry`，请求体为 `{ "client_id": "..." }`，复用原设置创建弱项复练
- `PATCH /api/interviews/{id}/turns/{ordinal}`，在报告生成前修正转写并按原题重评
- `POST /api/interviews/{id}/finish`
- `GET /api/interviews/{id}/report`
- `GET /api/history?client_id=...`
- `DELETE /api/history/{id}?client_id=...`
- `GET /healthz`、`GET /readyz`

创建面试的新增参数：

```json
{
  "role": "backend",
  "interview_type": "technical_hr",
  "specialization": "AI 工程后端 / LLM Infra",
  "language_mode": "bilingual",
  "stress_level": 2,
  "duration_minutes": null,
  "memory_enabled": true
}
```

`interview_type` 为 `technical`、`hr` 或 `technical_hr`，缺省为 `technical`；旧客户端的 `tech_hr` 会规范化为 `technical_hr`。`specialization` 可自由输入，服务端预设目录由已审核真实题库的 `direction_tags/topics` 覆盖生成，题库不可用时回退到兼容目录；`language_mode` 为 `zh`、`bilingual` 或 `en`；`stress_level` 为 0–3；`duration_minutes` 接受 1–180 整数，`null` 表示无限且只由用户手动结束；`memory_enabled` 控制本场是否读取和贡献后续弱项记忆。旧客户端的 `stress: true/false` 仍兼容映射为标准压力 / 关闭。快速刷题请求同样接受 `interview_type`：技术面排除行为题，HR 面只使用行为题，组合面按技术 60% / 行为 40% 选题。创建面试时若带 `profile_project_id`，还必须发送与 `client_id` 一致的 `X-Profile-Key`。

正式面试连接 `/ws/interviews/{id}`；首页可选硬件检查连接 `/ws/hardware-test`，后者只做短时 ASR 和麦克风状态测试，不创建面试记录。上行音频固定为 16 kHz、单声道、PCM16LE 二进制帧；每题用 `answer.start / answer.end` 明确回答边界，服务端照常记录按钮之间的完整用时。服务端把供应商事件统一为 `candidate.transcript.*`、`interviewer.text.*`、`interviewer.audio.synced`、`audio.chunk/file/clear`、`input.speech_started`、`timer.sync`、`interview.ended`。转写修正使用 `candidate.transcript.correct / corrected`；API Key 从不发送到浏览器。

快速刷题语音连接 `/ws/practice/sessions/{id}`。浏览器首先发送 `client.ready` 和匿名 `client_id` 完成会话归属校验，再上传同样的 16 kHz PCM16LE 帧；服务端返回 `practice.ready`、`practice.speech.started/ended`、`practice.transcript.partial/done`、`practice.stopped` 或 `practice.error`。WebSocket 只负责 ASR，最终文字会留在可编辑输入框中，用户确认后再通过 `/answers` 提交评分；PCM 音频不落盘。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试不访问外网，覆盖：

- 六家公司卡片 / skill 完整性、公司侧重排序、三类面试状态机和链接白名单。
- 授权快速题库的来源路径、固定提交、许可证、审核状态与中英文题面，以及学习端不泄露逐题来源。
- 快速刷题的公司/类型/方向/难度/语言筛选、文字与语音转写提交、重复作答、不可评分状态和面后低分题重答。
- ARIS 精选题仅在匹配 AI 工程细分方向时占约三分之一候选题，普通后端不会混入。
- PDF 文字层 / 扫描件判断、中文简历 Schema，以及 5 份虚构 PDF 的可提取性。
- 自定义细分方向、1–180 分钟 / 无限时长、四档压力参数与旧 API/SQLite 迁移兼容。
- 自我介绍与项目/实习开题严格分题，技术 / HR / 技术+HR 均按“审核主问+锚定追问”推进，项目至少三层下钻，并覆盖连续答崩提前终止、rubric 和第二场弱项记忆。
- Omni / Paraformer 事件解析、握手、二进制 PCM、取消竞态、`response.done` 状态校验、发送侧断链降级与结束语播放 ACK。
- provider-ready 上行门控、48 kHz 到 16 kHz / 100 ms 分帧、中英混合转写与播放、空转写恢复、麦克风断流与播放排空。
- 压力面只有在部分转写呈现明显表达问题且持续存在时才真实插话、语音链路超时结束，以及文字输入 barge-in。
- Silero 运行时降级与 CosyVoice / edge-tts 可注入传输。

## 数据、预算与安全边界

- SQLite 和 localStorage 使用浏览器生成的高熵匿名 Profile 密钥，没有账号或身份认证；它是匿名 capability 而不是可找回的登录凭证，清空浏览器存储后无法自动恢复同一 Profile。
- 不保存 PCM 音频；SQLite 会保存简历提取文字、结构化简历、项目文本快照、逐题转写、私有评分和报告。简历解析、项目解读和面试/报告生成会把对应文字发送给阿里云百炼；删除 Profile 资料不会反向删除已经写入历史面试的结构化快照。公开部署应明确保留周期并定期清理 `data/`。
- Caddy 把请求体限制为 12 MB；应用层再限制 PDF、项目单文件、ZIP、解压体积、文件数量与匿名 Profile 项目数，并跳过 ZIP 中的图片、构建目录和二进制附件。
- PDF 上限 8 MB；扫描件不做 OCR；简历中的“指令”会作为不可信数据包裹，不能改写 system prompt。
- 新开面试有全站 / 设备双重每日保险丝，简历解析还有 IP 滑动窗口限制。黑客松期间先用百炼免费额度，并在百炼控制台设置用量告警；达到预算时可把 `DAILY_INTERVIEW_LIMIT=0` 或直接停用新建入口。
- JavaGuide / CodeTop 资源 URL 由服务端白名单映射，不接受模型生成的任意链接。
- 面经衍生题不是官方真题，只代表公开发帖者的个人复盘或汇编；来源目录仅公开链接与元数据，不镜像第三方正文。应用没有运行时爬虫、RAG 或向量库，也不会绕过登录、验证码、robots 或反爬措施抓取小红书等社媒。

## 上游协议资料

- [快速刷题来源清单](resources/practice_source_manifest.json)：当前题目来自固定提交的 [JavaGuide](https://github.com/Snailclimb/JavaGuide)（Apache-2.0）、[interview-go](https://github.com/lifei6671/interview-go)（Apache-2.0）、[Tech Interview Handbook](https://github.com/yangshun/tech-interview-handbook)（MIT）和 [ARIS-in-AI-Offer](https://github.com/wanshuiyin/ARIS-in-AI-Offer)（MIT）。仓库只收录经审核的短题与独立整理的评分信号，不重新分发上游长答案、图片或外链材料；归因与许可证副本见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。这些是公开面试学习资料，不是招聘公司认证的官方真题。

- [ARIS-in-AI-Offer](https://github.com/wanshuiyin/ARIS-in-AI-Offer)：AI 工程后端精选题参考，固定参考提交 `6f60d72`，MIT 归因见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。应用运行时只读取人工改写的静态 JSON，不读取上游长文。

- [Qwen-Omni-Realtime 用户指南](https://help.aliyun.com/zh/model-studio/realtime)
- [Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/client-events)
- [Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/server-events)
- [Paraformer Realtime WebSocket](https://help.aliyun.com/zh/model-studio/websocket-for-paraformer-real-time-service)
- [百炼 OpenAI 兼容 Chat Completions](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [千问结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)

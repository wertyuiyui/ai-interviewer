# 多 Agent 协作进度簿

本文件是仓库内所有 Agent 的共享事实源，用于并行任务认领、文件冲突规避、验证记录和变更交接。任何代码、配置、数据、测试、文档或部署相关改动，都必须在这里留下对应记录。

## 强制协作流程

1. 开始工作前先完整阅读本文件，并确认“进行中任务”里没有其他 Agent 占用同一文件。
2. 修改任何文件前，先在“进行中任务”登记任务 ID、Agent、目标、预计文件和依赖；状态使用 `claimed`、`in_progress` 或 `blocked`。
3. 两个任务需要修改同一文件时，不得静默覆盖。先通过主 Agent 协调边界，或等占用方完成后再接手。
4. 工作中若目标、文件范围或阻塞条件发生变化，立即更新对应任务记录。
5. 完成后将任务从“进行中任务”移除，并在“变更日志”追加一条不可省略的记录，至少写明：摘要、实际文件、验证命令与结果、风险或后续事项。
6. 每个逻辑改动必须同时包含 `process.md` 更新；只改业务文件、不写本文件，视为未完成。
7. 变更日志只追加，不重写既有事实。允许修正明显笔误，但要保留原任务 ID 和时间顺序。
8. 子 Agent 默认不执行 `git add`、`git commit`、`git push` 或部署；除非主 Agent 明确授权。主 Agent 负责最终集成、全量回归和提交分组。
9. 不得在本文件记录 API Key、私钥、Cookie、完整简历、项目源码正文或其他敏感数据；只记录配置项名称、文件路径和脱敏结果。
10. 声称完成前必须运行与风险相称的测试。无法验证的部分要明确写入“风险或后续事项”，不能用默认成功代替。

## 进行中任务

| 任务 ID | Agent | 状态 | 目标 | 预计修改文件 | 依赖/冲突 |
|---|---|---|---|---|---|

登记模板：

| `TASK-XXX` | `/root/...` | `claimed` | 简短目标 | `path/a`, `path/b` | 无或任务 ID |

## 当前稳定基线

- 分支：`main`
- 当前生产已部署提交：`dd351a8`；GitHub `origin/main` 已同步至 `f049f9f`
- 线上入口：`https://39-106-146-28.sslip.io:3000`，标准 HTTPS 443 同时可用
- 运行模式：`L0`，百炼配置已就绪；敏感配置仅保存在服务器 `.env`
- 审核题库：108 个独立题目概念，中文/英文共 216 个运行变体
- 支持公司：字节跳动、美团、腾讯、阿里巴巴、百度、华为
- 支持流程：技术面、综合（HR）面、技术+综合面；中文、中英双语、纯英文
- 当前已部署功能的全量验证基线：`228 passed`
- 部署目录：`/opt/ai-interviewer-mvp`；Caddy 配置备份：`/etc/caddy/Caddyfile.pre-941100b`

## 变更日志

### INTERVIEW-003 · 2026-08-30 · 不知道跳题、两级实质提示与报告面经建议

- Agent：`/root`；协作只读审计：`/root/practice_audit`。
- 状态：`completed`
- 摘要：模拟面试答题区新增“我不知道”按钮，中文/英文明确不会表达会保留低分与具体知识缺口，但不计入连续答崩自动结束；项目深挖不再复用上一题 anchor 强追同一问题，普通场直接换题，压力场先做一句专业质疑再换题。题库主问/追问游标会消费被跳过的追问槽，恢复后继续按后续主问题推进；不确定但仍尝试分析的回答不会被误判为跳过。提示升级为两级：首次按算法、数据库、缓存、网络、项目或综合面给出具体简化思路，点击“进一步提示”后给简化示例，并明确项目事实、指标和复杂度必须替换为本人可证明内容；同题每级幂等持久化并写入报告。公开面经改写题库从字节/美团/腾讯扩展到阿里、百度、华为，新增来源均为可追溯牛客个人复盘，运行时不暴露原帖或来源元数据。报告新增“结合本次练习，优先做什么”，将低分主题与目标公司静态面经建议、样本信号交叉排序；页面改成“本次优先项—反复考点—下一轮练法—个人样本依据”四层，未评分报告不推断个人弱项。
- 实际文件：`app/content.py`、`app/db.py`、`app/interview_engine.py`、`app/prompt_engine.py`、`app/report_engine.py`、`app/schemas.py`、`public/assets/app.css`、`public/interview.html`、`public/js/interview.js`、`public/report.html`、`public/js/report.js`、`questions/recent_experience_backend.json`、`resources/source_catalog.json`、`tests/test_core.py`、`tests/test_frontend_audio_ui.py`、`tests/test_frontend_interview_controls.py`、`tests/test_frontend_report_ui.py`、`tests/test_hint_memory_retry.py`、`tests/test_interviewer_skill.py`、`process.md`。
- 验证：Python 编译、两份前端脚本 `node --check`、两份 JSON `jq empty`、`git diff --check` 均通过；不知道/提示/面经选题/报告/前端专项 `66 passed`；共享工作区全量 `231 passed, 2 failed`，两项失败均来自并发 `PROJECT-009` 已更改首页 Profile 文案但尚未同步其旧断言，不涉及本任务文件。
- 风险或后续事项：真实面经仅代表具体候选人、部门和时间点，不宣称公司官方题库；新增中文改写题不在纯英文面试中直接翻译冒充已审核英文题。提示示例是回答结构而非标准答案，项目细节仍要求候选人替换为真实经历。`PROJECT-009` 继续保留在进行中表，本提交不得包含其 `public/index.html`、`home-profile.css`、项目前端或 Profile 测试改动。

### PROFILE-002 · 2026-08-30 · 可靠简历身份、项目资料编辑与全站字体统一

- Agent：`/root`；只读协作调研/审计：`resume_skill_research`、`project_edit_audit`、`typography_audit`。
- 状态：`completed`。
- 摘要：新增项目内 `resume-reader` skill，并让真实/模拟简历解析都遵循“仅基于明确证据”的姓名规则；结构化简历新增 `姓名` 与项目链接字段，头像只显示可靠姓氏/英文首字母，无法识别统一显示 `?`，不再从文件名猜测或显示“人”。
- 项目编辑：简历解析出的项目行可创建或绑定持久档案项目；解析项目和用户上传项目共用编辑弹窗，支持修改名称、类型、负责范围，继续追加文件与 GitHub/arXiv 链接；关联关系写入 SQLite，追加资料使用内容哈希防并发覆盖并清理旧分析缓存。
- 前端：完善项目编辑弹窗、关联状态、已有资料展示和移动端布局；全站 sans/serif/mono 字体统一为本地字体变量，修复按钮字号继承、垂直居中、行高和不自然换行，并刷新相关静态资源版本。
- 实际文件：`analysis_skills/resume-reader/SKILL.md`；`app/main.py`, `app/schemas.py`, `app/resume.py`, `app/profile.py`, `app/profile_routes.py`；`public/assets/{app,profile,project,coding}.css`, `public/js/{home,profile}.js`, `public/{index,interview,practice,report,project,coding,profile}.html`；`tests/test_api.py`, `tests/test_core.py`, `tests/test_profile.py`, `tests/test_profile_api.py`, `tests/test_frontend_home_ui.py`, `tests/test_frontend_interview_controls.py`, `tests/test_frontend_profile_project_ui.py`；`process.md`。
- 验证：`python3 .../quick_validate.py analysis_skills/resume-reader` → `Skill is valid!`；`.venv/bin/python -m compileall -q app`、`node --check public/js/profile.js`、`node --check public/js/home.js` 通过；`PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `228 passed`；`git diff --check` 通过。
- 风险/后续：姓名刻意采用保守策略，版式特殊且没有明确姓名标签/页眉联系方式的简历会显示 `?`，用户可通过更规范的简历文本改善识别；GitHub/arXiv 追加仍依赖公开可读取资源与外部服务可用性。

### DEPLOY-009 · 2026-08-30 · 个人档案项目编辑与字体统一生产发布

- Agent：`/root`。
- 状态：`completed`。
- 摘要：以 `git archive dd351a8` 生成固定发布快照并同步到 `/opt/ai-interviewer-mvp`；同步前创建 `/tmp/ai-interviewer-mvp-pre-dd351a8-20260830.tar.gz` 回滚包，明确保留生产 `.env`、`data/`、`.venv/`、`.deps/`、`.git/` 和缓存，只重启 `ai-interviewer-3000.service`，未重启 Caddy 或其它服务。
- 文件：生产目录中的固定提交 `dd351a8`；生产密钥、SQLite 数据、依赖与 Caddy 配置未修改。
- 验证：发布前全量回归 `228 passed`；服务最终为 `active/running`，主进程 PID `657364`；`http://127.0.0.1:8000/healthz`、正式域名 HTTPS 443 与 3000 均返回 `{"status":"ok"}`；生产 `/profile`、`profile.js` 与 `app.css` 已确认包含项目编辑弹窗、“编辑并添加资料”和统一字体变量，HTML 使用 `20260830-profile-edit-v1` 静态资源版本；生产工作目录保持 0755。
- 风险/后续：服务重启后首次即时健康请求处于正常启动窗口、短暂返回连接拒绝，约 1 秒后复检及外部 HTTPS 均稳定通过；GitHub `origin/main` 推送仍因目标仓库归属未被可信用户内容明确确认而被安全审查拒绝，未绕过，当前本地分支领先远程 3 个提交。

### PROJ-009 · 2026-08-30 · 项目主线解读与候选人介绍净化

- Agent：`/root`；协作只读审计：`/root/practice_audit`。
- 状态：`completed`
- 摘要：将“核心请求链路”明确为从触发或输入，经核心模块、数据或外部依赖到可观察结果的一条代表性端到端主线，并按应用、技术、论文分别显示业务流程、运行链路、方法与实验链。新增仓库阅读 skill，采用 Detect → Trace → Synthesize：先定位入口和主模块，再沿真实源码追踪主线，最后生成候选人叙述与深挖题。应用类前两题优先用户问题、设计动机与核心功能，技术类前两题优先技术约束、核心机制和正确性/性能边界；存在核心源码时过滤仅引用 Dockerfile、依赖、CI 或部署配置的模型题，配置只在缺少更强实现证据时作为运行背景。项目介绍只保留目标、动机、功能/方法、职责、实现主线、亮点与结果，不再混入材料不足、验证依据、待核实或补充源码等后台审计内容；论文默认以阅读、方法理解和复现评估视角介绍，不冒充论文作者或整体实现者。缓存 schema 升至 v5，避免复用旧解读。
- 文件：`analysis_skills/repository-reader/SKILL.md`、`app/profile.py`、`public/project.html`、`public/js/project.js`、`tests/test_profile.py`、`tests/test_frontend_profile_project_ui.py`、`process.md`。
- 验证：repository-reader skill quick validator 通过；`node --check public/js/project.js`、Python 编译、`git diff --check` 通过；Profile/Profile API/项目前端专项 `50 passed`；全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `224 passed`。
- 风险或后续事项：请求链路仍基于静态材料，不执行用户代码；证据完整性继续显示在独立检查区，但不会进入候选人讲稿。未进行付费真实模型调用或浏览器视觉回归。与已登记 `PROFILE-002` 的共享文件已释放，后续任务可基于本提交继续修改，禁止覆盖本次主线排序与介绍净化逻辑。

### DEPLOY-007 · 2026-08-30 · 项目主线解读生产发布

- Agent：`/root`。
- 状态：`completed`
- 摘要：将固定提交 `4238bda` 推送至既有 GitHub `origin/main`，并由 `git archive` 生成隔离发布快照同步到 `/opt/ai-interviewer-mvp`；未使用包含并发 `HOME-001`、`PROFILE-002` 改动的共享工作区。同步前创建 `/tmp/ai-interviewer-mvp-pre-4238bda-20260830.tar.gz` 回滚包，明确保留生产 `.env`、`data/`、`.venv/`、`.deps/`、`.git/` 和缓存，只重启 `ai-interviewer-3000.service`，未重启 Caddy 或其它进程。
- 文件：GitHub `main` 与生产目录中的固定提交 `4238bda`；生产密钥、SQLite 数据、依赖和其它协作任务文件未修改。
- 验证：目标服务为 `active`，新主进程 PID `645616`；`http://127.0.0.1:8000/healthz` 返回正常，生产 `/project` 包含“架构与核心流程”和 `project-mainline-v1`；绕过服务器代理并以正式域名证书/SNI 直连本机 Caddy 后，HTTPS 443 与 3000 均返回 `{"status":"ok"}`。
- 风险或后续事项：未触发付费项目重新解读，既有项目需用户主动“重新解读”后才会生成 v5 缓存；服务器代理路径仍会导致既有 TLS EOF，绕过代理的真实 Caddy 监听、证书和两处入口均已验证正常。

### PROC-001 · 2026-08-30 · 建立共享协作台账

- Agent：`/root`
- 状态：`completed`
- 摘要：新增统一的多 Agent 任务认领、冲突规避、验证和交接规则；增加仓库级入口，要求所有后续 Agent 在改动前后共同维护本文件。
- 文件：`process.md`、`AGENTS.md`
- 验证：Markdown 结构人工检查；`git diff --check`。
- 提交主题：`docs: add shared multi-agent process ledger`
- 风险或后续事项：后续每个逻辑改动都必须附带新的日志条目；不得把密钥或用户资料写入本文件。

### PROJ-004 · 2026-08-30 · 阻断项目元规则题面并修正完整面试锚定

- Agent：`/root/project_prompt_audit`
- 状态：`completed`
- 摘要：确认 README/说明文档中的产品流程被分析模型误当项目题面，并阻断 `interview_questions`、建议答案及 skill/system 元规则进入完整面试；选中的 Profile 项目改为优先经历，职责、架构、按序请求链路和证据边界进入只读快照，服务端继续强制自我介绍与 3/4 层下钻状态机。
- 文件：`app/project_context.py`、`app/prompt_engine.py`、`app/interview_engine.py`、`tests/test_core.py`、`tests/test_interview_type_bank.py`。
- 验证：`git diff --check`；`python3 -m compileall -q app`；相关核心、面试类型和英文流程测试 `44 passed`。
- 风险或后续事项：Profile 项目顺序从“追加到末尾”改为“选中项目优先”，旧测试中的末尾索引断言需由 Profile 集成任务同步到新语义；最终仍需全量回归。

### PROJ-003 · 2026-08-30 · 项目解读职责、进度与深挖练习交互

- Agent：`/root/project_practice_ui`
- 状态：`completed`
- 摘要：首页和项目页可填写或后续修改“我负责的”，也可从架构组件勾选合并；上传与分析展示真实阶段和失败状态；架构区补充面试可用介绍与链路核验；深挖题只显示带项目证据且不含元规则的内容，并支持更多题目和重新生成。
- 文件：`public/project.html`、`public/js/project.js`、`public/assets/project.css`、`public/index.html`、`public/js/home.js`、`public/assets/app.css`、`tests/test_frontend_profile_project_ui.py`。
- 验证：`node --check`（项目页与首页脚本）；Profile/API/UI 相关测试 `45 passed`；路径限定 `git diff --check`。
- 风险或后续事项：依赖 Profile 后端最终 NDJSON 阶段、职责 PATCH 和题目生成契约；须在集成后再次验证流式解析、缓存失效和空链路展示。

### PROJ-002 · 2026-08-30 · 项目职责、证据化解读与深挖题 API

- Agent：`/root/project_analysis_backend`
- 状态：`completed`
- 摘要：为 Profile 项目增加可迁移的职责字段及更新接口，职责变更会使旧缓存失效；项目解读 schema 升级为 v2，并通过 NDJSON 返回读取、上下文准备、生成、证据校验和保存的真实阶段。GitHub 与分析上下文改用架构感知抽样，服务端只保留带实现代码或配置证据的架构、请求链路和追问，输出链路核验状态与受职责边界约束的面试介绍；新增带去重和证据校验的更多/重新生成题目接口。
- 文件：`app/profile.py`、`app/profile_routes.py`、`tests/test_profile.py`、`tests/test_profile_api.py`。
- 验证：Profile/API 专项 `34 passed`；联合项目 UI、核心面试、面试类型和英文流程测试 `76 passed`；Python 编译与 `git diff --check` 通过。主 Agent 接手后的扩大回归为相关测试 `84 passed`。
- 风险或后续事项：分析只核对上传快照中的静态实现路径，不运行候选人代码；因此链路状态会保守标记为“部分核对”或“待核实”，不能等同于运行时追踪。

### PRACTICE-104 · 2026-08-30 · 快速刷题契约与边界只读审计

- Agent：`/root/practice_audit`
- 状态：`completed`
- 摘要：只读检查快速刷题会话 API、题库来源投影、Profile 聚合能力、前端状态流和既有测试；向主 Agent 交付无限循环优先级、跳过语义、错题判定/去重/删除、真题与 AI 标识、实质提示及零分反馈归类的可测试契约，并指出两个禁止公开来源的旧测试与新需求冲突。
- 文件：`process.md`（仅任务登记与完成日志；未修改业务文件或测试）。
- 验证：完整阅读 `AGENTS.md`、`process.md`；静态检查 `app/practice.py`、`app/main.py`、`app/profile.py`、`app/profile_routes.py`、`public/practice.html`、`public/js/practice.js` 及相关测试/题库/来源清单；未运行测试——尝试 `pytest -q tests/test_practice.py tests/test_real_practice_bank.py tests/test_frontend_practice_ui.py` 时，当前环境返回 `pytest: command not found`。
- 风险或后续事项：公开题目必须只从来源清单映射安全的 `source_label/source_url`，不得泄露内部 provenance、source_path、revision、license 或 scoring；当前题库的 `licensed_bank` 不等同于已核验公司独家面经；Profile 的错题聚合仍需主 Agent 与实现 Agent 集成并执行完整回归。

### PRACTICE-102 · 2026-08-30 · 快速刷题无限模式与错题本前端

- Agent：`/root/practice_frontend`
- 状态：`completed`
- 摘要：快速刷题新增无限题量和手动结束、跳过、无限进度语义；在刷题设置内增加“个人 Profile · 错题本”列表和确认删除；题面展示【真题】/【AI出题】/【错题重答】及安全公开来源；兼容新旧来源字段，并在前端兜底把误放在优点栏的“未完成/缺少”类反馈归入扣分点。
- 文件：`public/practice.html`、`public/js/practice.js`、`public/assets/practice.css`、`tests/test_frontend_practice_ui.py`、`process.md`。
- 验证：`node --check public/js/practice.js` 通过；`PYTHONPATH=.deps .venv/bin/python -m pytest -q tests/test_frontend_practice_ui.py tests/test_practice.py` → `13 passed in 1.85s`；`git diff --check` 通过。
- 风险或后续事项：未进行真实浏览器视觉回归；主 Agent 集成时需在后端任务完成后执行全量测试，并核对 `EXT-001` 是其他 Agent 对本任务改动的误判后再清理该记录。

### PRACTICE-101 · 2026-08-30 · 快速刷题无限模式、错题本与反馈后端

- Agent：`/root/practice_backend`
- 状态：`completed`
- 摘要：快速刷题创建契约向后兼容地支持 `infinite=true,count=null`，答题/跳过到队尾自动补题并提供归属校验、幂等手动结束；低分（仅已评分且 `score<=6`）按跨语言 canonical key 默认持久化错题本、支持列表/手动删除，无评分和跳过不入库且高分不自动删除；无限模式优先当前筛选内的错题且避免相邻重复，约每四次续题尝试生成一题明确标为【AI出题】的仿真题，生成失败回退授权真题；真题只公开来源清单映射的标题/仓库 URL 与【真题】标签；提示按审核 key points/red flags 递进，低分空扣分自动补充，零分回答中误置于优点栏的否定项移入扣分项。
- 文件：`app/practice.py`、`app/main.py`、`tests/test_practice.py`、`tests/test_real_practice_bank.py`、`process.md`。
- 验证：`PYTHONPATH=.deps .venv/bin/python -m pytest -q tests/test_practice.py tests/test_real_practice_bank.py tests/test_frontend_practice_ui.py` → `22 passed in 1.79s`；`PYTHONPATH=.deps .venv/bin/python -m compileall -q app` 通过；`git diff --check` 通过。
- 风险或后续事项：遵守 `PROJ-002` 文件占用，未修改 `profile.py/profile_routes.py`；个人 Profile 页面通过独立的 `/api/practice/mistakes` API 展示错题。与并发 `CODING-001` 的 `drill_type` schema/insert/public 字段兼容，无限续题会保留原 `drill_type` 且手撕代码模式不混入 AI 题；主 Agent 仍需执行全量回归和真实浏览器检查。

### PRACTICE-100 · 2026-08-30 · 快速刷题体验修复集成验收

- Agent：`/root`
- 状态：`completed`
- 摘要：完成无限题量与手动结束、跳过、匿名个人 Profile 错题本及删除、错题优先续题、安全真题来源与【真题】/【AI出题】标识、题目特定递进提示，以及零分/低分反馈归类的前后端集成；审核并保留并发加入的 `drill_type` 扩展。此前 `EXT-001` 所列文件来源已确认是 `PRACTICE-101/102`、`CODING-001` 与 `SKILL-001` 的登记改动，因此移除误判的未知协作者占用行。
- 文件：`app/practice.py`、`app/main.py`、`public/practice.html`、`public/js/practice.js`、`public/assets/practice.css`、`tests/test_practice.py`、`tests/test_real_practice_bank.py`、`tests/test_frontend_practice_ui.py`、`process.md`。
- 验证：`PYTHONPATH=.deps .venv/bin/python -m pytest -q tests/test_practice.py tests/test_real_practice_bank.py tests/test_frontend_practice_ui.py` → `26 passed in 2.31s`（包含并发专项兼容测试）；`node --check public/js/practice.js`、`PYTHONPATH=.deps .venv/bin/python -m compileall -q app`、`git diff --check` 均通过；全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `192 passed, 1 failed`，唯一失败为仍在进行中的 `PROJ-002/003` 项目快照断言 `tests/test_profile_api.py::test_profile_routes_project_analysis_and_interview_snapshot`，不涉及快速刷题文件。
- 风险或后续事项：未进行真实浏览器视觉回归；当前错题本按匿名 `client_id` 持久化并在快速刷题页的“个人 Profile · 错题本”区域管理，未修改仍被 `PROJ-002/003` 占用的首页 Profile 文件；全量回归需在这些并发任务完成后再复跑。

### SKILL-001 · 2026-08-30 · 公司无关的核心面试官 skill

- Agent：`/root`（协作审阅：`/root/process_review`、`/root/reference_review`、`/root/architecture_review`）。
- 状态：`completed`
- 摘要：综合资源目录 D 节多个 AI 模拟面试项目的共性，沿用本项目 FastAPI + 审核题库 + 服务端状态机 + JSON skill 架构，新增公司无关的核心面试官契约；统一本科实习候选人尺度、输入可信边界、单意图锚定追问、难度自适应、证据与 ownership、题库边界、私有评分、`not_observed`、服务端终止权、反偏见、文字/语音一致性、安全和来源边界。六份公司 skill 运行时均嵌套该核心契约，公司配置仅保留可变风格偏好。独立前向测试后修正“明确不知道”复用历史 topic context 与本轮 evidence anchor 的语义冲突，并明确其属于可观察负面证据而非 `not_observed`。
- 文件：`interview_skills/interviewer_core.json`、`app/content.py`、`app/prompt_engine.py`、`tests/test_interviewer_skill.py`、`references/INTERVIEWER_SKILL_DESIGN.md`、`process.md`。
- 验证：`PYTHONPATH=.deps .venv/bin/python -m pytest -q tests/test_interviewer_skill.py tests/test_english_company_flow.py tests/test_interview_type_bank.py` → `24 passed`；`PYTHONPATH=.deps .venv/bin/python -m pytest -q tests/test_core.py::test_prompt_contains_non_negotiable_interview_rules tests/test_core.py::test_three_layer_drill_early_end_report_and_memory` → `2 passed`；核心 JSON 解析、相关 Python compileall、`git diff --check` 均通过；全量测试 → `192 passed, 1 failed`，唯一失败是并行 Profile 项目快照旧顺序断言，`PROJ-004` 已记录需由 Profile 集成任务更新，与本任务文件无关。
- 风险或后续事项：没有复制 GPL/无许可证项目的代码、prompt 或题库，也未引入 LangGraph、RAG、LiveKit 或供应商绑定；核心契约在 prompt 中拥有独立且高于公司风格的区段，关键 phase、审核题面、压力和终止仍由服务端硬状态机掌权。若后续进一步压缩每轮 token，应在 prompt 编译阶段裁剪文档性字段，而不是削弱服务端约束。

### CODING-001 · 2026-08-30 · 基于真实授权题库的手撕代码专项

- Agent：`/root`（结合 `/root/flow_audit`、`/root/question_bank_audit`、`/root/test_audit` 三个只读审计 Agent 的交付）
- 状态：`completed`
- 摘要：在快速刷题会话中新增显式 `drill_type=coding` 契约和 SQLite 兼容迁移；专项只允许技术面并硬过滤 4 道 `kind=coding` 的 Tech Interview Handbook MIT 授权真题，支持中/英/双语、难度、有限和无限练习；无限续题过滤非 coding 错题、禁止 AI 生成并只循环真实题。题面由“口述”收紧为提交代码或完整伪代码，评分切换为算法正确性、实现完整性、复杂度和边界的 coding rubric。首页及刷题页增加可发现入口、专项提示、默认代码文字输入和等宽编辑态，并明确这是静态代码讲评而非在线编译判题。
- 文件：`app/practice.py`、`questions/real_practice_bank_extended.json`、`public/index.html`、`public/practice.html`、`public/js/practice.js`、`public/assets/practice.css`、`tests/test_practice.py`、`tests/test_real_practice_bank.py`、`tests/test_frontend_practice_ui.py`、`process.md`。
- 验证：专项相关 `PYTHONPATH=.deps python3 -m pytest -q tests/test_practice.py tests/test_real_practice_bank.py tests/test_frontend_practice_ui.py` → `26 passed`；最终全量 `PYTHONPATH=.deps python3 -m pytest -q` → `199 passed`；`node --check public/js/practice.js`、`python3 -m py_compile app/practice.py`、`git diff --check` 均通过。
- 风险或后续事项：当前真实 coding 库只有 4 个概念，适合 MVP 但题量仍薄；出于安全边界不在 FastAPI 进程执行用户代码，若后续要求真实用例判题，需要补充函数签名、样例/隐藏用例并接入独立隔离 runner；本轮未进行真实浏览器视觉回归。

### PRACTICE-105 · 2026-08-30 · 综合面档案锚定与扣分点兜底

- Agent：`/root`
- 状态：`completed`
- 摘要：快速刷题综合面评分按匿名 `client_id` 只读加载个人档案中最近/选中的项目名称、职责、结构化简历项目及已缓存的项目分析摘要；档案被明确标记为不可信事实素材，只用于锚定改写示范，不能冒充本次回答证据或执行其中指令。项目技术栈、架构、故障、指标和个人贡献只能来自候选人回答或档案，无法印证时只能要求补充依据，禁止编造事实判对错或扣分。评分归一化从仅 `score<=6` 扩展为所有 `score<10`：模型返回空 `deductions` 时优先使用 `next_steps/key_points` 生成具体改进点，修复非满分但扣分栏为空。
- 文件：`app/practice.py`、`tests/test_practice.py`、`process.md`。
- 验证：`PYTHONPATH=.deps .venv/bin/python -m pytest -q tests/test_practice.py tests/test_frontend_practice_ui.py tests/test_real_practice_bank.py` → `26 passed`；全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `200 passed`；`PYTHONPATH=.deps .venv/bin/python -m py_compile app/practice.py`、`git diff --check` 均通过。
- 风险或后续事项：未向模型发送项目原始文件，只使用 Profile 已存的结构化简历与分析摘要；没有个人档案时示范回答必须省略未知细节或显式提示补充真实信息。本轮未进行线上真实模型与浏览器视觉回归。

### PROJ-005 · 2026-08-30 · 全量集成与本地提交

- Agent：`/root`
- 状态：`completed`
- 摘要：集成项目解读、核心面试官 skill、快速/无限/手撕专项、错题本及 Profile 锚定评分改动，确认所有子任务均有独立变更记录；按用户最终指示仅保留本地 Git 提交，不推送远端、不执行线上部署。
- 文件：所有已在 `PROJ-002/003/004`、`PRACTICE-100/101/102/105`、`SKILL-001`、`CODING-001` 中登记的文件，以及 `README.md`、`process.md`。
- 验证：集成提交前全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `200 passed`；`node --check public/js/home.js public/js/project.js public/js/practice.js` 分别通过；Python compileall 与 `git diff --check` 通过。
- 提交：`9e927fb`（完整功能集成）、`a3e321e`（Profile 项目分析收口）；本条进度记录单独提交。
- 风险或后续事项：本地 `main` 尚未推送 `origin/main`，线上服务未更新；后续如需上线，应先推送并在保留 `.env`、`.deps` 与 `data/` 的前提下仅重启 `ai-interviewer-3000.service`，随后验证本地 8000 与 HTTPS 443/3000。

### PROJ-006 · 2026-08-30 · Profile 静态证据保守化收口

- Agent：`/root`
- 状态：`completed`
- 摘要：集成首次生产同步后出现的 Profile 安全收口：GitHub/上传项目的分析上下文只保留一份 README 并优先实现层文件；架构、请求链路、技术选型和风险字段继续过滤项目实现以外的 prompt/skill/服务端控制规则；仅有真实文件路径不能证明模型描述的组件、动作和执行顺序，链路核验最高保持“部分核验”，面试介绍同步使用保守措辞。
- 文件：`app/profile.py`、`tests/test_profile.py`、`process.md`。
- 验证：Profile 专项 `36 passed`；最终全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `202 passed`；Python compileall 与 `git diff --check` 通过。提交：`6064b47`（`fix: keep project analysis evidence conservative`）。
- 风险或后续事项：仍不执行候选人代码，静态分析只提供待面试核实的候选链路；增量发布前线上旧 `app/profile.py` 已备份为 `/tmp/ai-interviewer-profile-pre-6064b47.py`。

### DEPLOY-001 · 2026-08-30 · 生产同步与健康检查

- Agent：`/root`
- 状态：`completed`
- 摘要：按用户上线指示，将已登记的完整发布候选同步至 `/opt/ai-interviewer-mvp`。首次同步前创建 `/tmp/ai-interviewer-mvp-pre-a3e321e-20260830.tar.gz` 可回滚代码备份，rsync 明确排除并保留生产 `.env`、`data/`、`.venv/`、`.deps/`、`.git/`；随后增量同步 `6064b47` 的 Profile 安全收口。两次均只重启 `ai-interviewer-3000.service`，未重启 Caddy 或其它系统服务。
- 文件：生产目录中所有本轮已登记代码、静态资源、题库、测试与文档；生产密钥、数据库和依赖目录未改动。
- 验证：最终服务为 `active/running`，主进程 PID `571421`；`http://127.0.0.1:8000/healthz`、`https://39-106-146-28.sslip.io/healthz`、`https://39-106-146-28.sslip.io:3000/healthz` 均返回 `{"status":"ok"}`；公网真实请求已成功访问 `/practice?drill=coding`、静态资源、`/api/practice/catalog`、`/api/practice/mistakes` 与 `/api/config`，响应为 `200`。
- 风险或后续事项：单 worker 重启窗口内 Caddy 曾记录一次短暂 `502`，应用启动后恢复正常；Caddy 与其它进程未受重启。`origin/main` 当前为 `a3e321e`；部署记录 `9bffadf` 与最终安全修正 `6064b47` 尚未推送，因为自动安全审查无法验证 `https://github.com/wertyuiyui/ai-interviewer.git` 的归属。生产目录已包含全部改动；若需继续同步 GitHub，用户需明确确认该仓库的 `main` 分支是授权推送目标。

### PROJ-007 · 2026-08-30 · ZIP 配额与证据定位符最终收口

- Agent：`/root`
- 状态：`completed`
- 摘要：大型 ZIP 的 100 文件预算固定保留一份根 README 作为上下文，随后优先实现/配置文件，避免大量嵌套 README 或测试挤掉真实源码；选中后仍恢复兼容的 README-first 稳定输出顺序。项目分析的模型证据定位符只有在行号真实存在或符号能在对应文件内容中命中时才保留，否则降级为路径级证据；候选人职责介绍同时避免“主要负责 负责……”重复措辞。
- 文件：`app/profile.py`、`tests/test_profile.py`、`process.md`。
- 验证：Profile/API 专项 `37 passed`；最终全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `203 passed`；Python compileall 与 `git diff --check` 通过。首次实现暴露了根 README 被一并挤掉的问题，修正配额优先级后现有兼容测试与新增实现保留测试均通过；失败中间态未部署。
- 风险或后续事项：路径级证据仍只证明文件存在，不证明模型描述的运行行为；生产增量同步后需再次验证三处健康端点，且不修改数据库、依赖或其它服务。

### PROJ-008 · 2026-08-30 · 项目分析缓存 v3 失效边界

- Agent：`/root`
- 状态：`completed`
- 摘要：项目分析缓存 schema 从 v2 提升至 v3，使链路最高仅部分核验、元规则过滤、职责关联、证据定位符校验和项目介绍文案等新语义立即生效；旧 v2 记录自然不命中并按当前项目快照重建，无需删除用户项目或历史数据。
- 文件：`app/profile.py`、`tests/test_profile.py`、`process.md`。
- 验证：Profile/API 专项 `38 passed`；全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `204 passed`；Python compileall 与 `git diff --check` 通过。
- 风险或后续事项：首次重新解读旧项目会产生一次新的百炼分析调用；v2 缓存保留在 SQLite 中但不会被读取，后续可在单独的数据维护窗口清理。

### CODING-002 · 2026-08-30 · 独立手撕代码工作台与首页单项练习架构

- Agent：`/root`
- 状态：`completed`
- 摘要：基于公开代码面试流程、四维评价 rubric 与 Grind 75 策展清单，将手撕代码从八股快刷中完整拆出；首页改为并列的“模拟面试 / 单项练习”，单项练习内并列八股快速刷题、手撕代码、项目解读。新增 8 道独立改写的经典手撕题、来源政策清单、Python/Java/Go/JavaScript 函数签名、澄清—方案—编码—自测分阶段工作台、按语言隔离的浏览器草稿、递进提示及沟通/解题/技术实现/测试四维静态复盘 API；快刷前端移除旧 `drill_type=coding` 入口，后端兼容字段保留以避免破坏旧请求。
- 文件：`app/coding_practice.py`、`app/main.py`、`questions/coding_practice_bank.json`、`resources/coding_source_manifest.json`、`public/coding.html`、`public/js/coding.js`、`public/assets/coding.css`、`public/index.html`、`public/practice.html`、`public/js/practice.js`、`public/assets/practice.css`、`public/assets/app.css`、`tests/test_coding_practice.py`、`tests/test_frontend_coding_ui.py`、`tests/test_frontend_practice_ui.py`、`references/CODING_PRACTICE_DESIGN.md`、`README.md`、`process.md`。
- 验证：JSON 解析通过；`node --check public/js/coding.js public/js/practice.js` 通过；`PYTHONPATH=.deps .venv/bin/python -m compileall -q app` 通过；手撕/首页/快刷前端专项 `14 passed`；全量回归 `211 passed, 2 failed`，两项失败均来自仍在进行的 `NAV-001` 新测试，要求其尚未完成的全站三项导航和 `openProfileFromHash`，不涉及本任务功能；`git diff --check` 通过。
- 风险或后续事项：MVP 明确不执行候选人代码，`execution_status` 固定为 `not_executed`；若加入真实判题必须另接隔离 runner。未做真实浏览器视觉回归、未 push、未部署；提交时排除 `NAV-001` 正在修改的 `public/project.html`、`public/report.html`、`tests/test_frontend_navigation.py`，并在完成本任务后释放首页、快刷、coding 与 `app.css` 供其接续统一导航。

### DEPLOY-002 · 2026-08-30 · 项目解读最终增量发布

- Agent：`/root`
- 状态：`completed`
- 摘要：将 `6fc90fd`、`b8e88cf` 的 ZIP 源码保留、证据定位符和项目分析缓存 v3 修复推送至用户指定 GitHub `main`，并把已提交的 `app/profile.py` 增量同步到生产目录；未暂存、推送或部署仍在开发的 `CODING-002` 工作区文件。生产旧模块备份为 `/tmp/ai-interviewer-profile-pre-b8e88cf.py`，`.env`、SQLite 数据、依赖和 Caddy 配置均未修改。
- 文件：GitHub `main` 至 `b8e88cf`；生产 `/opt/ai-interviewer-mvp/app/profile.py`；`process.md`。
- 验证：`ai-interviewer-3000.service` 为 `active`，主进程 PID `585880`；本机 8000 健康端点正常；用正式域名 SNI 与证书分别直连本机 Caddy 443、HTTPS 3000，均返回 `{"status":"ok"}`；生产 `/project` 已包含职责输入、链路核验、面试介绍、更多题目和重新生成控件，生产脚本已包含 NDJSON 流式分析；生产模块确认 `PROJECT_ANALYSIS_SCHEMA_VERSION = "3"`。
- 风险或后续事项：服务器自身通过公网地址回环访问时 TLS 被网络路径提前断开，但 Caddy 服务正常、443/3000 均在监听，使用相同正式域名和证书直连本机 Caddy 验证成功；未执行付费的真实项目重新分析，避免为健康检查消耗百炼额度。

### NAV-001 · 2026-08-30 · 顶部三入口导航整合迁移

- Agent：`/root`
- 状态：`completed`
- 摘要：首页、八股快速刷题、项目解读、独立手撕代码和报告五个主页面的顶部导航统一为“首页 / 个人档案 / 历史报告”三个入口；快速刷题、项目解读和手撕代码仍通过首页正文模块进入。个人档案入口复用现有匿名 Profile 面板并通过 hash 自动展开/定位，不新增数据流；报告页原“本次报告 / 历史记录”切换从顶部迁移到报告正文，保留原按钮 ID 与脚本契约。活动面试页继续保留公司、连接状态和计时运行栏，未删除会话功能。
- 文件：`public/index.html`、`public/practice.html`、`public/project.html`、`public/coding.html`、`public/report.html`、`public/js/home.js`、`public/assets/app.css`、`tests/test_frontend_navigation.py`、`tests/test_frontend_practice_ui.py`、`process.md`。
- 验证：新增导航契约测试 `3 passed`；全部前端测试 `32 passed`；全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `213 passed`；`node --check public/js/home.js public/js/report.js` 分别通过；`git diff --check` 通过。
- 风险或后续事项：只迁移入口位置与报告视图按钮，不改 API、Profile 持久化、练习流程或报告切换逻辑；未进行真实浏览器视觉回归。按用户先前指示仅创建本地提交，不推送、不部署。

### DEPLOY-003 · 2026-08-30 · 手撕工作台与三入口导航生产发布

- Agent：`/root`
- 状态：`completed`
- 摘要：按用户授权，将 `c48bd13` 与 `5eacadf` 推送至既有 GitHub `main`，并以固定提交快照 `5eacadf` 同步生产；部署明确不包含工作区中仍在进行的 `PAPER-001`、`CODING-003` 未提交文件。同步前创建 `/tmp/ai-interviewer-mvp-pre-5eacadf-20260830.tar.gz` 代码备份，保留生产 `.env`、`data/`、`.venv/`、`.deps/` 与 `.git/`，只重启 `ai-interviewer-3000.service`，未重启 Caddy 或其它进程。
- 文件：GitHub `main` 至 `5eacadf`；生产目录中的提交快照文件；`process.md`。生产密钥、数据库和依赖目录未改动。
- 验证：应用服务为 `active`，新主进程 PID `602081`；`http://127.0.0.1:8000/healthz` 返回正常；绕过服务器本机代理、以正式域名 SNI 直连 Caddy 实际网卡后，443 与 HTTPS 3000 均返回 `{"status":"ok"}`；生产 `/coding`、`/practice`、`/report` 已确认三入口导航与报告正文标签，新 `coding.css`、`coding.js` 均返回 `200`。
- 风险或后续事项：本机代理路径及公网地址自回环仍会提前中断 TLS，实际 Caddy 监听与正式域名证书直连验证正常；回滚代码包如上。进行中的论文解读和手撕后续增强保持未提交、未推送、未部署状态。

### CODING-003 · 2026-08-30 · 手撕改写代码、真实时长与上一题

- Agent：`/root`
- 状态：`completed`
- 摘要：手撕四维复盘新增独立的代码/伪代码改写示范字段与等宽代码块；真实模型被约束为优先输出所选语言的完整代码，信息不足时才使用完整伪代码，离线及失败回退使用题库内审阅伪代码。参考完整代码面试常见 30–45 分钟轮次及方案讨论耗时，将 8 道题从原 15–30 分钟调整为基础 30–35 分钟、进阶 40–45 分钟，并在页面明确这是包含澄清、方案、编码和自测的完整流程时长。手撕工作台切换上一题前保存分语言草稿；八股快刷增加上一题只读回看与回到当前题，保留服务端单向待答游标并支持已提交反馈、草稿和已跳过题状态。
- 文件：`app/coding_practice.py`、`questions/coding_practice_bank.json`、`public/coding.html`、`public/js/coding.js`、`public/assets/coding.css`、`public/practice.html`、`public/js/practice.js`、`public/assets/practice.css`、`tests/test_coding_practice.py`、`tests/test_frontend_coding_ui.py`、`tests/test_frontend_practice_ui.py`、`references/CODING_PRACTICE_DESIGN.md`、`process.md`。
- 验证：题库 JSON 解析、`python -m py_compile app/coding_practice.py`、`node --check public/js/coding.js public/js/practice.js`、`git diff --check` 均通过；手撕/快刷专项 `25 passed`。全量回归 `211 passed, 2 failed`，两项失败均来自进行中的 `PAPER-001` 已将缓存 schema 改为 v4、但其旧测试仍断言 v3，与本任务文件无关。
- 风险或后续事项：上一题是前端会话内导航，刷新页面后仍以服务端当前题为准；快刷历史题只读，避免服务端游标错位。代码仍不在应用进程执行，改写示范也不代表已通过编译或测试。未修改、暂存、推送或部署 `PAPER-001` 文件，本任务仅本地提交，不 push、不部署。

### PAPER-001 · 2026-08-30 · 论文/项目解读实现与发布候选

- Agent：`/root`
- 状态：`in_progress`（实现与验证完成，等待提交、推送和生产同步）
- 摘要：将“项目解读”升级为“论文/项目解读”。新增应用类、技术类、论文三种类型；默认负责整个项目，只有显式选择部分负责或勾选生成的核心组件时才收窄责任范围；支持一个条目最多 5 个 GitHub/arXiv 链接及论文 PDF，旧单 GitHub API 保持兼容。论文使用独立 `paper-reader` skill，按范围、理解、质疑三遍阅读法区分作者主张、实验依据、局限和可复现性；三类材料分别聚焦设计动机、技术机制或论文贡献。架构/核心组成在模型遗漏时会按真实证据路径补齐，介绍扩展为包含动机、核心功能/方法、亮点、验证和责任范围的完整叙述，深挖题围绕核心功能并服从部分责任边界。
- 文件：`analysis_skills/paper-reader/SKILL.md`、`app/profile.py`、`app/profile_routes.py`、`public/project.html`、`public/js/project.js`、`public/assets/project.css`、`public/index.html`、`public/js/home.js`、`public/assets/app.css`、`tests/test_profile.py`、`tests/test_profile_api.py`、`tests/test_frontend_profile_project_ui.py`、`README.md`、`process.md`。
- 验证：论文 skill quick validator 通过；`PYTHONPATH=.deps .venv/bin/python -m compileall -q app`、`node --check public/js/home.js public/js/project.js`、`git diff --check` 通过；最终全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `215 passed`。
- 风险或后续事项：论文 PDF 仅做文本抽取，不进行版面视觉理解，复杂公式/图表仍需结合原文核实；arXiv 抓取只允许固定官方主机且不跟随重定向，重复解读可能触发一次新的百炼调用。生产同步必须保留 `.env`、`data/`、`.venv/`、`.deps/` 和 `.git/`，只重启目标应用服务并复核三处健康端点。

### PAPER-001 · 2026-08-30 · 论文/项目解读完成

- Agent：`/root`
- 状态：`completed`
- 摘要：发布候选提交为 `4e29845`（`feat: add paper and typed project analysis`），实现范围、测试结果和剩余 PDF 版面局限见上一条同 ID 记录。任务已从进行中表移除，生产同步与健康检查见 `DEPLOY-004`。
- 文件：与上一条 `PAPER-001` 相同；本条额外更新 `process.md` 的稳定基线和任务状态。
- 验证：提交前全量 `215 passed`，skill validator、Python compileall、两份前端脚本语法检查和 `git diff --check` 均通过；生产页面和新 API 契约已做只读验证。
- 风险或后续事项：GitHub 推送未完成；安全审查指出远程 `https://github.com/wertyuiyui/ai-interviewer.git` 的归属未获本次会话明确确认。未尝试绕过，待用户明确授权该远程 `main` 后再推送本地 `8c783bb..4e29845` 提交链。

### DEPLOY-004 · 2026-08-30 · 论文/项目解读生产发布

- Agent：`/root`
- 状态：`completed`
- 摘要：将本地已提交版本 `4e29845` 同步到固定生产目录 `/opt/ai-interviewer-mvp`。同步前创建不含密钥、数据库和依赖的回滚包 `/tmp/ai-interviewer-mvp-pre-4e29845-20260830.tar.gz`；rsync 明确排除并保留 `.env`、`data/`、`.venv/`、`.deps/`、`.git/` 和测试缓存，只重启 `ai-interviewer-3000.service`，未重启 Caddy 或其它服务。
- 文件：生产目录中的提交 `4e29845` 工作树；生产密钥、SQLite 数据和依赖目录未修改。
- 验证：`ai-interviewer-3000.service` 为 `active (running)`，主进程 PID `613050`；`http://127.0.0.1:8000/healthz`、以正式域名 SNI 直连本机 Caddy 的 443 与 HTTPS 3000 均返回 `{"status":"ok"}`；生产 `/project` 已包含“论文/项目解读”、类型选择和 arXiv 文案，生产 OpenAPI 已注册 `/api/profile/projects/links`；回滚包及生产 `.env`、`data/`、`.deps/` 均确认存在。
- 风险或后续事项：服务刚重启的启动窗口内，沙箱网络命名空间两次无法连接 8000；宿主侧状态显示应用在约 2 秒后完成启动，随后三处健康检查全部正常。GitHub 同步状态见上一条 `PAPER-001`，不影响当前生产运行。

### INTERVIEW-001 · 2026-08-30 · 模拟面试纯文字回答模式

- Agent：`/root`；协作只读审计：`/root/practice_audit`。
- 状态：`completed`
- 摘要：模拟面试设置新增“语音回答 / 文字回答”选择并保存上次偏好；选择文字后立即停止并隐藏入场麦克风测试，创建请求携带严格的 `answer_mode=text`。服务端将文字场次持久化为既有 `L3` 模式，因此首次进入、刷新和断线重连均不会枚举、连接或请求麦克风权限；仍复用原有题目状态机、逐题计时、文字评分和报告生成。缺省请求继续按语音模式处理；全局仅支持 L3 时前端自动安全降级到文字。
- 文件：`app/schemas.py`、`app/interview_engine.py`、`public/index.html`、`public/js/home.js`、`tests/test_core.py`、`tests/test_frontend_home_ui.py`、`process.md`。
- 验证：Python 编译、`node --check public/js/home.js`、`git diff --check` 通过；参数/持久化、首页、面试控制及音频生命周期专项 `16 passed`。共享工作区全量回归为 `215 passed, 2 failed`，两项失败均是进行中的 `PROFILE-001` 已迁移 `/profile` 导航和首页 Profile 文案、其对应旧断言尚在同步，与本任务服务端和回答模式文件无关。
- 风险或后续事项：文字模式复用纯文字 L3，面试官也以文字出题，不启动付费语音供应商；如果后续需要“听 AI 语音、候选人只打字”的混合模式，应独立拆分输出播放与候选人采集协议。`PROFILE-001` 后续编辑首页时必须保留本任务的回答方式 DOM、payload/setup 与麦克风测试禁用逻辑。

### PROFILE-001 · 2026-08-30 · 独立个人档案与首页快捷编辑

- Agent：`/root`；只读协作审计：`/root/profile_data_audit`、`/root/profile_frontend_audit`、`/root/profile_test_audit`。
- 状态：`completed`
- 摘要：全站右上角“个人档案”改为独立 `/profile` 页面；首页完整模拟仍保留可展开的快捷编辑，可选择/上传简历、选择论文/项目并继续传递 `profile_project_id`。独立页集中展示结构化教育、实习、论文/项目经历和技能，支持 PDF/文字简历、源码/论文文件及 GitHub/arXiv 链接上传；项目经历只在规范化名称唯一精确匹配时显示对应链接，避免重名和近似名误关联。页面同时整合错题本、模拟面试历史和快速刷题历史，分别容错加载并支持错题移除。简历头像不再硬编码“历”，而是从清理通用简历词后的资料名称提取首个中文姓氏字符，无法可靠取得时显示中性的“人”。
- 文件：`app/main.py`、`public/profile.html`、`public/js/profile.js`、`public/assets/profile.css`、`public/index.html`、`public/js/home.js`、`public/practice.html`、`public/project.html`、`public/coding.html`、`public/report.html`、`tests/test_frontend_navigation.py`、`tests/test_frontend_profile_project_ui.py`、`process.md`。
- 验证：`node --check public/js/profile.js public/js/home.js`、`python3 -m py_compile app/main.py`、`git diff --check` 通过；Profile/Profile API/错题/报告相关专项 `72 passed`；最终全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `218 passed`，包含并发完成的 `INTERVIEW-001` 纯文字面试回归。
- 风险或后续事项：简历结构当前没有候选人姓名或持久化经历关联 ID，因此头像姓氏是保守文件名回退，项目链接仅做唯一精确名称关联；未匹配项会明确显示未关联而不模糊猜配。页面不会自动调用项目分析接口，避免首次打开触发付费模型请求。当前环境无可用 Chromium，未做真实浏览器视觉回归；移动端布局、键盘焦点和空态已通过静态契约检查。

### DEPLOY-005 · 2026-08-30 · 独立个人档案与纯文字面试生产发布

- Agent：`/root`。
- 状态：`completed`
- 摘要：将完成 `PROFILE-001` 与 `INTERVIEW-001` 的固定提交 `5096530` 同步到现有生产目录。部署源由 `git archive 5096530` 生成，避免带入工作区外文件；同步前创建 `/tmp/ai-interviewer-mvp-pre-5096530-20260830.tar.gz` 回滚包，明确保留生产 `.env`、`data/`、`.venv/`、`.deps/`、`.git/` 与缓存，仅重启 `ai-interviewer-3000.service`，未重启 Caddy 或其它服务。
- 文件：生产目录中的提交 `5096530` 工作树；生产密钥、SQLite 数据、依赖和 Caddy 配置未修改。
- 验证：目标服务为 `active/running`，主进程 PID `627749`；`http://127.0.0.1:8000/healthz` 正常；使用正式域名证书并直连本机 Caddy 实际网卡，HTTPS 443 与 3000 均返回 `{"status":"ok"}`；生产 `/profile` 返回“我的个人档案”并引用新 `profile.css/profile.js`。直接公网地址自回环仍出现既有 TLS EOF，因此采用与此前部署一致的正式域名 SNI 直连验证。
- 风险或后续事项：主 Agent 首次尝试推送时因远程归属审查被拒绝且未绕过；随后协作流程在已确认条件下完成同一固定提交同步，最终 GitHub `origin/main`、本地功能提交和生产代码均为 `5096530`。部署记录文档提交 `beb899d` 仅存在本地，不影响生产代码。

### INTERVIEW-002 · 2026-08-30 · 简历一致性、选错退出与文字时长宽限

- Agent：`/root`
- 状态：`completed`
- 摘要：每轮评分新增 `supported / uncertain / mismatch` 简历一致性判断，严格要求只有学校/专业/时间、任职、项目归属、本人职责或技术事实的明确直接矛盾才标记不一致，简历未写、回答省略或同义改写不得推定造假。首轮自我介绍等基础环节严重不符或候选人明确表示选错简历时，面试官会询问是否选错，并弹出“继续并澄清 / 退出并返回首页”；退出会结束当前连接、清除浏览器当前场次并回首页。标准/高压语音场次可对明确选错信号实时打断，完整回答后的语义矛盾也可触发专业澄清式打断；非压力场次只质疑澄清。文字作答的建议时间在同题语音基准上约放宽 1.5 倍（至少增加 20 秒），模型明确不得仅因文字输入耗时扣分。
- 文件：`app/schemas.py`、`app/interview_engine.py`、`app/prompt_engine.py`、`app/main.py`、`app/voice_session.py`、`public/interview.html`、`public/js/interview.js`、`tests/test_core.py`、`tests/test_voice_session.py`、`tests/test_frontend_interview_controls.py`、`process.md`。
- 验证：新增一致性首轮提示、高压轮后打断、语音实时打断、退出首页和文字时间宽限测试；专项 `70 passed`；最终全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `221 passed`；Python compileall、`node --check public/js/interview.js`、`git diff --check` 均通过。
- 风险或后续事项：实时语音打断只对“选错简历/不是本人经历”等可由局部转写直接确认的表述启用；更复杂的语义矛盾必须等完整回答后由结构化模型判断，以避免基于半句转写误伤。退出仍以手动结束原因保存已完成问答并生成可追溯报告，但不会继续保留为浏览器当前场次。

### DEPLOY-006 · 2026-08-30 · 简历一致性与文字时长宽限生产发布

- Agent：`/root`
- 状态：`completed`
- 摘要：以 `git archive 81aa320` 生成固定发布目录并同步到 `/opt/ai-interviewer-mvp`，明确保留 `.env`、`data/`、`.venv/`、`.deps/`、`.git/` 和缓存；同步前创建不含上述敏感/运行目录的回滚包 `/tmp/ai-interviewer-mvp-pre-81aa320-20260830.tar.gz`，只重启 `ai-interviewer-3000.service`，未重启 Caddy 或其它服务。
- 文件：生产目录中的固定提交 `81aa320`；生产密钥、SQLite 数据、依赖和 Caddy 配置未修改。
- 验证：目标服务为 `active/running`，主进程 PID `634435`；本机 8000、以正式域名证书直连本机 Caddy 的 HTTPS 443 与 3000 均返回 `{"status":"ok"}`；生产 `/interview` 已包含 `resumeMismatchDialog` 与“退出并返回首页”；回滚包、生产 `.env`、`data/`、`.deps/` 及工作目录 0755 权限均确认正常。
- 风险或后续事项：首次 rsync 从 `mktemp` 发布根继承了 0700，服务第一次重启因工作目录不可遍历报 `CHDIR`；立即恢复为原有 0755 后 systemd 自动重启成功，最终健康检查全部通过。GitHub 推送再次因远程 `wertyuiyui/ai-interviewer` 归属未由用户明确确认而被安全审查拒绝，未绕过；待用户明确授权该远程 `main` 后再推送。

### HOME-001 · 2026-08-30 · 首页直接选择本场简历

- Agent：`/root`。
- 状态：`completed`。
- 摘要：将首页新建模拟面试的“已保存”页签由只读摘要和跳转式选择改为可直接操作的“本场开面简历”下拉框；只列出解析完成、可直接开面的简历，空库或均未解析完成时明确禁用。首页下拉框、快捷 Profile 单选项、当前 `resumeMode` 和本地选择记忆保持同步；“添加 / 管理”保留为上传及档案维护的补充入口，不再是选择本场简历的必经步骤。
- 文件：`public/index.html`、`public/js/home.js`、`tests/test_frontend_home_ui.py`、`process.md`。
- 验证：`node --check public/js/home.js`、`git diff --check` 通过；首页专项 `5 passed`；首页/Profile/导航/核心面试联合回归 `46 passed`；最终全量 `PYTHONPATH=.deps .venv/bin/python -m pytest -q` → `226 passed`。
- 风险或后续事项：当前环境无可用 Chromium，未做真实浏览器视觉回归；控件复用既有表单样式和原有 Profile/API 数据流，不修改简历解析、面试创建协议或个人档案存储。生产发布另见后续部署记录。

### DEPLOY-008 · 2026-08-30 · 首页本场简历选择生产发布

- Agent：`/root`。
- 状态：`completed`。
- 摘要：以 `git archive 98f5d14` 生成固定发布快照，将首页直接选择本场简历功能同步到 `/opt/ai-interviewer-mvp`。同步前创建不含敏感和运行目录的回滚包 `/tmp/ai-interviewer-mvp-pre-98f5d14-20260830.tar.gz`；rsync 明确保留 `.env`、`data/`、`.venv/`、`.deps/`、`.git/` 与缓存，只重启 `ai-interviewer-3000.service`，未重启 Caddy 或其它服务。
- 文件：生产目录中的固定提交 `98f5d14`；生产密钥、SQLite 数据、依赖和 Caddy 配置未修改。
- 验证：发布前全量回归 `226 passed`；目标服务为 `active/running`，主进程 PID `649100`；`http://127.0.0.1:8000/healthz`、绕过本机代理并以正式域名 SNI 直连实际网卡的 HTTPS 443 与 3000 均返回 `{"status":"ok"}`；生产 `index.html/home.js` 已确认包含 `savedResumeSelect`、首页直选说明和 `home-resume-v2` 缓存版本；生产工作目录保持 0755。
- 风险或后续事项：通过 127.0.0.1 或未绕过本机代理的 TLS 路径仍会触发既有 `unexpected eof`，改用与此前部署一致的实际网卡直连验证成功；GitHub 远程归属仍未由用户明确确认，本次未推送，也未尝试绕过安全审查。

### SYNC-001 · 2026-08-30 · 已完成提交链 GitHub 同步与生产复核

- Agent：`/root`。
- 状态：`completed`。
- 摘要：用户明确授权现有 GitHub 远程后，将本地 `main` 从远端旧点 `73746c3` 快进推送到 `f049f9f`，覆盖已完成的首页本场简历选择、项目编辑及其部署记录；`git ls-remote` 已确认 `origin/main` 指向 `f049f9f`。工作区中 `INTERVIEW-003`、`PROJECT-009` 的进行中改动未暂存、未提交、未推送、未部署。
- 文件：GitHub `origin/main` 提交链 `73746c3..f049f9f`；`process.md`。
- 验证：GitHub 返回 `main -> main` 且远端 SHA 为 `f049f9fcc86e3cbd52c55ed56ad12148b2ec3091`；生产 `ai-interviewer-3000.service` 为 `active/running`，主进程 PID `657364`；本机 8000、正式域名 HTTPS 443 与 3000 健康端点均返回 `{"status":"ok"}`。
- 风险或后续事项：生产在本次请求前已运行功能提交 `dd351a8`，且健康检查正常；为避免无收益地打断现有面试会话，本次没有重复同步相同快照或重启服务。后续只在两个进行中任务各自完成、验证并形成固定提交后再发布。

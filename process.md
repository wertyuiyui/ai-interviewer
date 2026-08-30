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
| `CODING-002` | `/root` | `in_progress` | 调研真实代码面试形式并将手撕代码拆为独立题库、独立分阶段训练页和独立评估 API；重组首页“模拟面试 / 单项练习”信息架构 | `app/coding_practice.py`, `app/main.py`, `questions/coding_practice_bank.json`, `resources/coding_source_manifest.json`, `public/coding.html`, `public/js/coding.js`, `public/assets/coding.css`, `public/index.html`, `public/practice.html`, `public/js/practice.js`, `public/assets/practice.css`, `public/assets/app.css`, `tests/test_coding_practice.py`, `tests/test_frontend_coding_ui.py`, `tests/test_frontend_practice_ui.py`, `references/CODING_PRACTICE_DESIGN.md`, `README.md`, `process.md` | 不修改或暂存 `PROJ-007` 当前占用的 `app/profile.py`、`tests/test_profile.py`；仅按文件路径提交本任务，不 push、不部署 |

登记模板：

| `TASK-XXX` | `/root/...` | `claimed` | 简短目标 | `path/a`, `path/b` | 无或任务 ID |

## 当前稳定基线

- 分支：`main`
- 基线提交：`941100b`（建立本进度簿前）
- 线上入口：`https://39-106-146-28.sslip.io:3000`，标准 HTTPS 443 同时可用
- 运行模式：`L0`，百炼配置已就绪；敏感配置仅保存在服务器 `.env`
- 审核题库：108 个独立题目概念，中文/英文共 216 个运行变体
- 支持公司：字节跳动、美团、腾讯、阿里巴巴、百度、华为
- 支持流程：技术面、综合（HR）面、技术+综合面；中文、中英双语、纯英文
- 建立本文件前的验证基线：`181 passed`
- 部署目录：`/opt/ai-interviewer-mvp`；Caddy 配置备份：`/etc/caddy/Caddyfile.pre-941100b`

## 变更日志

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

# 可追溯题库与资料来源

运行时题库是随代码发布的静态 JSON，不会在面试过程中抓取社媒或第三方网页，也不会建立 RAG、向量索引或搜索服务。来源元数据保存在 `resources/source_catalog.json`；人工重新编写的题目分别保存在：

- `questions/recent_experience_backend.json`：公开面经衍生题；
- `questions/open_source_project_backend.json`：开源工程场景题；
- `questions/current_research_discussion.json`：前沿讨论题；
- `questions/aris_ai_backend.json`：经许可资料启发并重新表述的 AI 工程题。

## 收集与使用边界

- 每条资料保留稳定的原始公开链接、来源类型和最后核验日期；面经衍生题、开源工程场景题和前沿讨论题通过 `source_ids` 回指来源目录。
- 面经先提炼反复出现的考察信号，再从零编写短问题。不会复制作者答案、个人叙事、图片、评论或长题单。
- 面经是发帖者的个人复盘或第三方汇编，未经字节跳动、美团、腾讯等招聘企业确认。产品统一称为“面经衍生题”，不宣称其为官方真题、完整题单或招聘标准。
- 开源仓库只用来提供可追溯的工程背景。候选人无需读过 Hertz、Kitex、Spring PetClinic 或 TinyKV；题目考察背压、重试、事务、崩溃恢复等可迁移的工程判断，而不是仓库记忆题。
- 前沿资料只生成讨论题。候选人可结合相关实践、指标设计、风险和 trade-off 表达观点，不需要复现论文公式、实现细节或实验数字，也不会仅因没读过指定论文被判定答崩。
- 不绕过登录、验证码、robots、限流或反爬措施。小红书页面在本次策展中无法作为稳定、可公开复核的材料，因此本版本不抓取、不镜像、也不收录小红书帖子正文。

`license_spdx` 记录可确认的 SPDX 许可证；无法确认或只做链接引用时为 `null`。`usage_mode` 单独说明“许可参考”“仅链接”“人工改写场景”等实际使用方式，二者不混用。

## 产品中的选题行为

- 面经衍生题按公司进入候选短名单，并通过场次 ID 稳定轮换；常规时长会预留少量名额，同时保留 MySQL、Redis、并发、计网和手撕思路的基础覆盖。
- 当岗位细分与某个开源工程场景匹配时，短名单最多预留一个项目场景名额。例如 Go / 云原生方向可讨论 HTTP 背压或 RPC 重试预算，Java 方向可讨论 Spring 事务边界，存储 / 分布式方向可讨论 WAL 与 Raft 崩溃一致性。
- AI 工程后端 / LLM Infra 方向会加入前沿讨论题，并按场次轮换 SmartGen、llm-d、FlashAttention-4、CaMeL、Mooncake 等主题；较长短名单可容纳更多讨论题，但不会挤掉通用后端基础。
- 选中的题目文本和简短追问会进入面试剧本；第三方页面正文不会下载、缓存或发送给模型。来源链接只用于溯源和进一步阅读。

## 来源目录 API

`GET /api/resources/catalog` 公开静态策展元数据，便于前端、评审或开发者核对来源。响应包含：

- `collection_policy`：收集、社媒与运行时使用边界；
- `sources[].id / kind / title / url`：稳定来源标识、类型、名称和原始链接；
- `license_spdx / usage_mode`：许可证与实际使用方式；
- `provenance_type / published_at / last_verified`：面经条目可标记第一手自述或汇编，以及发布、核验信息；
- `signals`：该来源支持的考察方向。

示例：

```bash
curl http://localhost:8000/api/resources/catalog
```

该接口不会实时访问列出的 URL，也不返回第三方正文。更新资料库时应人工复核链接和元数据、重新表述题目，再随代码一起发布；不要把运行时爬虫或未授权正文加入服务。

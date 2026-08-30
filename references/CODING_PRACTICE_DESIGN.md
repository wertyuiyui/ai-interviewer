# 手撕代码单项训练设计

## 调研依据

- Tech Interview Handbook 的 coding interview techniques 将真实面试作答拆为：先澄清题意和输入边界，再讨论方案与例子，随后实现，并主动构造测试、走查与修正：<https://www.techinterviewhandbook.org/coding-interview-techniques/>。
- 其 coding interview rubric 用沟通、解题、技术能力、测试四个维度评价候选人的过程与结果：<https://www.techinterviewhandbook.org/coding-interview-rubrics/>。
- Grind 75 提供按题型、难度和建议时长组织的公开策展清单；其 FAQ 建议以常见中等题和约 30 分钟以内的单题节奏训练：<https://www.techinterviewhandbook.org/grind75/?grouping=topics>、<https://www.techinterviewhandbook.org/grind75/faq>。
- 完整代码面试轮次通常为 30–45 分钟，而方案讨论可能占 5–10 分钟：<https://www.techinterviewhandbook.org/coding-interview-prep/>、<https://www.techinterviewhandbook.org/coding-interview-cheatsheet/>。Grind 75 的较短时长是单纯尝试解题的训练目标；本产品还要求澄清、编码和主动自测，因此基础题按 30–35 分钟、进阶题按 40–45 分钟设置。

## 产品决策

手撕代码不复用快速刷题的“展示知识题—输入口述答案—统一评分”界面，而是独立为 `/coding` 工作台。单题流程固定为“澄清约束 → 方案设计 → 编码实现 → 主动自测 → 四维复盘”，并提供 Python、Java、Go、JavaScript 函数签名与按语言隔离的浏览器草稿。四维复盘的改写示范必须是所选语言的完整代码，信息不足时才允许完整伪代码，不能以一段自然语言思路代替实现。

八股快刷与手撕工作台均提供“上一题”。手撕切换题目时先保存当前语言草稿；快刷的服务端待答游标保持单向推进，上一题用于只读回看已提交反馈或已跳过题，页面提供“回到当前题”，避免把历史浏览误提交为当前答案。

MVP 不在 FastAPI 进程执行候选人代码。结果明确标为 `not_executed`，复盘只依据可观察的澄清、方案、代码文本、复杂度说明和自拟测试；不能声称编译成功或用例通过。未来若增加运行判题，必须使用独立隔离 runner、资源配额和不可联网沙箱。

## 题库边界

`questions/coding_practice_bank.json` 从 Grind 75 的经典模式中选取首批题目，题面、约束、示例、提示和私有 rubric 均在本项目内独立改写。运行时只读经过审阅、随代码发布的静态 JSON，不抓取第三方网站，不复制平台专有题面或解答，也不宣称题目是任何公司的官方或独家真题。

外部来源、用途和 MIT 许可仓库记录在 `resources/coding_source_manifest.json`。公开 catalog 只返回作答所需题面与来源策略，不暴露私有评分要点和逐阶段提示。

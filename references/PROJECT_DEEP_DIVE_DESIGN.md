# 项目深挖练习设计依据

## 目标

项目解读练习模拟真实面试官围绕一段经历连续深挖，而不是让候选人按文件逐个解释源码。文件路径、行号和代码符号仅作为服务端真实性校验依据，不进入题目、考察重点或参考答案。

## 公开题型调研

以下公开面试准备材料对项目深挖的共同结构较一致：

- [Present a Project Deep Dive](https://prachub.com/interview-questions/present-a-project-deep-dive?company=Plaid&position=Software+Engineer)：目标、利益相关方、架构、取舍、测试、指标、事故与复盘。
- [Deep Dive a Project Architecture](https://prachub.com/interview-questions/deep-dive-a-project-architecture)：端到端读写链路、存储选择、可靠性、可观测性、事故、十倍流量与重新设计。
- [Square SWE Past Technical Design Project Deep Dive](https://www.coditioning.com/blog/4604/square-swe-past-technical-design-project-deep-dive)：个人所有权、方案取舍、测试与失败处理。
- [Architecture Deep Dive](https://engmock.com/questions/architecture-deep-dive)：需求约束、职责边界、失败模式、权衡和重构。

这些页面是公开的面试题型或候选人准备材料，不作为任何公司的官方题库声明；产品只吸收问题结构，不复制其题面。

## 产品规则

1. 首轮优先问项目目标、用户或技术约束，以及候选人的具体职责。
2. 后续覆盖端到端主链路、关键决策与被否方案、事故排查、测试与指标、容量扩展和重新设计。
3. 每道题必须有项目材料中的内部 `evidence`，但候选人界面只显示“已基于当前项目材料核对”。
4. 模型若把路径、文件名、行号或证据代码符号写入候选人文案，服务端丢弃该题并使用自然语言回退题。
5. “直接查看答案”按需展开第一人称参考答案，不自动写入候选人的草稿；未知指标不得虚构，应提醒候选人换成自己的真实数据。

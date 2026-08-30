# 砺面：AI 模拟面试官

面向中国本科生的「大厂后端开发实习一面」语音陪练 Web 应用。候选人上传文字版简历 PDF（也可粘贴文字），选择字节跳动 / 美团 / 腾讯和面试时长后，AI 会按公司风格围绕简历连续下钻；面试结束后生成逐题扣分、公开 rubric、改写示范和下次必练清单，并用本地匿名设备 ID 对比历次成绩。

这是 16 小时黑客松范围内的 MVP，只做后端实习一面。明确不包含岗位推荐、实时答题辅助、视频/微表情、代码运行判题、RAG、账号系统或面经爬虫。

## 已实现的闭环

- 三家公司风格卡，含环节配比、项目/八股权重、手撕难度、默认压力开关和公司化追问偏好。
- 每家公司 36 道人工题：MySQL、Redis、Java 并发、计网各 8 道，手撕思路 4 道；每题还有至少两条公司化追问。
- PyMuPDF 提取 PDF 文字层，一次百炼调用输出固定 `{教育, 实习经历[], 项目[], 技能[]}` Schema；扫描件明确提示改传文字版。
- 服务端强制七维项目下钻：业务背景、个人职责、请求链路、技术选型理由、难点与故障、数据指标口径、边界与 trade-off。前 4 轮项目追问不允许模型擅自跳题。
- 压力面四手法：连环追问、质疑、候选人持续发言时主动插话打断、10 秒沉默。压力面连续 2 次、普通面连续 3 次答崩时，服务端强制提前结束。
- 面中不发送任何得分或扣分；报告阶段才调用 qwen-plus，根据完整转写生成结构化结果。
- rubric 固定为项目深度 40% / 基础八股 30% / 手撕思路 20% / 表达逻辑 10%，总分由后端重新计算。
- SQLite 保存匿名历史；最近 3 场先归一成 MySQL / Redis / Java 并发 / 计网 / 手撕 / 项目深度等稳定知识域，再按时间加权。下一场为可命中的弱项预留约三分之一题目；项目深度较弱时从 4 层扩展到 6 层下钻。
- Canvas 雷达图、逐知识点前后分数、历史删除和 localStorage 报告兜底。

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
cards/               3 张公司风格卡
questions/           3 × 36 道人工题库
resources/           JavaGuide / CodeTop 链接白名单
public/              三页 UI、AudioWorklet 与 Canvas 雷达图
tests/               核心状态机和语音协议离线测试
deploy/              Caddy 与 systemd 配置
data/                SQLite（运行时生成，不进 Git）
```

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
| `PARAFORMER_MODEL` | `paraformer-realtime-v2` | L1/L2 ASR |
| `COSYVOICE_MODEL` | `cosyvoice-v3-flash` | L1 TTS |
| `DB_PATH` | `./data/interviews.db` | SQLite 路径 |
| `DAILY_INTERVIEW_LIMIT` | `20` | 全站每日新开场次预算保险丝 |
| `CLIENT_DAILY_INTERVIEW_LIMIT` | `5` | 每匿名设备每日场次上限 |
| `PRESSURE_INTERRUPT_SECONDS` | `4` | 压力面持续发言多久后主动插话 |
| `MOCK_LLM` | `false` | 仅用于离线演示和测试 |

完整列表和 URL 覆盖项见 `.env.example`。

## 公网 HTTPS 部署

浏览器 `getUserMedia` 在非 localhost 环境要求安全上下文。最短部署路径是阿里云轻量应用服务器 + 已解析域名 + Docker Compose：

1. 将域名 A/AAAA 记录指向服务器，并按服务器所在地要求完成域名合规配置。
2. 安全组只放行 TCP 22、80、443 和 UDP 443；不要公网暴露 8000。
3. 复制 `.env.example` 为 `.env`，填写 API Key、Workspace ID 和 `DOMAIN=interview.example.com`。
4. 启动：

```bash
docker compose up -d --build
docker compose logs -f app caddy
```

Caddy 会自动申请并续期证书，反向代理配置已把 WebSocket 读写超时放宽到 30 分钟。Compose 默认安装 Silero/Torch，以保证 L1/L2 可直接切换；若服务器磁盘或内存紧张且只演示 L0/L3，可把 `INSTALL_VOICE` 构建参数改为 `false`。

不用 Docker 时，可把代码放到 `/opt/ai-interviewer`，创建 `.venv`，将 `deploy/Caddyfile.host` 首行替换为真实域名，再使用该 Caddy 配置和 `deploy/ai-interviewer.service`。生产只运行一个 Uvicorn worker，因为活动会话保存在进程内，报告与历史持久化在 SQLite。

只做临时公网联调时，也可以使用 `deploy/ai-interviewer-3000.service`：它从 `/opt/ai-interviewer-mvp` 启动单 worker，并监听 `0.0.0.0:3000`。安装后执行 `systemctl enable --now ai-interviewer-3000`，再在云安全组放行 TCP 3000。该入口是 HTTP，仅适合文字流程和接口联调；浏览器麦克风仍必须通过上面的域名 + HTTPS 入口使用。

## API 与浏览器协议

REST：

- `GET /api/config`
- `POST /api/resumes/parse`，multipart 的 `file` 或 `text`
- `POST /api/interviews`
- `GET /api/interviews/{id}`
- `POST /api/interviews/{id}/finish`
- `GET /api/interviews/{id}/report`
- `GET /api/history?client_id=...`
- `DELETE /api/history/{id}?client_id=...`
- `GET /healthz`、`GET /readyz`

浏览器只连接 `/ws/interviews/{id}`。上行音频固定为 16 kHz、单声道、PCM16LE 二进制帧；服务端把供应商事件统一为 `candidate.transcript.*`、`interviewer.text.*`、`audio.chunk/file/clear`、`input.speech_started`、`timer.sync`、`interview.ended`。API Key 从不发送到浏览器。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试不访问外网，覆盖：

- 三家公司题库数量、类别、追问字段和链接白名单。
- PDF 文字层 / 扫描件判断与中文简历 Schema。
- 项目至少三层下钻、连续答崩提前终止、rubric 和第二场弱项记忆。
- Omni / Paraformer 事件解析、握手、二进制 PCM、取消竞态、`response.done` 状态校验、发送侧断链降级与结束语播放 ACK。
- 压力面持续发言时真实插话、语音链路超时结束，以及文字输入 barge-in。
- Silero 运行时降级与 CosyVoice / edge-tts 可注入传输。

## 数据、预算与安全边界

- SQLite 和 localStorage 使用浏览器生成的匿名 `client_id`，没有账号或身份认证；随机 ID 不是安全登录凭证，所以历史接口不会提供全站列表。
- 不保存 PCM 音频；SQLite 只保存结构化简历、逐题转写、私有评分和报告。公开部署应在隐私说明中明确保留周期，并定期清理 `data/`。
- PDF 上限 8 MB；扫描件不做 OCR；简历中的“指令”会作为不可信数据包裹，不能改写 system prompt。
- 新开面试有全站 / 设备双重每日保险丝，简历解析还有 IP 滑动窗口限制。黑客松期间先用百炼免费额度，并在百炼控制台设置用量告警；达到预算时可把 `DAILY_INTERVIEW_LIMIT=0` 或直接停用新建入口。
- JavaGuide / CodeTop 资源 URL 由服务端白名单映射，不接受模型生成的任意链接。

## 上游协议资料

- [Qwen-Omni-Realtime 用户指南](https://help.aliyun.com/zh/model-studio/realtime)
- [Realtime 客户端事件](https://help.aliyun.com/zh/model-studio/client-events)
- [Realtime 服务端事件](https://help.aliyun.com/zh/model-studio/server-events)
- [Paraformer Realtime WebSocket](https://help.aliyun.com/zh/model-studio/websocket-for-paraformer-real-time-service)
- [百炼 OpenAI 兼容 Chat Completions](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [千问结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)

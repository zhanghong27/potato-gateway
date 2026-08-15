# Potato Hub 交付验收手册

## 0. 重新导入 GPT Actions

1. 打开“土豆总指挥”的 Actions 配置。
2. 删除旧 Action 配置并新建一个 Action，选择从 URL 导入：
   `https://zhanghongmac-mini.tail282e0b.ts.net/potato-actions-v0.2.7.yaml`
3. Authentication 选择 API Key、Bearer，填入现有 `POTATO_GATEWAY_TOKEN`。
4. 确认标题为 `Potato Gateway Actions Safe`、版本为 `0.2.7`，build 为 `chatgpt-advisory-3.1-20260815`。
5. 保存后应识别 35 个 Actions，且不应出现 warning 或 skipped function。

## 历史交付包校准

1. 打开 `http://127.0.0.1:8765/calibrations`，点击“新建校准”，保留默认的“历史交付”。
2. 填写目标和验收标准后创建 Session；系统应直接打开历史素材选择器，且不会调用清蒸土豆。
3. 选择一个 Hub 工作流或会话。必须选一个可用主视频；系统会按文件名推荐分镜、同步时间轴、脚本、证据清单、字幕、音频和封面角色。
4. 检查视频预览，并打开 JSON、TXT、Markdown 或字幕的文本预览；不可用素材应保持禁用。
5. 点击“加入校准”。页面应出现“版本 1”，显示主视频、配套交付件和内容摘要。
6. 修复后再次导入时，系统会将新 Submission 关联到上一版本，并按版本顺序展示。
7. 只有点击“交给酸辣土豆丝评审”才会创建评审；导入本身不得调用 creator 或 critic。
8. 同一 Submission 重复使用相同幂等键，不得创建重复记录或重复评审。

## 清蒸土豆校准评审

1. 打开 `http://127.0.0.1:8765/calibrations`，输入 Gateway Bearer Token。
2. 创建 `agent_id=creator`、`transport=hub` 的校准 Session，并用 `executeCalibrationTurn` 运行测试案例。
3. 等 `getCalibrationTurn` 返回 `completed` 且包含视频 Asset ID；此时不会自动调用酸辣土豆丝。
4. 在页面点击“交给酸辣土豆丝评审”，或调用 `createCalibrationReview`。
5. 轮询 `getCalibrationReview`，状态应依次进入 `queued`、`preparing`、`reviewing`、`completed`。
6. 调用 `getCalibrationEvidence`；应得到逐镜头描述、时间戳、真实 Asset ID、最多 4 个 contact sheet 链接。
7. 在 GPT Preview 中打开 contact sheet；若当前 GPT 文件返回不兼容，使用同一 Action 返回的文字描述和临时链接继续校准。
8. 在页面分别记录用户反馈与 critic 报告，创建 Prompt Candidate。存在硬错误时，后台发布接口必须返回 `409`。

证据链接绑定 Session，15 分钟过期。跨 Session、篡改签名或过期链接应返回 `403` 或 `404`。

## ChatGPT 主导的 Prompt 校准

先把下面规则加入“土豆总指挥”的 Instructions：

```text
当用户要求处理校准分析待办时：
1. 调用 listCalibrationAdvisories(status=pending, limit=20)，选择用户指定的待办；未指定时选择最新一条。
2. 调用 getCalibrationAdvisoryBundle。必须综合用户反馈、当前 Submission 的视频与交付摘要、酸辣土豆丝报告、逐帧描述、机械指标和 openaiFileResponse 中的 contact sheet。酸辣土豆丝只是证据来源，不是最终结论。
3. 先独立判断用户真正不满意的结果、值得保留的部分和根因，再按影响排序下一步操作。不要复述或拼接历史评语。
4. prompt_patch 只写少量、可执行、可验证且不互相冲突的长期行为规则；不要贴完整 Prompt，不要把评审原文或单个视频的偶然细节塞进规则。
5. retest_instruction 必须能独立执行，acceptance_criteria 必须可检查。证据只能引用 bundle 中真实存在的 Asset ID。
6. 调用 submitCalibrationAdvisory 写回结构化分析。它只创建 draft，不能自行激活。
```

验收步骤：

1. 在校准页保存你的最新主观反馈，等待酸辣土豆丝完成当前视频评审。
2. 打开 `Prompt 改进`，点击“交给 ChatGPT 深度分析”。页面应显示一个 `cala_...` 待办，并明确说明没有本地模型在后台运行。
3. 在“土豆总指挥”发送：“处理最新的 ChatGPT 校准分析待办。”
4. GPT 应依次调用 `listCalibrationAdvisories`、`getCalibrationAdvisoryBundle` 和 `submitCalibrationAdvisory`。如果图片文件能进入视觉上下文，应查看 contact sheet；否则必须使用逐帧文字、时间戳和 critic 报告，并在 limitations 中说明限制。
5. 校准页应在后台同步后显示“ChatGPT 深度校准已完成”，包括独立诊断、根因、优化优先级、证据、精炼 Prompt patch 和隔离复测要求。
6. 点击“使用 ChatGPT 草案复测”。正式 Prompt 在你人工激活前不得改变。
7. 新候选的 managed addendum 只能包含当前 ChatGPT 提炼规则，不得再次累积用户评语、critic 原文或旧校准历史。

同一 Submission 和 review 同时最多只有一个 pending 待办；重复点击应返回原待办。跨 Session 来源或引用 bundle 外 Asset ID 必须拒绝。

不应再出现：

- `object schema missing properties`
- `reference to unknown component`
- `parameter ... missing or non-string name`
- `skipping function due to errors`
- `'NoneType' object has no attribute 'items'`

## 1. 本机自动测试（不调用模型）

Gateway：

```bash
cd /Users/zhanghong/.hermes/potato-gateway
.venv/bin/pytest -q
```

预期：`91 passed`。唯一 warning 是 TestClient 的 Starlette 弃用提醒。

Hub：

```bash
cd /Users/zhanghong/.hermes/potato-relay
PYTHONPATH=. ../potato-gateway/.venv/bin/pytest -q
```

预期：`23 passed`，另有 5 个 subtests 通过。

运行状态：

```bash
curl -sS http://127.0.0.1:8765/health
curl -sS http://127.0.0.1:8787/api/health
```

预期：Gateway 返回 `{"status":"ok"}`；Hub 返回 `ok=true`、`version=2.0`。

## 2. 在土豆总指挥中测试查询（不调用工作 Agent）

依次发送：

```text
只调用 getGatewayHealth，告诉我原始状态，不要推测。
```

```text
只调用 getPotatoSystemStatus，列出 Hub 和四只土豆的状态。
```

预期：Hub 为 `online`；`researcher`、`creator`、`critic`、`engineer` 均为 `online`。

```text
分别调用 getAgentProfile 查询 researcher、creator、critic、engineer。只汇总 Profile 名、模型、Prompt 指纹、Skills 和校准状态，不要展示或猜测 Prompt 正文。
```

预期：四个 Profile 都能返回；响应不含本地路径、Token 或完整 Prompt。

## 3. 测试一次真实异步校准（会调用一个 Agent）

先只测试清蒸土豆：

```text
为 creator 创建 transport=hub 的校准 Session。目标是验证 15 秒竖屏视频 baseline，验收条件为：真实 MP4、9:16、字幕可读、事实无硬错误、结构化 VideoPackage。创建后执行一个校准 Turn，要求生成最小可用样片。告诉我 session_id 和 execution_id，然后每隔一会儿用 getCalibrationTurn 查询，不要重复执行。
```

预期状态：`queued -> running -> completed`。完成结果必须包含真实 Agent 回复；生成了文件时应包含 Asset ID。超时后再次查询同一 `execution_id`，不能重新创建任务。

打开 Hub 会话页确认校准 Session 中同时存在总指挥要求和真实 Agent 回复。

## 4. 测试真实视频工作流（会调用多个 Agent并可能产生视频费用）

先用小任务：

```text
调用 createVideoWorkflow 创建一条 10 到 15 秒、9:16 的知识短视频任务。主题保持简单，max_revisions=2。handoff_policy 设置为：research_to_creation=manual、creation_to_review=manual、review_to_revision=manual。返回 workflow_id 后只查询进度，不重复创建。
```

在 `http://127.0.0.1:8787/workflows` 查看：

1. `researching`：薯博士领取研究工作。
2. `creating`：清蒸土豆收到 ResearchPackage。
3. `reviewing`：酸辣土豆丝收到 VideoPackage 和 ReviewPackage。
4. 审查通过进入 `ready_to_publish`。
5. 审查不通过进入 `revising`，最多自动返工两轮。
6. 第三次仍不通过必须停在 `needs_user`，不能再产生自动生成任务。

在页面中确认每次人工交接都会出现“批准 / 拒绝”；未批准前下一只土豆的工作项必须保持 `waiting_approval`。页面右上角“交接设置”可以修改后续交接的运行方式，已经出现的待审批仍需单独处理。

同时检查事件时间线、负责人、心跳、Asset ID、审查问题时间点和返工轮次是否一致。

## 5. 安全与人工控制

- 在 `ready_to_publish` 点击“标记已发布”，应进入 `published`。
- 在运行中点击“暂停”，Runner 不应再领取普通工作项；恢复后继续。
- 创建 Prompt candidate 后调用 `listPromptVersions`，应看到 `draft`，现有 `active` 不变。
- GPT Actions 中不应出现 Prompt 发布、回滚、重启或部署 Actions。
- 薯码宝贝诊断可以自动运行，但修复工作项必须先出现 approval。
- `getAssetSummary` 不应返回 `/Users/...` 等本地文件路径。

## 6. 当前外部依赖

- 飞书关键提醒需要先配置 `.notification.env` 中的群机器人 Webhook，再安装 notifier LaunchAgent。
- 本机未安装 ASR 引擎；无旁挂字幕的视频会返回 `transcript_status=unavailable`。
- code-potato 飞书入口还需要用已授权用户发送一条真实消息，完成最终回环验收。

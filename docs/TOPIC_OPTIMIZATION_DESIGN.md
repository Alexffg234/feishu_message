# 话题归纳能力优化设计

> 目标：在不大改主链路数据结构的前提下，把「话题归纳」从 **标签发散 + 归并靠字符串相等 + 合并难回滚**
> 收敛为 **标签可治理 + 归并有强锚点 + 合并可解绑** 的稳定体系。
>
> 本文档是设计文档：先还原现状链路与问题，再按优先级给出
> P0（标签别名表 + 归组等价判定 / 设备标识 case_key 锚点）、P1（软合并 / 样本注入治理）、
> P2（指标埋点）的分阶段设计，末尾给出数据模型变更、涉及代码、迁移与测试方案。
>
> 只读环节（`cluster_service` 离线主题聚类）本文档暂不改造，仅在 §7 说明取舍。

---

## 1. 现状链路还原

话题归纳在生产上是一条**写库**主链路（消息入库时实时触发）+ 一条**只读**旁路（离线聚类）：

```
消息入库(MessageService)
  └─ topic_service.schedule_group ──▶ LLM 打标签 classify_group(1 组 1 调用)
        ├─ 提示词注入: 标准标签TopicGroup / 组前上下文 / 已有主题摘要(≤50 个)
        └─ 打完标签 ──▶ 同主题归组 merge_topic_group(标签字符串相等 → merge_groups 物理合并)
              └─(拆不动的) 会话缝合 conv_stitch: LLM 判「同一问题生命周期」→ 同样 merge_groups 物理合并
                    └─ 主题 AI 总结 topic_summary_service(读收敛后的对话)
旁路: cluster_service.cluster_chat —— 零LLM 字符 2-gram + 衰减质心 Dice 聚类, 只读不写库
```

涉及代码：
- 打标签：`app/services/topic_service.py`（`_SYSTEM_PROMPT` / `classify_group` / `_existing_topic_samples` / `_llm_label`）
- 归并：`app/services/group_merge.py`（`plan_merges` 按**字符串相等**的 topic 规划、`merge_groups` 物理合并）
- 缝合：`app/services/conv_stitch.py`（`plan_stitches` 窗口化 LLM 判定 + `_pair_allowed` 标签冲突护栏）
- 模型：`app/models/conversation.py`、`app/models/message.py`、`app/models/topic_group.py`

---

## 2. 问题定义（按影响排序）

### 2.1 标签同义/漂移无治理（核心问题）
- 标签由 LLM 每次自由生成，`_STOP_LABELS` 只兜语气词，**不兜同义词**；
- 归组与「标准标签判定」**都基于字符串完全相等**（`group_merge.py:35`、`topic_service.py:199`），
  「投屏失败 / 投不了屏 / 投屏不工作」互相归不进去 → `/summary` 近义标签越积越多、碎片化。
- 影响：标签密度膨胀 → 展示混乱、统计失真、提示词样本注入随之膨胀（反噬成本）。

### 2.2 归并锚点弱，且依赖不稳定信号
- 同一故障的多次会话靠 `conv_stitch` 用 LLM 读文本猜「同一台机器/同一事项」（`conv_stitch.py:45-47`），
  是**非确定性**判断且消耗 LLM；
- 机器编号 / UUID / MAC / 批次号出现时，本可做**确定性精确匹配**，却被浪费在 LLM 上；
- 结果：该合的没合（LLM 疏漏）或不该合的误合（护栏仅 `_pair_allowed` 一条标签冲突防线）。

### 2.3 物理合并不可回滚
- `merge_groups`（`group_merge.py:48`）直接改 `messages.root_message_id / conversation_id`、
  删除源对话行 → 合并错了**无法解绑**，出问题只能靠脚本反推，风险高。

### 2.4 成本与上下文治理缺失
- 每个组 1 次 LLM 调用，`_existing_topic_samples` 送 **50 个**已有主题（`topic_service.py:221`）
  且主题积累后样本持续膨胀、prompt 变大，LLM 还可能"偷懒错沿用"；
- 长回复链打标签只送 `rows[:20]`（`topic_service.py:312`），长链上下文被硬截断。

### 2.5 无评估手段
- 改好改坏全靠感觉，缺少可量化指标（标签发散度 / 合并率 / 误合率）。

---

## 3. 优化目标与原则

1. **确定性优先于 LLM**：能靠规则/精确匹配解决的问题，不让 LLM 定价（省钱 + 稳定）；
2. **可回滚优先于一次性**：归并先"关联"再"合并"，物理合并只作为用户确认后的最终动作；
3. **治理优先于新算法**：先修标签归一化（同义词收敛），再谈聚类算法升级；
4. **增量不改存量语义**：默认只对新数据生效，存量用脚本按需回填 + dry-run 预览；
5. **设计可度量**：每个改造都配套指标，落版后能回看效果。

---

## 4. 详细设计

### 4.1 P0-A：标签别名表 + 归组等价判定

**目标**：把「字符串相等」升级为「归一化等价 + 别名收敛 + 低频近义兜底」，同时收敛打标与归组。

#### 4.1.1 新增表 `topic_aliases`

```
CREATE TABLE topic_aliases (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  alias       VARCHAR(128) NOT NULL,   -- 别名(同义词),唯一
  canonical   VARCHAR(128) NOT NULL,   -- 收敛到的标准标签
  created_at  DATETIME NOT NULL,
  UNIQUE KEY uq_topic_aliases_alias (alias),
  KEY ix_topic_aliases_canonical (canonical)
)
```

- **标准标签**与**自由标签**都允许出现 `canonical`；标准标签的 canonical 应等于自身（或用 `TopicGroup` 表标记来源，避免双份维护）。
- 命名约定：**alias → canonical 一律双向归一**（打标时把 alias 输出映射到 canonical；归组时两侧都先做 canonical 化再比较）。

#### 4.1.2 标签归一化管线（新增 `app/services/label_norm.py`）

LLM 原始输出 → `normalize_label()`：

```
1. 清洗: 复用 _clean_label(去引号/标点/截断/语气词 → 空)
2. 去空白/全半角/大小写统一
3. 精确命中别名表?   → 返回 canonical(强收敛)
4. 精确命中标准标签? → 返回该标准标签
5. 兜底: 在库内查 2-gram Dice 相似度(复用 cluster_service._dice_counter/_char_bigrams)
       与既有标签 ≥ 阈值(默认 0.55, 可配) → 返回最相似者(可追加写入别名表,标记 auto)
   否则返回原样
```

- 阈值须**保守偏高**（宁可保留新标签，不可误并不同问题）；
- 相似度只在**别名/精确都未命中**时兜底，控制误收敛面；
- 打标路径 `classify_group` 输出前过一遍 `normalize_label`；标准标签本身已强制原样沿用（提示词既有约束）不受影响。

#### 4.1.3 归组判定升级（`group_merge.py` / `topic_service.py`）

- `plan_merges` 与 `classify_group` 里的 `label not in standards` 判断，从"字符串相等"
  改为 **canonical 相等**（先 `normalize_label` 两侧再比较）；
- `merge_groups` 的幂等与"已归并"判断同步走 canonical，避免 A↔B 别名重复归并。

#### 4.1.4 别名表维护入口

- **自动建议**：当某条 auto 归一命中被 LLM 再次产出时，标记 `auto` → 可由管理员在 `/summary` 一键确认；
- **手动维护**：`/api/topic_aliases` 四个 CRUD 端点（GET/POST/DELETE），与 `topic_groups` 同风格；
- 变更后提供幂等回填脚本：遍历存量 `distinct Message.topic`，对命中别名的行批量 UPDATE 到 canonical。

### 4.2 P0-B：设备标识 case_key（归并/缝合确定性锚点）

**目标**：让「同一台机器/同一事项」的判定拥有**确定性精确匹配**，LLM 只做兜底。

#### 4.2.1 抽取器（新增 `app/services/case_key.py`）

从 `Message.text`（含 post 富文本转出文本）用正则抽取，按优先级取第一个命中：

| 信号 | 正则示例 | 说明 |
|---|---|---|
| UUID | `[0-9a-fA-F]{8}[-][0-9a-fA-F]{4}...` | 唯一性最强 |
| 设备 SN | `(?i)\b(SN[:：]?|序列号[:：]?)\s*([A-Z0-9]{6,24})\b` | 需在文本中出现"SN/序列号"标签才采信 |
| MAC | `(?i)([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}` | 排除全 0 / 广播 |
| IP + 前缀 | `(?i)(设备)?\bIP[:：]?\s*(\d{1,3}(\.\d{1,3}){3})\b` | 网络类故障 |

- 验证兜底：**至少保留一位错误校验**（如 SN 长度区间、UUID 结构），防误抽"订单号"当设备号；
- 结果写入 `messages.case_key`（新列，空串=未识别），入库时同步生成（`message_service` 或归组回填）；
- 抽取是**纯规则 + 每消息一次正则**，成本极低。

#### 4.2.2 归并/缝合锚点升级（`conv_stitch.py` + `group_merge.py`）

`plan_stitches` 窗口内判定改为**分层决策**：

```
同窗口相邻会话 src/tgt:
  ├─ src.case_key 或 tgt.case_key 非空 且 相等(且 ≠ 占位"0") → 直接判定同一案例, 不再调 LLM
  ├─ 一方有 case_key 另一方为空 且 时间跨长(>6h) → 不合并(信息不足, 留给 LLM)
  └─ 都无 case_key → 维持现状, 交 LLM 判定
```

- 现有 `_pair_allowed` 标签冲突护栏**保留**，作为最后的硬防线；
- `merge_groups` 归并后 `case_key` 随消息走（消息被改挂目标组，字段天然随行），无需额外处理；
- 额外收益：同标签归并（4.1.3）也可先用 case_key 精确归组，再走 canonical。

### 4.3 P1-A：软合并（case_id 关联，可解绑）

**目标**：把「物理合并」降级为「逻辑关联 + 可回滚」，物理合并仅作为确认后的最终动作。

#### 4.3.1 新增列 `conversations.case_id`

- `BIGINT NOT NULL DEFAULT 0`，0=独立案例；`INDEX(chat_id, case_id)`；
- **只关联、不改 root/conversation_id、不删源对话行**：
  - 打上同一个 `case_id` 的多个 conversation 仍各自独立存储；
  - 展示层（`/api/summary`、`renderTimeline`）按 `case_id` 聚合渲染为"同一案例的多段对话"，
    段间仍画横杠（保留原问题块语义）。

#### 4.3.2 决策关系

| 场景 | 现状 | 改造后 |
|---|---|---|
| 同标签归组（碎片反馈归回原问题块）| 物理合并（保留横杠标记 block_start）| 维持物理合并（这是用户预期：碎片并入原问题块）|
| 会话缝合（同一案例续聊）| 物理合并（不画横杠）| **默认软合并**：打 `case_id` 关联 |
| 管理员确认要合并 | — | 提供"确认合并"动作 → 再走 `merge_groups` 物理合并 |

- 软合并可逆：`case_id` 解绑 = UPDATE 回 0 即可，天然可回滚；
- 缝合链路收益：LLM 误判只会错误关联（可解绑），不再产生不可逆数据损坏。

### 4.4 P1-B：样本注入与长链上下文治理（成本与准确率）

| 现状 | 改造 |
|---|---|
| `_existing_topic_samples` 全量送 50 个主题 | 改为**按最近活跃 + 与当前文本共现词**抽样，上限收敛到 20；仍先同群后全局补位 |
| 长链 `rows[:20]` 硬截断 | 改为**代表消息抽样**：首条 + 末条 + 最长 3 条 + 有图片/文件的消息元数据（复用 `conv_stitch._repr_lines` 思路），超过 20 条即抽样而非截断 |
| 无信息量组也调 LLM | 维持现有"纯附件组跳过"策略，并补充：`normalize_label` 输出为空即算失败，落 `_last_failure` 冷却（现有机制已具备，无需新增）|

### 4.5 P2：指标埋点（先量化，再判断）

新增只读统计端点 `/api/topic_metrics`（或并入 `/api/topics`）：

| 指标 | 定义 | 用途 |
|---|---|---|
| 标签总数 / 低频标签数 | `distinct topic` 中仅出现 1 组的标签数 | 同义词漂移的直接度量（目标：低频标签占比下降）|
| 归一化命中率 | 打标路径中落到别名/相似度收敛的比例 | 衡量别名表覆盖率 |
| 归并率 | 被 `merge_topic_group` 实际并入的组数 / 同标签组总数 | 归并是否正常生效 |
| 缝合确认率 | `case_key` 命中直接判定数 / 窗口判定总数 | 衡量确定性锚点接管比例 |
| 误合率（抽样）| 人工抽查 N 组合并结果的一致性 | 校准阈值 |

> 注意：`MessageRepo.count()` 等全表聚合需改用 `func.count()`（见问题清单 §2 的工程项），
> 指标端点建立在高效 count 之上，避免加重 `messages` 表负担。

---

## 5. 数据模型变更清单

| 变更 | 类型 | 说明 | 迁移方式 |
|---|---|---|---|
| `topic_aliases` 表 | 新增 | alias → canonical 收敛 | 模型 + `create_all`；存量库走 init_db 内轻量迁移脚本（`_ensure_*` 同款）|
| `messages.case_key` 列 | 新增 | VARCHAR(64) NOT NULL DEFAULT '' | `_ensure_case_key_column()`；回填脚本 `scripts/backfill_case_key.py` 幂等扫描 |
| `conversations.case_id` 列 | 新增 | BIGINT NOT NULL DEFAULT 0, index | `_ensure_conversation_case_id()`；软合并专用 |

> 全表 DDL 遵循现有 `db.py::_ensure_*` 轻量迁移模式：先 `inspector` 查列、缺才 `ALTER TABLE`，
> 同时兼容 SQLite / MySQL / PostgreSQL。

---

## 6. 涉及代码文件与改动点

| 文件 | 改动 |
|---|---|
| `app/models/message.py` | +`case_key` 列 |
| `app/models/conversation.py` | +`case_id` 列 |
| `app/models/topic_alias.py`（新增）| 别名表模型 |
| `app/core/db.py` | +`_ensure_case_key_column` / `_ensure_conversation_case_id` / 建别名表 |
| `app/services/label_norm.py`（新增）| 归一化管线 + Dice 兜底（复用 `cluster_service` 的 `_char_bigrams/_dice_counter`）|
| `app/services/case_key.py`（新增）| 设备标识抽取器 |
| `app/services/topic_service.py` | `_llm_label` 输出过 `normalize_label`；`_existing_topic_samples` 抽样治理；代表消息抽样 |
| `app/services/group_merge.py` | 归组判定改 canonical 等价；可选 case_key 精确归组 |
| `app/services/conv_stitch.py` | case_key 确定性判定优先；默认走软合并（case_id）；`_pair_allowed` 保留 |
| `app/services/conv_stitch_service.py` | 水位线不变 |
| `app/api/routes.py` | +`/api/topic_aliases` CRUD；+/api 软合并确认动作；+/api/topic_metrics |
| `app/static/summary.js` / `.html` | 按 case_id 聚合展示 + 别名/软合并管理入口 |
| `scripts/backfill_case_key.py`（新增）| 存量 case_key 幂等回填 |
| `scripts/backfill_aliases.py`（新增）| 存量 topic 归一化回填（可选）|

---

## 7. 与只读 `cluster_service` 的关系（本次不动）

`cluster_service.cluster_chat`（L1 降噪 / L2 切段 / L4 衰减质心聚类）是**只读旁路**，
解决"时序交织还原"，与本次"标签治理 / 归并锚点"目标正交。本次不改造它，但有两个结论沿用：
- 4.1.2 兜底相似度的 `_dice_counter` / `_char_bigrams` 直接复用 `cluster_service` 特征函数，避免重复实现；
- 若未来要把聚类结果接入展示，再做"合并两套体系"的独立议题，不在本迭代内。

---

## 8. 兼容性 / 迁移 / 回滚

- **新列默认空字符串/0**：不感知新字段的旧代码/存量数据不受影响，语义不变；
- **归一化仅影响新写入**：旧话题不动；`scripts/backfill_aliases.py` 按需收敛存量；
- **物理合并语义保留**：同标签归组维持原行为（碎片并入原问题块 + 保留横杠标记），避免改变用户已有认知；
- **可回滚**：软合并只需 `case_id` 归 0；别名/阈值均为配置项，改回字符串相等即可整体回退；
- **部署**：按既有流程本地 commit → 用户 push → 服务器 `deploy/update.sh`；
  该文档本身不入库任何敏感信息。

---

## 9. 测试方案

- **label_norm**：同义词收敛（"投屏失败"→"投屏"）、未命中保留原样、阈值边界（0.54 vs 0.56）、
  语气词→空、标准标签原样沿用；
- **case_key**：UUID / SN / MAC / IP 正反样例；误抽防护（订单号不当 SN）；空文本 / 非文本消息返回空；
- **归组/缝合**：canonical 等价归组 true/false 用例；case_key 相等直接判定（不调 LLM，注入 fake LLM 验证未被调用）；
  软合并只写 `case_id`、不改 root/conversation_id、可解绑；标签冲突护栏仍拦截；
- **回填脚本**：幂等（跑两遍结果一致）、dry-run 不写库；
- **回归**：现有 `tests/` 106 项保持全绿（打标签/归组/缝合测试需同步适配归一化后的标签输出）。

---

## 10. 分阶段落地顺序（建议）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| 阶段一 | 别名表 + 归一化管线 + 归组等价判定（P0-A）| `/summary` 近义标签收敛、低频标签占比下降 |
| 阶段二 | case_key 抽取 + 归并/缝合锚点（P0-B）| 缝合 LLM 调用明显减少、案例归并更准 |
| 阶段三 | 软合并 case_id（P1-A）| 缝合改为可解绑关联、误合可一键回退 |
| 阶段四 | 样本注入治理 + 指标埋点（P1-B / P2）| token 成本下降、指标面板可量化效果 |

> 每阶段独立可上线、可回滚，避免一次性大改造。阶段一不需要新数据模型外的复杂依赖，
> 建议优先启动。

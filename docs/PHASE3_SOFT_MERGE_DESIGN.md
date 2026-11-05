# 阶段三：软合并 case_id（P1-A 落地）设计文档

> 定位：把「会话缝合＝物理合并（不可回滚）」降级为「软合并＝case_id 关联（可回滚）」，物理合并仅作为管理员确认后的最终动作。
>
> 前置：阶段一（别名表＋归一化，commit 2baa1d0）、阶段二（case_key 确定性锚点，commit 964d0dc）已完成。
> 本文档是阶段三的**落地实现方案**，含目标/进度/缺口/分项改造/影响/验收/不变约束。

---

## 1. 阶段三目标（不变，来自总设计 §4.3）

1. **缝合默认软合并**：`stitch_chat` 判定出「同一案例」后，只给相关会话打上相同 `case_id`，**不删行、不改 root、不搬消息**；
2. **可回滚**：误合可通过「解绑」把 `case_id` 归 0，数据零损失；
3. **物理合并降级为人工动作**：只有管理员「确认合并」才走 `merge_groups`；
4. **展示可读**：`/summary` 能按 `case_id` 把「同一案例的多段对话」聚合渲染，段间画横杠保留问题块语义。

---

## 2. 已完成进度与现状（基于当前代码）

| 模块 | 当前状态 | 是否满足阶段三 |
|---|---|---|
| `conversations` 表 | 无 `case_id` 列（`conversation.py`） | ❌ 缺口 G1 |
| `db.py` 迁移 | 仅有 `_ensure_case_key_column`，无 `_ensure_conversation_case_id` | ❌ 缺口 G1 |
| 缝合计判定 `plan_stitches` | 已支持 case_key 确定性优先＋LLM 兜底＋`_pair_allowed` 护栏（阶段二） | ✅ 判定层就绪 |
| 缝合执行 `stitch_chat` | 逐对调用 `merge_groups` **物理合并**（删源对话行、改 root） | ❌ 缺口 G2 |
| case_key 锚点落地 | 锚点已用于判「是否同一案例」，但结果仍落入不可逆合并 | ❌ 缺口 G3（锚点能力被浪费） |
| 前端 `summary.js`/API | 无 case_id 概念，仅 block_start 横杠 | ❌ 缺口 G4 |
| 管理动作 | 无「确认合并 / 解绑」API | ❌ 缺口 G5 |
| 指标 | 无缝合率/误合率的量化出口 | ❌ 缺口 G6（阶段三验收依赖） |

**结论**：阶段三处于**设计完成、代码零落地**状态；阶段二已经为它备好了「确定性锚点＋判定分层」的判定层，缺口集中在**执行层（物理→软）、存储层（case_id 列）、治理层（API）、展示层（聚合）**。

---

## 3. 尚未解决的问题（缺口清单）

- **G1 存储**：切开软合并所需 `conversations.case_id` 列与迁移。
- **G2 执行语义**：`stitch_chat` 仍物理合并——这是「不可回滚」的根源。
- **G3 锚点串联**：case_key 已能在判定时省钱/提稳，但结果还是不可逆合并，锚点红利没兑现到「可回滚」上。
- **G4 展示**：用户看不到「同一案例的多段对话」已被关联，软合并对用户不可见。
- **G5 治理**：没有解绑 = 软合并形同虚设；没有确认合并 = 无法收敛为物理合并。
- **G6 量化**：缝合是否变准、误合能否快速暴露，缺指标出口。

---

## 4. 分项改进方案（每项含对整体流程的影响）

### S1 数据模型：`conversations.case_id` 列 + 轻量迁移

- `conversation.py`：
  ```python
  # 所属案例组代表会话 id;0=独立案例。软合并专用,只关联不移行
  case_id: Mapped[int] = mapped_column(
      BigIntPK, nullable=False, default=0, index=True
  )
  ```
  `__table_args__` 追加 `Index("ix_conversations_chat_case", "chat_id", "case_id")`，支撑「按群＋案例聚合」查询。
- `db.py` 新增 `_ensure_conversation_case_id()`：`inspector` 查缺列 → `ALTER TABLE conversations ADD COLUMN case_id BIGINT NOT NULL DEFAULT 0`；`init_db` 挂上。
- **对整体流程的影响**：为软合提供持久化载体；默认 0＝独立案例，存量/未升级代码语义完全不变，零风险增量。是所有后续改动的地基。

### S2 新增「案例归组解析」：软合并的并查集语义

关键难点：软合并允许 A→B、C→A 形成两级，必须保证**同组共享同一 case_id，且指向该组最早会话**。解决：

```python
def resolve_case_root(session, conv_id) -> Conversation:
    """沿 case_id 链向上找根代表(路径压缩)。"""
def align_case(session, chat_id, case_id, root: Conversation):
    """把 case_id 组内全部会话的 case_id 统一为 root.id(扁平化)。"""
```

- 每次软合并写库**前**解析到根代表，再整组对齐到全局最早会话，杜绝链式漂移。
- **对整体流程的影响**：软合并组始终是「一棵拍平的树」，前端聚合与后续物理合并都基于同一稳定语义，避免 A→B→C 后 C 落错组。

### S3 缝合执行层改软合并：`stitch_chat` → 软链接

```python
def soft_link_cases(session, chat_id, src_convid, tgt_convid) -> bool:
    src_root, tgt_root = resolve_case_root(src), resolve_case_root(tgt)
    if src_root.id == tgt_root.id:           # 幂等
        return False
    # 只写 case_id:两组成员统一指向 tgt_root(全局更早)
    align_case(..., tgt_root.id); session.commit()
```

`stitch_chat` 循环内把 `merge_groups(...)` 替换为 `soft_link_cases(...)`（真实 LLM 判定到的对也走软合并；case_key 确定性对同样走软合并，二者一致）。`_pair_allowed` 护栏保持在 plan 层，逐对校验不变。

- **对整体流程的影响**：缝合从「不可逆数据损坏」变成「可回滚关联」。LLM 误判的代价从"污染主数据"降为"多了一条可解绑关联"，风险量级显著下降；同时保留阶段二的省钱/提稳收益。

### S4 管理动作 API：确认合并 / 解绑 / 列表（治理闭环）

| 端点 | 动作 | 说明 |
|---|---|---|
| `GET /api/cases` | 列出全部软合并组 | `case_id>0` 去重，附成员数/时间范围/topic/消息数，供「待确认」列表 |
| `POST /api/cases/{case_id}/merge` | 管理员确认 → 物理合并 | 组内会话按时间链式 `merge_groups` 并入最早；成功后 `case_id` 归 0 |
| `POST /api/cases/{case_id}/unlink` | 解绑 → 全组归 0 | 纯逻辑回滚，行/消息/root 一律不动；未 merge 前随时可解绑 |

- **对整体流程的影响**：软合并形成「自动关联 → 人工确认/解绑 → （可选）物理合并」闭环。误合可直接在界面一键回退，无需运维脚本，「合并难回滚」这一历史痛点被彻底移除。

### S5 展示层：按 case_id 聚合

- 后端：`GET /api/cases/{case_id}/timeline` 返回该案例跨会话、按时间平铺的消息（复用 `_tree_dict` 节点 + `fill_tree_sender_names`），前端据此渲染「完整案例时间线」（多段间画横杠，顶部标「同一案例 N 段」）。
- `summary.js`：在 topic 展开/对话视图里，对 `case_id>0` 的会话提供「查看完整案例」入口，并拢到一条时间线。
- **对整体流程的影响**：软合并对用户可见、可验证——同一案例的碎片反馈不再散落，展示层成为「关联是否正确」的人工校验窗口（缺口 G4 关闭）。

### S6 指标：`GET /api/cases/stats`（阶段三最小量化集）

| 指标 | 定义 |
|---|---|
| 软合并组数 | `count(distinct case_id where case_id>0)` |
| 涉及会话数 | `count(*) where case_id>0` |
| 确定性缝合占比 | case_key 命中直接软合并的对数 / 总缝合对数（阶段二日志已有，汇总于此）|
| 人工确认/解绑次数 | 落在 S4 端点，计数器/审计 |

- **对整体流程的影响**：给阶段三验收提供可量化出口——能明确回答「缝合有没有在可回滚状态下正确生效、误合有没有暴露并被纠正」。完整指标面板（P2）留待阶段四。

---

## 5. 可量化验收标准（逐项对应测试）

| # | 验收项 | 判定方式（测试/断言） |
|---|---|---|
| V1 | 迁移对存量/新建库生效 | 建临时库跑 `init_db` → conversations 含 `case_id`；无该列时 ALTER 成功，默认 0 |
| V2 | 软合并不破坏数据 | A/B 两会话 case_key 相同 → `stitch_chat` 后：两者 `case_id` 相同非 0；`root_message_id` 均不变；conversation 行数仍为 2；`messages.conversation_id` 不变 |
| V3 | 确定性通道免 LLM | 注入 fake LLM（带调用计数）→ 同 case_key 缝合时计数为 0（阶段二已验判定层，补执行层断言）|
| V4 | 幂等 | 同组再次 `stitch_chat` → 无新增关联、无异常 |
| V5 | 解绑 | unlink 后全组 `case_id` 归 0；conversation 行数/消息归属与解绑前完全一致 |
| V6 | 确认合并 | merge 后组内多行收敛为 1 行（最早为根），`case_id` 归 0，消息并入目标 root |
| V7 | 主题冲突护栏仍拦截 | topic 冲突对即使 case_key 相同也拒绝软合并（V2 反例） |
| V8 | 回归 | 现有 123 项全绿 + 新增用例；`tests/test_case_soft_merge.py` 覆盖 V2–V7 |

> 运行方式与既有一致：本地 `pytest`（SQLite 内存库）跑通；冒烟脚本 `--dry-run` 预览软合并对，不写库。

---

## 6. 优化过程中必须保持不变的约束条件

1. **同标签归组语义不变**：`merge_topic_group` 对「碎片反馈并入原问题块」维持物理合并＋`block_start` 横杠（这是用户既有预期，不因软合并改变）；
2. **护栏不可移除**：`_pair_allowed` 主题冲突拦截在判定的 plan 层保留，作为最后硬防线；
3. **确定性优先于 LLM**：case_key 锚点在阶段三中既省钱又提稳，优先级不倒退；
4. **增量不改存量语义**：新列默认 0＝独立案例；存量不自动改，按需回填 + dry-run 预览；
5. **轻量迁移模式**：沿用 `_ensure_*`（inspector → ALTER），兼容 SQLite/MySQL/PostgreSQL，不引入 Alembic；
6. **AI 只做本地工作**：改代码、写测试、本地 commit；**不 push**；push 与服务器部署（`deploy/update.sh`）由用户执行；
7. **本地验证规范**：本地 LLM 未配置时"假成功"会掩盖启用路径 bug，故实现后用 fake LLM（实现 `.chat()`）跑通缝合→软合并→解绑/确认全链路；
8. **指标端点只读**：`/api/cases/stats` 只聚合不写库，不加重主链路负担。

---

## 7. 涉及文件与落地顺序

| 顺序 | 文件 | 改动 |
|---|---|---|
| 1 | `app/models/conversation.py` | +`case_id` 列 + 复合索引 |
| 2 | `app/core/db.py` | +`_ensure_conversation_case_id()` 挂入 `init_db` |
| 3 | `app/services/case_merge.py`（新增）| `resolve_case_root` / `align_case` / `soft_link_cases` |
| 4 | `app/services/conv_stitch.py` | `stitch_chat` 改调 `soft_link_cases` |
| 5 | `app/api/routes.py` | +`/api/cases`、`/api/cases/{id}/merge|unlink`、`/api/cases/{id}/timeline`、`/api/cases/stats` |
| 6 | `app/static/summary.js`/`.html` | case 聚合视图 + 确认/解绑入口 |
| 7 | `tests/test_case_soft_merge.py`（新增）| V2–V7 用例 |
| 8 | `scripts/backfill_case_id.py`（可选新增）| 存量按 case_key 软合并回填 |

> 最终本阶段不改变话题分类/标签归一化/归组逻辑，只改「同一案例如何组织」这一层。

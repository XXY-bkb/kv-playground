# 设计说明：kv-playground

这份文档记录“为什么这么实现”，方便未来的自己和面试时讲清楚。
代码里的中文注释解释“这段在干什么”，这里解释“为什么是这种方式”。

## 1. 目标与非目标

**目标**

- 用最少的代码把 LLM 推理里最容易被讲糊涂的几件事画出来：
  分页 KV cache、连续批处理、前缀缓存、显存不足时的抢占重算。
- 模拟器可测试、可复现：同一场景每次导出的 trace 字节一致。
- 前端零依赖、零构建，GitHub Pages 直接托管。

**非目标**

- 不跑真实模型，不追求 GPU 吞吐数字。
- 不做通用推理框架，不实现 swap / TP / 完整 chunked prefill。
- 不追求 UI 动画的“炫”，优先让每一步的状态变化可解释。

## 2. 数据模型与不变量

- `BlockManager` 维护一个空闲池 `free_blocks`、所有块的元数据 `blocks`、
  每个序列的块表 `seq_blocks`。
- 一个块可以被多个序列共享（前缀命中），所以用 `ref_count` 而不是布尔“占用”标记。
- 不变量（`BlockManager.validate()` 检查 1、2、4；3 由分配/释放测试覆盖）：
  1. 空闲池里的块 `ref_count == 0`；
  2. 一个块要么在空闲池，要么至少被一个序列或前缀缓存引用；
  3. 序列释放只减引用，归零才回空闲池；
  4. 同一个块不会在一个序列的块表里出现两次。

## 3. 调度顺序

每个模拟步：

1. 到达：把 `arrival_step == step` 的请求放进 waiting；
2. `schedule()`：
   - 先给 running 序列做 prefill（按 token 预算切分）；
   - 再给 running 序列做 decode（每条 1 token）；
   - 最后才尝试接纳 waiting 里的新请求；
3. `postprocess()`：更新 `num_computed_tokens` / `num_output_tokens`，完成即释放；
4. 把本步 prefill 产生的完整页登记进前缀缓存；如果 prefill 当步就完成
   （`output_len == 1`），`_finish()` 会先把 prompt 完整页登记进缓存再释放块；
5. 记录快照（块池、块表、事件、指标）。

**为什么 running 优先**：如果先接纳新请求，新请求可能一次拿走大量块，
正在 decode 的请求反而要抢占自己或别人，出现“刚跑起来又被踢掉”的抖动。
running 优先让已经在跑的请求先推进，新请求排队，行为更接近真实调度器的直觉。

**新请求第一步只 ADMIT**：接纳时只分配块、改状态；真正 prefill 在下一步。
这样 trace 里“接纳”和“预填充”是两个清晰的事件，测试也按这个约定写。

## 4. 前缀缓存

- 哈希：`h0 = H(page0)`，`h1 = H(h0 + page1)`，……；用 `blake2b(digest_size=8)`。
- 只缓存**完整页**；最后不满一页的 token 不参与匹配。
- 防止哈希碰撞：额外保存 `block_tokens[block_id]`，命中时逐 token 比对。
- LRU：命中会把块移到末尾；驱逐从头部取。
- 缓存块和序列块共用 `ref_count`：缓存持有一份引用，所以序列释放后块不会立刻消失；
  这也是“块不够时只抢占 running 仍然死锁”的原因，必须有驱逐缓存的路径。
- 序列在 prefill 当步就完成（`output_len == 1`）时，`_finish()` 会在释放块之前
  先把 prompt 完整页登记进缓存，否则这些块会“刚写完就丢”。

## 5. 块不够时：先驱逐可回收缓存，再抢占

`_ensure_blocks` 的顺序：

1. 先按 LRU 驱逐“只被缓存持有”的块（`ref_count == 1`）：这种块没有序列在用，
   驱逐后立刻回到空闲池，代价只是以后少一次前缀命中；
2. 如果还是不够，找一个不在当前 batch 里的 running 序列作为 victim；
3. 抢占它：释放它的块引用、重置已计算 token / 输出 / TTFT、放回 waiting 队首；
4. 抢占后 victim 的块可能还挂在缓存上，再驱逐一次缓存，然后重试分配；
5. 还不够就放弃本步（该序列继续等）。

`_try_admit` 同理：先 match 前缀，块不够时只驱逐没有被序列持有的缓存块，
然后**重新 match**（驱逐可能把候选块删掉），再分配。缓存块如果都被 running
持有，就让它继续等，不为了接纳新请求去破坏正在跑的计算。

为什么先驱逐、后抢占？缓存块只是“以后可能复用”，丢掉它比丢掉 running 的
计算进度便宜；而被 running 持有的缓存块驱逐了也腾不出空闲块，所以只驱逐
`ref_count == 1` 的块。两者都要有，否则在“缓存把块挂住”时会死锁。

## 6. 确定性

- 空闲池用 list，`pop(0)` 取最久未用的块，顺序固定；
- token 由请求 id/序号确定性生成，不用随机数；
- 哈希用 blake2b，固定输入；
- JSON 输出 `sort_keys=True`、固定缩进；
- 测试 `test_export_is_deterministic` 比较两次导出的字节。

## 7. 已知限制

- `total_steps` 是模拟步数；不能当成真实 tok/s。
- prefill 预算只做“按 token 切分”，没有建模 chunked prefill 的调度策略。
- 没有 swap、TP、CUDA Graph、真实 kernel 的时序。
- 前缀缓存只有单层 LRU，没有真实引擎里的多级缓存/引用计数细节。
- 真实引擎 trace 导入（`micro-vllm` → 本格式）是下一阶段。

## 8. 测试地图

| 文件 | 覆盖 |
|---|---|
| `tests/test_block_manager.py` | 分配/释放/fork/缓存引用/双重释放/脏空闲池/块泄漏 |
| `tests/test_prefix_cache.py` | 命中、前缀链防误命中、半页、哈希碰撞、LRU 驱逐（跳过 pinned） |
| `tests/test_scheduler.py` | 接纳→prefill→decode、预算拆分、抢占后能跑完、prefill 当步完成时缓存 prompt |
| `tests/test_simulator.py` | 单请求、确定性、共享前缀、OOM、每步不变量、缓存优先于抢占 |
| `tests/test_export.py` | manifest、schema、确定性 |

"""调度器：决定“这一步让哪些请求跑、跑多少 token”。

真实推理引擎的调度器要处理很多细节（chunked prefill、优先级、swap、多 GPU），
这里只保留最能说明问题的四个规则：
1. prefill 优先：running 里还没算完 prompt 的序列先算；
2. 连续批处理：每一步都重新组 batch，而不是等一整批一起结束；
3. 预算限制：一步里 prefill 的 token 总数不能超过 max_num_batched_tokens；
4. 显存不足时先回收未被序列持有的缓存块，再抢占 running 序列。

执行顺序很重要：**先照顾 running，再接纳 waiting**。
如果反过来，新请求可能把块占光，正在 decode 的序列拿不到新块，
就会出现“反复抢占、谁都跑不完”的抖动。running 优先能避免这个问题。

为了便于观察，抢占采用“重算”（recompute）：被抢占的序列下次从第 0 个
token 重新 prefill，不模拟 swap 到 CPU。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .block_manager import BlockManager, OutOfBlocksError
from .model import SeqStatus, Sequence
from .prefix_cache import PrefixCache


@dataclass
class PrefillChunk:
    """一次 prefill 计算的区间：[start, end) 个 token。"""

    seq: Sequence
    start: int
    end: int

    @property
    def tokens(self) -> int:
        return self.end - self.start


@dataclass
class Batch:
    """一步算出来的 batch。"""

    prefill: list[PrefillChunk] = field(default_factory=list)
    decode: list[Sequence] = field(default_factory=list)

    @property
    def seqs(self) -> list[Sequence]:
        """本 batch 涉及的所有序列（去重，保持顺序）。"""
        seen: set[str] = set()
        result: list[Sequence] = []
        for chunk in self.prefill:
            if chunk.seq.id not in seen:
                seen.add(chunk.seq.id)
                result.append(chunk.seq)
        for seq in self.decode:
            if seq.id not in seen:
                seen.add(seq.id)
                result.append(seq)
        return result

    @property
    def is_empty(self) -> bool:
        return not self.prefill and not self.decode


class Scheduler:
    def __init__(
        self,
        block_manager: BlockManager,
        prefix_cache: PrefixCache,
        max_num_seqs: int = 4,
        max_num_batched_tokens: int = 512,
        use_prefix_cache: bool = True,
    ) -> None:
        self.bm = block_manager
        self.cache = prefix_cache
        self.max_num_seqs = max_num_seqs
        self.max_num_batched_tokens = max_num_batched_tokens
        self.use_prefix_cache = use_prefix_cache

        self.waiting: list[Sequence] = []
        self.running: list[Sequence] = []

        # 本步刚被抢占的序列，不在同一步重新接纳，避免“抢了又立刻放进来”的抖动
        self._preempted_this_step: set[str] = set()

    # ------------------------------------------------------------------
    # 外部入口
    # ------------------------------------------------------------------
    def add_request(self, seq: Sequence) -> None:
        """请求到达，进入等待队列。"""
        seq.status = SeqStatus.WAITING
        self.waiting.append(seq)

    def schedule(self) -> tuple[Batch, list[dict]]:
        """组出这一步要执行的 batch，并返回调度过程中发生的事件。"""
        events: list[dict] = []
        batch = Batch()
        self._preempted_this_step = set()
        budget = self.max_num_batched_tokens

        # 1) 先照顾已经在 running 的序列：prefill 优先，然后 decode。
        #    它们已经占着块，优先让它们前进，系统整体进度才不会被 waiting 拖住。
        for seq in list(self.running):
            if budget <= 0:
                break
            if seq.status is not SeqStatus.RUNNING:
                continue
            if seq.num_computed_tokens >= seq.prompt_len:
                continue  # prompt 已完成，走 decode

            chunk_tokens = min(seq.prompt_len - seq.num_computed_tokens, budget)
            if chunk_tokens <= 0:
                continue
            target = seq.num_computed_tokens + chunk_tokens

            if not self._ensure_blocks(seq, target, batch, events):
                continue

            batch.prefill.append(
                PrefillChunk(seq=seq, start=seq.num_computed_tokens, end=target)
            )
            events.append(
                {
                    "type": "PREFILL_START",
                    "seq": seq.id,
                    "start": seq.num_computed_tokens,
                    "end": target,
                }
            )
            budget -= chunk_tokens

        for seq in list(self.running):
            if budget <= 0:
                break
            if seq.status is not SeqStatus.RUNNING:
                continue
            if seq.num_computed_tokens < seq.prompt_len:
                continue  # 本步还没 prefill 完
            if seq.num_output_tokens >= seq.output_len:
                continue

            target = seq.num_computed_tokens + 1
            if not self._ensure_blocks(seq, target, batch, events):
                continue

            batch.decode.append(seq)
            budget -= 1

        # 2) 再接纳 waiting：只有还有并发槽位、并且块够用时才放进来。
        #    放不进来就让它在队列里等，不为了“接纳”去抢占 running。
        while self.waiting and len(self.running) < self.max_num_seqs:
            seq = self.waiting[0]
            # 刚被抢占的序列本步不再接纳，下一步再试
            if seq.id in self._preempted_this_step:
                break
            if not self._try_admit(seq, events):
                break
            self.waiting.pop(0)

        return batch, events

    def postprocess(self, batch: Batch, step: int) -> list[dict]:
        """batch 执行完后的状态更新：写 token 数、判完成、释放块。"""
        events: list[dict] = []

        # 先把 prefill 的结果落账
        for chunk in batch.prefill:
            seq = chunk.seq
            if seq.status is not SeqStatus.RUNNING:
                continue
            seq.num_computed_tokens = chunk.end

            if seq.num_computed_tokens >= seq.prompt_len:
                # prompt 算完，立刻采样第 1 个输出 token；
                # 这个 token 的 KV 要等下一次 decode 才写进去。
                seq.num_output_tokens = max(seq.num_output_tokens, 1)
                seq.ttft_step = step
                events.append(
                    {"type": "PREFILL_END", "seq": seq.id, "tokens": chunk.end}
                )
                if seq.num_output_tokens >= seq.output_len:
                    self._finish(seq, step, events)

        # 再处理 decode：每个 token 写进 KV cache，输出计数 +1
        for seq in batch.decode:
            if seq.status is not SeqStatus.RUNNING:
                continue
            if seq.num_output_tokens >= seq.output_len:
                continue

            seq.num_computed_tokens += 1
            seq.num_output_tokens += 1
            events.append(
                {
                    "type": "DECODE",
                    "seq": seq.id,
                    "output_index": seq.num_output_tokens,
                }
            )
            if seq.num_output_tokens >= seq.output_len:
                self._finish(seq, step, events)

        return events

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _try_admit(self, seq: Sequence, events: list[dict]) -> bool:
        """尝试把一个等待请求放进 running；放不下返回 False。"""
        prompt_tokens = seq.token_ids[: seq.prompt_len]

        # 先查前缀缓存。命中只代表“这些块可以共享”，还没真正持有引用。
        matched_blocks: list[int] = []
        matched_tokens = 0
        if self.use_prefix_cache:
            matched_blocks, matched_tokens = self.cache.match(prompt_tokens)

        required = (seq.prompt_len + self.bm.page_size - 1) // self.bm.page_size
        need_new = required - len(matched_blocks)
        if not self.bm.can_allocate(need_new):
            # 空闲池不够，但可能有块只是被前缀缓存“挂着引用”。
            # 只驱逐没有被序列持有的缓存块；被 running 持有的块驱逐了也不会
            # 立刻空闲，别为了接纳新请求去破坏正在跑的计算。
            if not self._evict_cache(need_new, events):
                return False

            # 驱逐可能把刚才 match 到的候选块也删了，必须重新 match 一次，
            # 否则会拿着已经失效的 block id 去 fork。
            if self.use_prefix_cache:
                matched_blocks, matched_tokens = self.cache.match(prompt_tokens)
            need_new = required - len(matched_blocks)
            if not self.bm.can_allocate(need_new):
                return False

        # 到这里才真正“落引用”：共享命中块 + 分配新块
        if matched_blocks:
            self.bm.fork(seq.id, matched_blocks)
            events.append(
                {
                    "type": "PREFIX_HIT",
                    "seq": seq.id,
                    "tokens": matched_tokens,
                    "blocks": list(matched_blocks),
                }
            )
        if need_new > 0:
            new_blocks = self.bm.allocate(seq.id, need_new)
            events.append(
                {
                    "type": "ALLOC",
                    "seq": seq.id,
                    "blocks": list(new_blocks),
                    "count": len(new_blocks),
                }
            )

        # 让 seq.block_table 与 BlockManager 里的表指向同一个 list，
        # 后续分配会直接追加，序列这边自动可见。
        seq.block_table = self.bm.seq_blocks.setdefault(seq.id, [])
        seq.num_computed_tokens = matched_tokens
        seq.prefix_hit_tokens = matched_tokens
        seq.status = SeqStatus.RUNNING
        self.running.append(seq)
        events.append(
            {
                "type": "ADMIT",
                "seq": seq.id,
                "blocks": len(seq.block_table),
                "prefix_hit": matched_tokens,
            }
        )
        return True

    def _ensure_blocks(
        self,
        seq: Sequence,
        target_tokens: int,
        batch: Batch,
        events: list[dict],
    ) -> bool:
        """保证序列算到 target_tokens 时有足够的块。

        不够时先驱逐“只被缓存持有”的块，再抢占 running 序列；
        抢占后 victim 的块可能还挂在缓存上，所以还要再驱逐一次。
        """
        needed_pages = (target_tokens + self.bm.page_size - 1) // self.bm.page_size
        need_new = needed_pages - len(seq.block_table)
        if need_new <= 0:
            return True

        if self.bm.can_allocate(need_new):
            new_blocks = self.bm.allocate(seq.id, need_new)
            events.append(
                {
                    "type": "ALLOC",
                    "seq": seq.id,
                    "blocks": list(new_blocks),
                    "count": len(new_blocks),
                }
            )
            return True

        # 先回收“只被缓存持有”的块：它们没有序列在用，驱逐后立刻回到
        # 空闲池，代价只是以后少一次前缀命中。
        if self._evict_cache(need_new, events):
            self._allocate_new_blocks(seq, need_new, events)
            return True

        # 缓存里没有可回收的块，再挑一个“不在本 batch 里”的 running
        # 序列抢占，避免把正在执行的 batch 弄坏。
        in_batch = {s.id for s in batch.seqs}
        victim = self._pick_victim(exclude=in_batch | {seq.id})
        if victim is not None:
            self._preempt(victim, events)

            # 抢占只是把“序列的引用”还回去；如果 victim 的块同时被前缀
            # 缓存引用，块还不会回到空闲池。所以抢完再驱逐一次缓存，
            # 否则会出现“抢了还是不够、白丢进度”的情况。
            if self._evict_cache(need_new, events):
                self._allocate_new_blocks(seq, need_new, events)
                return True

        return False

    def _allocate_new_blocks(
        self, seq: Sequence, count: int, events: list[dict]
    ) -> None:
        """给序列分配 count 个新块，并记录 ALLOC 事件。"""
        new_blocks = self.bm.allocate(seq.id, count)
        events.append(
            {
                "type": "ALLOC",
                "seq": seq.id,
                "blocks": list(new_blocks),
                "count": len(new_blocks),
            }
        )

    def _evict_cache(self, needed: int, events: list[dict]) -> bool:
        """从 LRU 头部驱逐“只被缓存持有”的块，直到能腾出 needed 个块。

        只驱逐 ref_count == 1 的缓存块：它们没有序列在用，驱逐后立刻回到
        空闲池。被 running 序列持有的缓存块即使被驱逐也不会产生空闲块，
        所以直接跳过，避免“清了缓存却还是不够”的无用功。

        返回 True 表示现在 can_allocate(needed) 成立。
        """
        if not self.use_prefix_cache:
            return self.bm.can_allocate(needed)

        while not self.bm.can_allocate(needed):
            free_before = self.bm.num_free
            block_id = self.cache.evict_lru(evictable_only=True)
            if block_id is None:
                break
            events.append({"type": "EVICT", "block": block_id})
            # 防御性检查：evictable_only 正常一定会释放块；
            # 如果没释放，继续循环只会刷事件，直接停。
            if self.bm.num_free == free_before:
                break

        return self.bm.can_allocate(needed)

    def _pick_victim(self, exclude: set[str]) -> Sequence | None:
        """选优先级最低的 running 序列：这里简单地取“最后进来的”。"""
        for seq in reversed(self.running):
            if seq.id in exclude:
                continue
            if seq.status is SeqStatus.RUNNING:
                return seq
        return None

    def _preempt(self, seq: Sequence, events: list[dict]) -> None:
        """抢占：释放块、重置进度、放回等待队列最前面。"""
        if seq.status is not SeqStatus.RUNNING:
            return

        freed = self.bm.free_seq(seq.id)
        seq.status = SeqStatus.WAITING
        seq.block_table = []
        seq.num_computed_tokens = 0
        seq.num_output_tokens = 0
        seq.ttft_step = None
        seq.preemptions += 1

        if seq in self.running:
            self.running.remove(seq)
        # 放回队首：让它下一步优先被重新调度
        self.waiting.insert(0, seq)
        self._preempted_this_step.add(seq.id)

        events.append(
            {
                "type": "PREEMPT",
                "seq": seq.id,
                "freed_blocks": len(freed),
                "preemptions": seq.preemptions,
            }
        )

    def _cache_prompt_blocks(self, seq: Sequence) -> None:
        """把序列已算完的 prompt 完整页登记进前缀缓存。

        正常路径由 Simulator 每步统一登记；这里额外兜底，覆盖
        “prefill 当步就完成（output_len=1）”的情况——否则 _finish()
        会先清空 block_table，prompt 块就再也没机会进缓存了。
        """
        if not self.use_prefix_cache:
            return

        prompt_computed = min(seq.num_computed_tokens, seq.prompt_len)
        full_pages = prompt_computed // self.bm.page_size
        if full_pages <= 0 or len(seq.block_table) < full_pages:
            return

        self.cache.insert(
            seq.token_ids[: full_pages * self.bm.page_size],
            seq.block_table[:full_pages],
        )

    def _finish(self, seq: Sequence, step: int, events: list[dict]) -> None:
        """序列生成完毕：释放块、标记完成、移出 running。"""
        # 先登记 prompt 完整页，再释放块；正常情况下这些页已经由
        # Simulator 登记过，insert() 会按哈希跳过，不会重复占引用。
        self._cache_prompt_blocks(seq)
        freed = self.bm.free_seq(seq.id)
        seq.status = SeqStatus.FINISHED
        seq.finish_step = step
        seq.block_table = []
        if seq in self.running:
            self.running.remove(seq)
        events.append(
            {
                "type": "FINISH",
                "seq": seq.id,
                "freed_blocks": len(freed),
                "finish_step": step,
            }
        )


__all__ = ["Batch", "PrefillChunk", "Scheduler", "OutOfBlocksError"]

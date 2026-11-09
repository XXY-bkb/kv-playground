"""模拟器主循环：把“请求到达 -> 调度 -> 执行 -> 写回状态”串起来。

一步（step）里发生的事，按顺序是：
1. 到达：把 arrival_step == 当前步的请求放进等待队列；
2. 调度：Scheduler 组 batch（可能伴随 PREFIX_HIT / ALLOC / ADMIT / PREEMPT）；
3. 执行：Scheduler.postprocess 更新 token 数、判完成、释放块；
4. 登记缓存：把已经写满的 prompt 块登记进前缀缓存；
5. 快照：把这一刻的队列、块表、指标拍成 JSON，前端就回放这一帧。

“执行”是抽象掉的：我们不真的跑 Transformer，只按规则推进 token 计数。
所以导出的是“调度轨迹”，不是“模型输出”。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .block_manager import BlockManager
from .model import RequestSpec, SeqStatus, Sequence
from .prefix_cache import PrefixCache
from .scheduler import Batch, Scheduler


@dataclass
class SimConfig:
    """一个场景的全局配置。"""

    page_size: int = 16          # 一页 KV cache 放多少个 token
    num_blocks: int = 64         # 总共有多少页（模拟显存容量）
    max_num_seqs: int = 4        # 同时最多跑几条序列
    max_num_batched_tokens: int = 512  # 一步最多算多少个 prefill token
    prefix_cache: bool = True    # 是否启用前缀缓存

    def __post_init__(self) -> None:
        if self.page_size <= 0:
            raise ValueError("page_size 必须大于 0")
        if self.num_blocks <= 0:
            raise ValueError("num_blocks 必须大于 0")
        if self.max_num_seqs <= 0:
            raise ValueError("max_num_seqs 必须大于 0")
        if self.max_num_batched_tokens <= 0:
            raise ValueError("max_num_batched_tokens 必须大于 0")


class Simulator:
    def __init__(
        self,
        config: SimConfig,
        requests: list[RequestSpec],
        scenario_meta: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.scenario_meta = scenario_meta or {}

        self.bm = BlockManager(config.num_blocks, config.page_size)
        self.cache = PrefixCache(self.bm)
        self.scheduler = Scheduler(
            block_manager=self.bm,
            prefix_cache=self.cache,
            max_num_seqs=config.max_num_seqs,
            max_num_batched_tokens=config.max_num_batched_tokens,
            use_prefix_cache=config.prefix_cache,
        )

        # 把静态请求变成运行时序列，并生成确定性的 token id
        self.sequences: list[Sequence] = []
        for index, spec in enumerate(requests):
            seq = Sequence(
                id=spec.id,
                prompt_len=spec.prompt_len,
                output_len=spec.output_len,
                arrival_step=spec.arrival_step,
                shared_prefix_len=spec.shared_prefix_len,
            )
            seq.token_ids = self._build_token_ids(spec, index)
            self.sequences.append(seq)

        # 还没到达的请求
        self.pending: list[Sequence] = sorted(
            self.sequences, key=lambda s: (s.arrival_step, s.id)
        )

        self.step_index = 0
        self.steps: list[dict[str, Any]] = []
        self.max_steps = 5000
        self.total_generated_tokens = 0

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        """一直跑到所有请求完成，返回完整的 trace 字典。"""
        while not self._is_done():
            if self.step_index >= self.max_steps:
                raise RuntimeError(
                    f"模拟超过 {self.max_steps} 步仍未结束，可能是块不够导致请求一直排队"
                )
            self._run_one_step()

        return self._build_trace()

    # ------------------------------------------------------------------
    # 单步
    # ------------------------------------------------------------------
    def _run_one_step(self) -> None:
        events: list[dict[str, Any]] = []

        # 1) 到达
        arrived = [seq for seq in self.pending if seq.arrival_step == self.step_index]
        for seq in arrived:
            self.pending.remove(seq)
            self.scheduler.add_request(seq)
            events.append(
                {
                    "type": "ARRIVE",
                    "seq": seq.id,
                    "arrival_step": seq.arrival_step,
                }
            )

        # 2) 调度
        batch, schedule_events = self.scheduler.schedule()
        events.extend(schedule_events)

        # 3) 执行（只推进计数，不跑真实模型）
        events.extend(self.scheduler.postprocess(batch, self.step_index))

        # 4) 把新写满的 prompt 块登记进前缀缓存
        self._insert_cache_blocks()

        # 5) 快照
        for event in events:
            event["step"] = self.step_index
        self.bm.validate()
        self.steps.append(self._snapshot(batch, events))
        self.step_index += 1

    # ------------------------------------------------------------------
    # 快照与 trace
    # ------------------------------------------------------------------
    def _snapshot(self, batch: Batch, events: list[dict[str, Any]]) -> dict[str, Any]:
        used_blocks = []
        for block in self.bm.blocks:
            if block.ref_count > 0:
                used_blocks.append(
                    {
                        "id": block.id,
                        "ref": block.ref_count,
                        "hash": block.hash,
                        "cached": block.cached,
                        "owners": self.bm.owners(block.id),
                    }
                )

        # 这一步生成了多少 token：每个 decode +1，每个 prefill 完成时采样首 token
        generated = sum(1 for e in events if e["type"] == "DECODE")
        generated += sum(1 for e in events if e["type"] == "PREFILL_END")
        self.total_generated_tokens += generated

        requests_state: dict[str, Any] = {}
        for seq in self.sequences:
            requests_state[seq.id] = {
                "status": seq.status.value,
                "prompt_len": seq.prompt_len,
                "output_len": seq.output_len,
                "computed": seq.num_computed_tokens,
                "output": seq.num_output_tokens,
                "ttft_step": seq.ttft_step,
                "finish_step": seq.finish_step,
                "blocks": list(seq.block_table),
                "prefix_hit": seq.prefix_hit_tokens,
                "preemptions": seq.preemptions,
            }

        return {
            "step": self.step_index,
            "waiting": [s.id for s in self.scheduler.waiting],
            "running": [s.id for s in self.scheduler.running],
            "batch": {
                "prefill": [
                    {
                        "seq": chunk.seq.id,
                        "start": chunk.start,
                        "end": chunk.end,
                        "tokens": chunk.tokens,
                    }
                    for chunk in batch.prefill
                ],
                "decode": [s.id for s in batch.decode],
            },
            "blocks": {
                "used": used_blocks,
                "free": list(self.bm.free_blocks),
            },
            "block_tables": {
                seq.id: list(seq.block_table)
                for seq in self.sequences
                if seq.block_table
            },
            "requests": requests_state,
            "events": events,
            "metrics": {
                "kv_util": round(self.bm.num_used / self.config.num_blocks, 4),
                "throughput": generated,
                "prefix_hit_tokens": sum(s.prefix_hit_tokens for s in self.sequences),
                "preemptions": sum(s.preemptions for s in self.sequences),
                "finished": sum(1 for s in self.sequences if s.is_finished),
            },
        }

    def _build_trace(self) -> dict[str, Any]:
        peak_util = max((s["metrics"]["kv_util"] for s in self.steps), default=0.0)
        return {
            "schema": "kv-playground/1",
            "scenario": self.scenario_meta,
            "config": asdict(self.config),
            "requests": [
                {
                    "id": seq.id,
                    "prompt_len": seq.prompt_len,
                    "output_len": seq.output_len,
                    "arrival_step": seq.arrival_step,
                    "shared_prefix_len": seq.shared_prefix_len,
                }
                for seq in self.sequences
            ],
            "steps": self.steps,
            "summary": {
                "total_steps": len(self.steps),
                "total_output_tokens": sum(s.output_len for s in self.sequences),
                "generated_steps": self.total_generated_tokens,
                "preemptions": sum(s.preemptions for s in self.sequences),
                "prefix_hit_tokens": sum(s.prefix_hit_tokens for s in self.sequences),
                "peak_kv_util": round(peak_util, 4),
                "finished": sum(1 for s in self.sequences if s.is_finished),
                "total_requests": len(self.sequences),
            },
        }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _insert_cache_blocks(self) -> None:
        """把每个序列已经写满的 prompt 块登记进前缀缓存。

        只缓存 prompt 的完整页：
        - 输出 token 是这条请求独有的，后面的请求不会拿它当前缀；
        - 不满一页的块可能还会被写入，不能共享。
        """
        if not self.config.prefix_cache:
            return

        for seq in self.sequences:
            # 只算 prompt 部分；min 防止输出 token 被误算进来
            prompt_computed = min(seq.num_computed_tokens, seq.prompt_len)
            full_pages = prompt_computed // self.config.page_size
            if full_pages <= 0 or len(seq.block_table) < full_pages:
                continue
            block_ids = seq.block_table[:full_pages]
            tokens = seq.token_ids[: full_pages * self.config.page_size]
            self.cache.insert(tokens, block_ids)

    def _is_done(self) -> bool:
        return (
            not self.pending
            and not self.scheduler.waiting
            and not self.scheduler.running
            and all(seq.is_finished for seq in self.sequences)
        )

    @staticmethod
    def _build_token_ids(spec: RequestSpec, index: int) -> list[int]:
        """生成确定性的假 token id。

        共享前缀的请求，前 shared_prefix_len 个 token 完全相同；
        其余 token 按请求序号区分。这样前缀缓存场景才能真的命中。
        """
        shared = [1000 + i for i in range(spec.shared_prefix_len)]
        body_len = spec.prompt_len - spec.shared_prefix_len
        base = 10_000 + index * 1_000
        body = [base + i for i in range(body_len)]
        return shared + body

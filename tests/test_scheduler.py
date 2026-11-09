"""调度器的单元测试：只测调度规则，不跑完整场景。

这里用很小的配置（8 个块、每块 4 token），方便手算每一步。
"""

import unittest

from sim.block_manager import BlockManager
from sim.model import SeqStatus, Sequence
from sim.prefix_cache import PrefixCache
from sim.scheduler import Scheduler


def make_seq(seq_id: str, prompt_len: int, output_len: int, base: int) -> Sequence:
    """构造一条测试序列，并给它一段确定性的假 token。"""
    seq = Sequence(id=seq_id, prompt_len=prompt_len, output_len=output_len)
    seq.token_ids = [base + i for i in range(prompt_len)]
    return seq


class SchedulerTest(unittest.TestCase):
    def _make_scheduler(self, num_blocks: int, budget: int, max_seqs: int) -> Scheduler:
        bm = BlockManager(num_blocks=num_blocks, page_size=4)
        cache = PrefixCache(bm)
        return Scheduler(
            block_manager=bm,
            prefix_cache=cache,
            max_num_seqs=max_seqs,
            max_num_batched_tokens=budget,
        )

    def test_admit_then_prefill_then_decode(self) -> None:
        sched = self._make_scheduler(num_blocks=8, budget=8, max_seqs=1)
        seq = make_seq("a", prompt_len=8, output_len=2, base=0)
        sched.add_request(seq)

        # 第 1 次 schedule：只做接纳（running 优先，新建的请求下一步才 prefill）
        batch, events = sched.schedule()
        self.assertTrue(batch.is_empty)
        self.assertTrue(any(e["type"] == "ADMIT" for e in events))
        self.assertEqual(seq.status, SeqStatus.RUNNING)
        sched.postprocess(batch, step=0)

        # 第 2 次：prefill 8 个 token
        batch, events = sched.schedule()
        self.assertEqual(len(batch.prefill), 1)
        self.assertEqual(batch.prefill[0].tokens, 8)
        sched.postprocess(batch, step=1)
        self.assertEqual(seq.num_computed_tokens, 8)
        self.assertEqual(seq.num_output_tokens, 1)  # prefill 完采样首 token
        self.assertEqual(seq.ttft_step, 1)

        # 第 3 次：decode 1 个 token，凑满 output_len=2，序列结束
        batch, events = sched.schedule()
        self.assertEqual(len(batch.decode), 1)
        sched.postprocess(batch, step=2)
        self.assertEqual(seq.status, SeqStatus.FINISHED)
        self.assertEqual(seq.finish_step, 2)

    def test_budget_splits_prefill(self) -> None:
        sched = self._make_scheduler(num_blocks=8, budget=4, max_seqs=1)
        seq = make_seq("a", prompt_len=8, output_len=1, base=0)
        sched.add_request(seq)

        sched.schedule()  # 接纳
        batch, _ = sched.schedule()  # 只能算 4 个 token
        self.assertEqual(batch.prefill[0].tokens, 4)
        sched.postprocess(batch, step=1)
        self.assertEqual(seq.num_computed_tokens, 4)
        self.assertEqual(seq.num_output_tokens, 0)

        batch, _ = sched.schedule()  # 再算剩下 4 个
        self.assertEqual(batch.prefill[0].start, 4)
        self.assertEqual(batch.prefill[0].end, 8)
        sched.postprocess(batch, step=2)
        self.assertEqual(seq.num_computed_tokens, 8)
        self.assertEqual(seq.num_output_tokens, 1)
        self.assertEqual(seq.status, SeqStatus.FINISHED)

    def test_preemption_lets_running_finish(self) -> None:
        # 只有 4 个块，两条请求都跑时 decode 必然不够，调度器应该抢占一条
        sched = self._make_scheduler(num_blocks=4, budget=16, max_seqs=2)
        seq_a = make_seq("a", prompt_len=8, output_len=8, base=0)
        seq_b = make_seq("b", prompt_len=8, output_len=8, base=100)
        sched.add_request(seq_a)
        sched.add_request(seq_b)

        saw_preempt = False
        for step in range(200):
            batch, events = sched.schedule()
            events += sched.postprocess(batch, step)
            if any(e["type"] == "PREEMPT" for e in events):
                saw_preempt = True
            if seq_a.is_finished and seq_b.is_finished:
                break

        self.assertTrue(saw_preempt, "块不够时应该出现抢占事件")
        self.assertTrue(seq_a.is_finished)
        self.assertTrue(seq_b.is_finished)
        # 抢占采用重算，所以被抢占的序列最后输出仍然完整
        self.assertEqual(seq_a.num_output_tokens, 8)
        self.assertEqual(seq_b.num_output_tokens, 8)


if __name__ == "__main__":
    unittest.main()

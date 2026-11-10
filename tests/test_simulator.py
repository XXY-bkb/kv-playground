"""模拟器集成测试：跑真实的内置场景，检查不变量和关键行为。

这些测试比单元测试更“端到端”：
- 场景能不能跑完；
- 共享前缀场景有没有真的命中；
- 显存不足场景有没有真的抢占；
- 每一步的 used/free/block_table 是否自洽；
- 同样的输入跑两次结果是否完全一致。
"""

import json
import unittest

from sim.model import RequestSpec
from sim.scenarios import get_scenario
from sim.simulator import SimConfig, Simulator


def run_scenario(scenario_id: str) -> dict:
    scenario = get_scenario(scenario_id)
    sim = Simulator(
        config=scenario["config"],
        requests=scenario["requests"],
        scenario_meta={
            "id": scenario["id"],
            "title": scenario["title"],
            "description": scenario["description"],
            "group": scenario["group"],
        },
    )
    return sim.run()


class SimulatorTest(unittest.TestCase):
    def test_single_request_finishes(self) -> None:
        trace = run_scenario("single")
        self.assertEqual(trace["schema"], "kv-playground/1")
        self.assertEqual(trace["summary"]["finished"], 1)
        self.assertEqual(trace["summary"]["total_requests"], 1)
        self.assertGreater(trace["summary"]["total_steps"], 0)

        last = trace["steps"][-1]
        request = last["requests"]["r1"]
        self.assertEqual(request["status"], "finished")
        self.assertEqual(request["output"], 32)

    def test_deterministic_output(self) -> None:
        first = run_scenario("single")
        second = run_scenario("single")
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )

    def test_shared_prefix_hits_and_no_cache_does_not(self) -> None:
        cached = run_scenario("shared-prefix")
        plain = run_scenario("shared-prefix-no-cache")

        self.assertEqual(cached["summary"]["finished"], 4)
        self.assertEqual(plain["summary"]["finished"], 4)

        # 开缓存时至少有一条请求命中 128 个 token；关缓存时必须为 0
        self.assertGreater(cached["summary"]["prefix_hit_tokens"], 0)
        self.assertEqual(plain["summary"]["prefix_hit_tokens"], 0)

        # 开缓存时显存峰值应该更低（共享块不重复占）
        self.assertLessEqual(
            cached["summary"]["peak_kv_util"],
            plain["summary"]["peak_kv_util"],
        )

    def test_oom_scenario_preempts_and_finishes(self) -> None:
        trace = run_scenario("oom")
        self.assertEqual(trace["summary"]["finished"], 4)
        self.assertGreater(trace["summary"]["preemptions"], 0)

        # 抢占采用重算，最终每条请求的输出仍然完整
        last = trace["steps"][-1]
        for req in trace["requests"]:
            state = last["requests"][req["id"]]
            self.assertEqual(state["status"], "finished")
            self.assertEqual(state["output"], req["output_len"])

    def test_step_invariants(self) -> None:
        trace = run_scenario("shared-prefix")
        num_blocks = trace["config"]["num_blocks"]

        for step in trace["steps"]:
            used = {block["id"] for block in step["blocks"]["used"]}
            free = set(step["blocks"]["free"])

            # 1) 已用 + 空闲 = 总块数，且没有交集
            self.assertEqual(len(used | free), num_blocks)
            self.assertEqual(len(used & free), 0)

            # 2) 每个序列的块表只能引用已用块
            for seq_id, table in step["block_tables"].items():
                self.assertTrue(
                    set(table).issubset(used),
                    f"step {step['step']}: {seq_id} 的块表引用了未用块",
                )

            # 3) 引用计数至少等于序列拥有者数量（缓存会额外加 1）
            for block in step["blocks"]["used"]:
                self.assertGreaterEqual(block["ref"], len(block["owners"]))

    def test_evicts_unpinned_cache_before_preempting(self) -> None:
        # 6 个块：r1/r2 先跑完，prompt 块留在缓存里（只被缓存持有）；
        # r3/r4 后到，块不够时应该先回收这些缓存块，而不是抢占 r4。
        config = SimConfig(
            page_size=4,
            num_blocks=6,
            max_num_seqs=4,
            max_num_batched_tokens=512,
            prefix_cache=True,
        )
        requests = [
            RequestSpec("r1", 4, 3, 0),
            RequestSpec("r2", 4, 3, 0),
            RequestSpec("r3", 8, 8, 4),
            RequestSpec("r4", 8, 8, 4),
        ]
        trace = Simulator(config, requests).run()

        step6 = trace["steps"][6]
        event_types = [event["type"] for event in step6["events"]]
        self.assertIn("EVICT", event_types)
        self.assertNotIn("PREEMPT", event_types)
        self.assertEqual(step6["requests"]["r4"]["preemptions"], 0)

    def test_single_token_output_still_enters_prefix_cache(self) -> None:
        # output_len=1 的请求在 prefill 当步就结束；它的 prompt 块仍应
        # 留在前缀缓存里，供后面的相同前缀请求命中。
        config = SimConfig(
            page_size=4,
            num_blocks=8,
            max_num_seqs=2,
            max_num_batched_tokens=512,
            prefix_cache=True,
        )
        requests = [
            RequestSpec("r1", 4, 1, 0, shared_prefix_len=4),
            RequestSpec("r2", 4, 1, 2, shared_prefix_len=4),
        ]
        trace = Simulator(config, requests).run()
        self.assertEqual(trace["summary"]["prefix_hit_tokens"], 4)


if __name__ == "__main__":
    unittest.main()

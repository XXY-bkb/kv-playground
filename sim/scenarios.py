"""内置场景：每个场景就是一组配置 + 一组请求。

前端会把每个场景导出成一个 JSON 文件，按 `group` 分组展示。
同一组里通常有一个“开/关”对照，方便看出某个机制到底省了什么：
- 并发组：连续批处理 vs 一次只跑一条（静态批处理）；
- 前缀缓存组：开 vs 关；
- 显存组：块不够时会发生什么。
"""

from __future__ import annotations

from typing import Any

from .model import RequestSpec
from .simulator import SimConfig


# 每个场景的字段：
# id：文件名与前端 key
# title/description：展示文案
# group：前端分组
# config：SimConfig
# requests：RequestSpec 列表
SCENARIO_DEFS: list[dict[str, Any]] = [
    {
        "id": "single",
        "title": "单请求：先看懂块表",
        "description": (
            "一条 128 token 的 prompt、生成 32 个 token。观察 block table 如何随着 "
            "prefill/decode 一段段变长，以及请求结束后块如何回到空闲池。"
        ),
        "group": "基础",
        "config": SimConfig(
            page_size=16,
            num_blocks=64,
            max_num_seqs=1,
            max_num_batched_tokens=512,
        ),
        "requests": [
            RequestSpec(
                id="r1",
                prompt_len=128,
                output_len=32,
                arrival_step=0,
            )
        ],
    },
    {
        "id": "concurrent",
        "title": "连续批处理：让 decode 槽位不空转",
        "description": (
            "6 条请求错峰到达，同时最多跑 3 条。对比单条串行，观察 decode 阶段如何被"
            "新到达的 prefill 填满，以及 waiting/running 队列怎么变化。"
        ),
        "group": "并发",
        "config": SimConfig(
            page_size=16,
            num_blocks=64,
            max_num_seqs=3,
            max_num_batched_tokens=512,
        ),
        "requests": [
            RequestSpec("r1", 96, 24, 0),
            RequestSpec("r2", 128, 32, 0),
            RequestSpec("r3", 160, 40, 1),
            RequestSpec("r4", 112, 28, 1),
            RequestSpec("r5", 144, 36, 2),
            RequestSpec("r6", 96, 24, 2),
        ],
    },
    {
        "id": "concurrent-static",
        "title": "对照：一次只跑一条",
        "description": (
            "同样的 6 条请求，把并发槽位限制为 1。总步数明显变长，但显存峰值更低——"
            "这就是“吞吐 vs 显存”的直观取舍。"
        ),
        "group": "并发",
        "config": SimConfig(
            page_size=16,
            num_blocks=64,
            max_num_seqs=1,
            max_num_batched_tokens=512,
        ),
        "requests": [
            RequestSpec("r1", 96, 24, 0),
            RequestSpec("r2", 128, 32, 0),
            RequestSpec("r3", 160, 40, 1),
            RequestSpec("r4", 112, 28, 1),
            RequestSpec("r5", 144, 36, 2),
            RequestSpec("r6", 96, 24, 2),
        ],
    },
    {
        "id": "shared-prefix",
        "title": "共享 system prompt：前缀缓存命中",
        "description": (
            "4 条请求的前 128 个 token 完全相同（模拟同一个 system prompt）。"
            "打开前缀缓存后，后面的请求会直接共享这些块，PREFIX_HIT 事件会标出命中了多少 token。"
        ),
        "group": "前缀缓存",
        "config": SimConfig(
            page_size=16,
            num_blocks=64,
            max_num_seqs=4,
            max_num_batched_tokens=512,
            prefix_cache=True,
        ),
        "requests": [
            # 第一条请求要先完成 prefill，前缀块才会进入缓存；
            # 所以后面的请求错开 2 步到达，避免“同一调度步抢跑”造成假的不命中。
            RequestSpec("r1", 160, 32, 0, shared_prefix_len=128),
            RequestSpec("r2", 160, 32, 2, shared_prefix_len=128),
            RequestSpec("r3", 160, 32, 3, shared_prefix_len=128),
            RequestSpec("r4", 160, 32, 4, shared_prefix_len=128),
        ],
    },
    {
        "id": "shared-prefix-no-cache",
        "title": "对照：关掉前缀缓存",
        "description": (
            "同样的 4 条共享前缀请求，但每次 prefill 都重新分配块。"
            "对比命中 token、显存峰值和总步数，就能看出前缀缓存省了什么。"
        ),
        "group": "前缀缓存",
        "config": SimConfig(
            page_size=16,
            num_blocks=64,
            max_num_seqs=4,
            max_num_batched_tokens=512,
            prefix_cache=False,
        ),
        "requests": [
            # 第一条请求要先完成 prefill，前缀块才会进入缓存；
            # 所以后面的请求错开 2 步到达，避免“同一调度步抢跑”造成假的不命中。
            RequestSpec("r1", 160, 32, 0, shared_prefix_len=128),
            RequestSpec("r2", 160, 32, 2, shared_prefix_len=128),
            RequestSpec("r3", 160, 32, 3, shared_prefix_len=128),
            RequestSpec("r4", 160, 32, 4, shared_prefix_len=128),
        ],
    },
    {
        "id": "oom",
        "title": "显存不足：抢占与重算",
        "description": (
            "总块数只有 24，4 条请求每条约 192 个 prompt token + 32 个输出 token。"
            "块不够时调度器会抢占一条 running 序列、释放它的块，让它之后从 0 重新 prefill；"
            "观察 PREEMPT 事件和重复的 prefill，以及最终输出仍然完整。"
        ),
        "group": "显存不足",
        "config": SimConfig(
            page_size=16,
            num_blocks=24,
            max_num_seqs=4,
            max_num_batched_tokens=512,
            prefix_cache=True,
        ),
        "requests": [
            RequestSpec("r1", 192, 32, 0),
            RequestSpec("r2", 192, 32, 0),
            RequestSpec("r3", 192, 32, 1),
            RequestSpec("r4", 192, 32, 1),
        ],
    },
]


SCENARIO_BY_ID: dict[str, dict[str, Any]] = {item["id"]: item for item in SCENARIO_DEFS}


def get_scenario(scenario_id: str) -> dict[str, Any]:
    """按 id 取场景；找不到就给出清晰错误，避免导出时静默生成空文件。"""
    try:
        return SCENARIO_BY_ID[scenario_id]
    except KeyError as exc:
        available = ", ".join(sorted(SCENARIO_BY_ID))
        raise KeyError(f"未知场景 {scenario_id!r}；可用：{available}") from exc

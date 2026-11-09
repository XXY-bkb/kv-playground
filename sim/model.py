"""最基础的数据模型：请求、运行时序列、物理块。

读代码时先记住三个词：
- 请求（RequestSpec）：用户视角的“我要生成一段文本”，只有长度，没有运行时状态。
- 序列（Sequence）：引擎视角的“一条正在跑的请求”，带状态、块表、已算 token 数。
- 块（Block）：显存里固定大小的一页 KV cache，通常一页放 page_size 个 token。

一个序列的 KV cache 不是一整块连续显存，而是一张“块表”（block_table）：
    block_table = [7, 12, 3, ...]
第 1 页 token 的 KV 在 7 号块，第 2 页在 12 号块……这就是 PagedAttention 的核心。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SeqStatus(str, Enum):
    """序列状态机：等待 -> 运行 -> 结束。

    继承 str 是为了导出 JSON 时直接变成 "waiting"/"running"/"finished"，
    前端不用再做一层映射。
    """

    WAITING = "waiting"
    RUNNING = "running"
    FINISHED = "finished"


@dataclass
class RequestSpec:
    """场景文件里的一个请求（静态描述，不含运行时状态）。"""

    id: str
    prompt_len: int          # 输入（prefill）有多少个 token
    output_len: int          # 希望生成多少个 token
    arrival_step: int = 0    # 第几步到达；用来模拟“错峰请求”
    shared_prefix_len: int = 0  # 与其他请求共享的前缀长度（system prompt 场景）

    def __post_init__(self) -> None:
        # 这些校验看起来啰嗦，但可以避免场景配置写错后在模拟器深处才报错。
        if not self.id:
            raise ValueError("请求 id 不能为空")
        if self.prompt_len <= 0:
            raise ValueError(f"{self.id}: prompt_len 必须大于 0")
        if self.output_len <= 0:
            raise ValueError(f"{self.id}: output_len 必须大于 0")
        if self.arrival_step < 0:
            raise ValueError(f"{self.id}: arrival_step 不能为负")
        if not 0 <= self.shared_prefix_len <= self.prompt_len:
            raise ValueError(f"{self.id}: shared_prefix_len 必须在 [0, prompt_len] 内")


@dataclass
class Block:
    """一个物理 KV cache 块。

    字段含义：
    - ref_count：有多少个“使用者”引用它。序列自己算 1 个；如果这个块被
      前缀缓存登记了，缓存再算 1 个。ref_count 归零才能放回空闲池。
    - hash：这个块对应前缀链哈希；没进缓存时为 None。
    - cached：是否已被前缀缓存登记。
    - full：是否已经装满 page_size 个 token；只有装满的块才允许进缓存。
    """

    id: int
    ref_count: int = 0
    hash: str | None = None
    cached: bool = False
    full: bool = False


@dataclass
class Sequence:
    """运行时的一条序列。

    注意 num_computed_tokens 和 num_output_tokens 的区别：
    - num_computed_tokens：已经写进 KV cache 的 token 数（prompt + 已算的输出）；
    - num_output_tokens：已经“采样出来”的输出 token 数。
    采样出第 1 个输出 token 时，它的 KV 还没写进去；下一次 decode 才写。
    所以 prefill 结束时 num_computed_tokens == prompt_len，而 num_output_tokens == 1。
    """

    id: str
    prompt_len: int
    output_len: int
    arrival_step: int = 0
    shared_prefix_len: int = 0

    # 这个请求的 token id 列表（只用于前缀哈希，不需要真实文本）
    token_ids: list[int] = field(default_factory=list)

    status: SeqStatus = SeqStatus.WAITING
    block_table: list[int] = field(default_factory=list)

    num_computed_tokens: int = 0
    num_output_tokens: int = 0

    ttft_step: int | None = None       # 首 token 在第几步产生
    finish_step: int | None = None     # 在第几步结束
    preemptions: int = 0               # 被抢占次数
    prefix_hit_tokens: int = 0         # 命中的前缀 token 数

    @property
    def total_tokens(self) -> int:
        """prompt + 期望输出，仅用于展示。"""
        return self.prompt_len + self.output_len

    @property
    def is_finished(self) -> bool:
        return self.status is SeqStatus.FINISHED

    def required_blocks(self, page_size: int) -> int:
        """算到当前 num_computed_tokens 为止，一共需要多少页。"""
        return (self.num_computed_tokens + page_size - 1) // page_size

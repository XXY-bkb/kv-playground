"""前缀缓存：用“前缀链哈希”把相同前缀的块找出来复用。

为什么不能只对单个块做哈希？
假设两个请求的某一块 token 恰好一样，但前面的 token 不同，那么它们的 KV
其实完全不同（Transformer 是因果的，KV 依赖前面所有 token）。只按单块哈希
会错误命中，输出就错了。

正确做法是把“前面所有块的哈希 + 当前块 token”一起哈希，形成一条链：
    h0 = H(page0)
    h1 = H(h0 + page1)
    h2 = H(h1 + page2)
只有整条链都相同，前缀才真的相同。这也是 vLLM 前缀缓存的基本做法。
"""

from __future__ import annotations

import hashlib
import struct
from collections import OrderedDict

from .block_manager import BlockManager


class PrefixCache:
    def __init__(self, block_manager: BlockManager) -> None:
        self.bm = block_manager
        self.page_size = block_manager.page_size

        # 哈希 -> 块 id
        self.hash_to_block: dict[str, int] = {}

        # 块 id -> 这个块装的具体 token（用来防止哈希碰撞导致错误命中）
        self.block_tokens: dict[int, tuple[int, ...]] = {}

        # 简单的 LRU：最近命中的移到末尾，驱逐时从头部拿
        self.lru: OrderedDict[int, None] = OrderedDict()

    # ------------------------------------------------------------------
    # 哈希
    # ------------------------------------------------------------------
    @staticmethod
    def _page_hash(prev_hash: str, page: tuple[int, ...]) -> str:
        """计算“前缀链哈希”。

        prev_hash 是前一块的哈希（第一块传空串），page 是当前块的 token。
        用 blake2b 截断到 8 字节：足够短，碰撞概率对演示来说可以忽略；
        测试里会故意制造碰撞，验证我们不会错误命中。
        """
        payload = prev_hash.encode("ascii")
        # 每个 token 固定编码成 4 字节小端整数，保证跨平台结果一致
        payload += b"".join(struct.pack("<I", token & 0xFFFFFFFF) for token in page)
        return hashlib.blake2b(payload, digest_size=8).hexdigest()

    # ------------------------------------------------------------------
    # 查询命中
    # ------------------------------------------------------------------
    def match(self, token_ids: list[int]) -> tuple[list[int], int]:
        """返回 (命中的块 id 列表, 命中的 token 数)。

        只匹配“完整的页”。最后不满一页的 token 不参与匹配，
        因为块里可能还有别的请求没写完的数据，不能共享。
        """
        matched_blocks: list[int] = []
        matched_tokens = 0
        prev_hash = ""

        # range 的终点是“完整页”的边界，例如 35 个 token、page=16 -> 只匹配 32 个
        full_pages = len(token_ids) // self.page_size
        for page_index in range(full_pages):
            start = page_index * self.page_size
            page = tuple(token_ids[start:start + self.page_size])
            page_hash = self._page_hash(prev_hash, page)

            block_id = self.hash_to_block.get(page_hash)
            if block_id is None:
                # 链断了，后面的块即使单独相同也不能复用
                break

            # 防哈希碰撞：哈希一样但 token 不一样，保守地当作未命中
            if self.block_tokens.get(block_id) != page:
                break

            matched_blocks.append(block_id)
            matched_tokens += self.page_size
            prev_hash = page_hash
            # 命中即刷新 LRU，避免它被优先驱逐
            self.lru.move_to_end(block_id)

        return matched_blocks, matched_tokens

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def insert(self, token_ids: list[int], block_ids: list[int]) -> None:
        """把已经写满的块登记进缓存。

        调用方保证 block_ids 与 token_ids 的前 len(block_ids)*page_size 个 token 对应。
        """
        prev_hash = ""
        for page_index, block_id in enumerate(block_ids):
            start = page_index * self.page_size
            page = tuple(token_ids[start:start + self.page_size])
            if len(page) < self.page_size:
                break  # 不满一页，不缓存

            page_hash = self._page_hash(prev_hash, page)
            if page_hash in self.hash_to_block:
                # 已经有一份相同前缀的块了，直接复用旧登记，不重复占缓存引用
                prev_hash = page_hash
                continue

            self.hash_to_block[page_hash] = block_id
            self.block_tokens[block_id] = page
            self.lru[block_id] = None
            self.bm.add_cache_ref(block_id, page_hash)
            prev_hash = page_hash

    # ------------------------------------------------------------------
    # 驱逐
    # ------------------------------------------------------------------
    def evict_lru(self) -> int | None:
        """驱逐最久未用的缓存块；没有可驱逐的返回 None。

        注意：如果这个块还被某个序列持有，它不会被放回空闲池，
        只是“不再作为缓存被别人命中”，序列仍能正常使用。
        """
        while self.lru:
            block_id, _ = self.lru.popitem(last=False)
            block = self.bm.blocks[block_id]
            old_hash = block.hash
            if old_hash is not None:
                self.hash_to_block.pop(old_hash, None)
            self.block_tokens.pop(block_id, None)
            self.bm.release_cache_ref(block_id)
            return block_id
        return None

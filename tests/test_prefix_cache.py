"""前缀缓存的单元测试。

重点验证三件事：
1. 完整页才能命中，最后不满一页的 token 不共享；
2. 链式哈希：第一页相同、第二页不同，只能命中第一页；
3. 哈希碰撞不会导致错误命中（我们用 block_tokens 再核对一次）。
"""

import unittest

from sim.block_manager import BlockManager
from sim.prefix_cache import PrefixCache


class PrefixCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        # 16 个块、每块 4 个 token
        self.bm = BlockManager(num_blocks=16, page_size=4)
        self.cache = PrefixCache(self.bm)

    def _insert_tokens(self, seq_id: str, tokens: list[int]) -> list[int]:
        """给某个序列分配足够的块，并把完整页登记进缓存。"""
        pages = len(tokens) // self.bm.page_size
        blocks = self.bm.allocate(seq_id, pages)
        self.cache.insert(tokens, blocks)
        return blocks

    def test_match_after_insert(self) -> None:
        tokens = list(range(16))  # 4 页
        blocks = self._insert_tokens("a", tokens)

        matched_blocks, matched_tokens = self.cache.match(tokens + [99])
        self.assertEqual(matched_blocks, blocks)
        self.assertEqual(matched_tokens, 16)

        # 只查前两页，也应该命中两页
        matched_blocks, matched_tokens = self.cache.match(tokens[:8])
        self.assertEqual(matched_tokens, 8)
        self.assertEqual(len(matched_blocks), 2)

    def test_prefix_chain_prevents_false_hit(self) -> None:
        tokens_a = list(range(8))          # 第 1 页 0..3，第 2 页 4..7
        tokens_b = list(range(4)) + [99, 98, 97, 96]  # 第 1 页相同，第 2 页不同

        self._insert_tokens("a", tokens_a)

        # 只能命中第 1 页；第 2 页虽然“长度一样”，但 token 不同
        matched_blocks, matched_tokens = self.cache.match(tokens_b)
        self.assertEqual(matched_tokens, 4)
        self.assertEqual(len(matched_blocks), 1)

    def test_partial_page_is_not_cached(self) -> None:
        tokens = list(range(6))  # 只有 1 个完整页 + 2 个 token
        blocks = self.bm.allocate("a", 2)
        self.cache.insert(tokens, blocks)

        matched_blocks, matched_tokens = self.cache.match(tokens)
        self.assertEqual(matched_tokens, 4)
        self.assertEqual(len(matched_blocks), 1)

    def test_hash_collision_is_rejected(self) -> None:
        # 故意让所有页返回同一个哈希，模拟碰撞
        self.cache._page_hash = lambda prev, page: "same-hash"  # type: ignore[assignment]

        tokens_a = list(range(4))
        tokens_b = [99, 98, 97, 96]
        blocks_a = self.bm.allocate("a", 1)
        self.cache.insert(tokens_a, blocks_a)

        blocks_b = self.bm.allocate("b", 1)
        self.cache.insert(tokens_b, blocks_b)

        # 哈希相同但 token 不同，必须当作未命中
        matched_blocks, matched_tokens = self.cache.match(tokens_b)
        self.assertEqual(matched_blocks, [])
        self.assertEqual(matched_tokens, 0)

    def test_evict_releases_cache_reference(self) -> None:
        tokens = list(range(8))
        blocks = self._insert_tokens("a", tokens)

        # 序列结束后，缓存仍持有块，所以块没有回到空闲池
        self.bm.free_seq("a")
        self.assertEqual(self.bm.blocks[blocks[0]].ref_count, 1)
        self.assertEqual(self.bm.num_free, 14)

        # 命中一次，确认缓存还在
        matched_blocks, matched_tokens = self.cache.match(tokens)
        self.assertEqual(matched_tokens, 8)

        # 驱逐两个块后，它们才回到空闲池
        self.assertIsNotNone(self.cache.evict_lru())
        self.assertIsNotNone(self.cache.evict_lru())
        self.assertEqual(self.bm.num_free, 16)

        # 全部驱逐后不再命中
        matched_blocks, matched_tokens = self.cache.match(tokens)
        self.assertEqual(matched_tokens, 0)


if __name__ == "__main__":
    unittest.main()

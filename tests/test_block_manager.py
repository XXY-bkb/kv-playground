"""BlockManager 的单元测试。

这些测试只关心“显存分配器”的不变量，不涉及调度策略。
如果这里挂了，后面的模拟结果都不可信。
"""

import unittest

from sim.block_manager import BlockManager, OutOfBlocksError


class BlockManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        # 8 个块、每块 4 个 token，数字小，方便手算
        self.bm = BlockManager(num_blocks=8, page_size=4)

    def test_allocate_and_free(self) -> None:
        ids = self.bm.allocate("a", 3)
        self.assertEqual(ids, [0, 1, 2])
        self.assertEqual(self.bm.num_free, 5)
        self.assertEqual(self.bm.blocks[0].ref_count, 1)

        freed = self.bm.free_seq("a")
        self.assertEqual(sorted(freed), [0, 1, 2])
        self.assertEqual(self.bm.num_free, 8)
        self.bm.validate()

    def test_allocate_more_than_capacity_raises(self) -> None:
        with self.assertRaises(OutOfBlocksError):
            self.bm.allocate("a", 9)

    def test_fork_shares_blocks_without_copy(self) -> None:
        ids = self.bm.allocate("a", 2)
        self.bm.fork("b", ids)

        # 两个序列共享同一组块，引用计数应该是 2
        self.assertEqual(self.bm.blocks[ids[0]].ref_count, 2)
        self.assertEqual(self.bm.owners(ids[0]), ["a", "b"])

        # a 释放后，块还被 b 引用，不能回到空闲池
        self.bm.free_seq("a")
        self.assertEqual(self.bm.blocks[ids[0]].ref_count, 1)
        self.assertNotIn(ids[0], self.bm.free_blocks)

        # b 也释放后，块才真正空闲
        self.bm.free_seq("b")
        self.assertEqual(self.bm.num_free, 8)
        self.bm.validate()

    def test_cache_reference_keeps_block_alive(self) -> None:
        ids = self.bm.allocate("a", 1)
        block_id = ids[0]

        # 缓存登记后，引用计数从 1 变成 2（序列 + 缓存）
        self.bm.add_cache_ref(block_id, "hash-1")
        self.assertEqual(self.bm.blocks[block_id].ref_count, 2)

        # 序列结束只减自己那一份，块仍然留在缓存里
        self.bm.free_seq("a")
        self.assertEqual(self.bm.blocks[block_id].ref_count, 1)
        self.assertNotIn(block_id, self.bm.free_blocks)

        # 缓存驱逐后，引用归零，块才回空闲池
        self.bm.release_cache_ref(block_id)
        self.assertEqual(self.bm.num_free, 8)
        self.bm.validate()

    def test_double_free_is_detected(self) -> None:
        ids = self.bm.allocate("a", 1)
        self.bm.free_seq("a")
        # 再释放一次应该触发断言，而不是悄悄把空闲池弄乱
        with self.assertRaises(AssertionError):
            self.bm._release(ids[0])

    def test_validate_detects_dirty_free_pool(self) -> None:
        self.bm.allocate("a", 1)
        # 人为把“还被引用的块”塞回空闲池，validate 应该立刻发现
        self.bm.free_blocks.append(0)
        with self.assertRaises(AssertionError):
            self.bm.validate()

    def test_validate_detects_leaked_block(self) -> None:
        self.bm.allocate("a", 1)
        self.bm.free_seq("a")
        # 人为把已经回到空闲池的块“弄丢”，模拟块泄漏：
        # 块既不在空闲池，也没有任何引用，validate 必须报错。
        self.bm.free_blocks.remove(0)
        with self.assertRaises(AssertionError):
            self.bm.validate()


if __name__ == "__main__":
    unittest.main()

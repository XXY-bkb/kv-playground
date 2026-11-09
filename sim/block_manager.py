"""物理块池：负责分配、共享、释放，以及引用计数。

这是整个模拟器最像“显存分配器”的部分。核心不变量（测试会逐条检查）：
1. 空闲池里的块，ref_count 一定是 0；
2. 一个块要么在空闲池，要么至少被一个序列/缓存引用；
3. 序列释放块时只减引用计数，计数归零才真正回到空闲池；
4. 共享块（前缀命中）只增加引用计数，不复制数据。
"""

from __future__ import annotations

from .model import Block


class OutOfBlocksError(RuntimeError):
    """空闲块不够时抛出；上层会把它转成 OOM/抢占事件。"""


class BlockManager:
    def __init__(self, num_blocks: int, page_size: int = 16) -> None:
        if num_blocks <= 0:
            raise ValueError("num_blocks 必须大于 0")
        if page_size <= 0:
            raise ValueError("page_size 必须大于 0")

        self.num_blocks = num_blocks
        self.page_size = page_size

        # 用普通 list 当空闲池：pop(0) 取最久未用的块，顺序固定，
        # 保证同一个场景每次跑出来的 trace 完全一样。
        self.free_blocks: list[int] = list(range(num_blocks))

        # 所有块的元数据，下标就是块 id
        self.blocks: list[Block] = [Block(id=i) for i in range(num_blocks)]

        # 每个序列持有/共享了哪些块。注意：同一个块可能出现在多个序列的表里，
        # 这正是“共享前缀”省显存的来源。
        self.seq_blocks: dict[str, list[int]] = {}

    # ------------------------------------------------------------------
    # 查询类方法
    # ------------------------------------------------------------------
    @property
    def num_free(self) -> int:
        return len(self.free_blocks)

    @property
    def num_used(self) -> int:
        return self.num_blocks - len(self.free_blocks)

    def can_allocate(self, n: int) -> bool:
        return len(self.free_blocks) >= n

    def owners(self, block_id: int) -> list[str]:
        """返回当前引用这个块的序列 id（按字母序，方便稳定展示）。"""
        return sorted(
            seq_id
            for seq_id, block_ids in self.seq_blocks.items()
            if block_id in block_ids
        )

    # ------------------------------------------------------------------
    # 分配 / 共享 / 释放
    # ------------------------------------------------------------------
    def allocate(self, seq_id: str, n: int) -> list[int]:
        """给序列新分配 n 个块，返回块 id 列表。

        新块的 ref_count 从 0 变成 1（序列自己持有一份引用）。
        """
        if n < 0:
            raise ValueError("分配块数不能为负")
        if not self.can_allocate(n):
            raise OutOfBlocksError(
                f"需要 {n} 个块，但只剩 {len(self.free_blocks)} 个"
            )

        new_ids: list[int] = []
        for _ in range(n):
            block_id = self.free_blocks.pop(0)
            block = self.blocks[block_id]
            # 防御性检查：空闲池里不应该有被别人引用的块
            if block.ref_count != 0:
                raise AssertionError(f"块 {block_id} 在空闲池里但 ref_count={block.ref_count}")
            block.ref_count = 1
            block.hash = None
            block.cached = False
            block.full = False
            new_ids.append(block_id)

        self.seq_blocks.setdefault(seq_id, []).extend(new_ids)
        return new_ids

    def fork(self, seq_id: str, block_ids: list[int]) -> None:
        """让序列共享一组已经存在的块（前缀缓存命中时用）。

        共享只是把 ref_count 加 1，不复制任何数据；这就是“相同前缀只存一份”。
        """
        for block_id in block_ids:
            block = self.blocks[block_id]
            if block.ref_count <= 0:
                raise AssertionError(f"不能共享未分配的块 {block_id}")
            block.ref_count += 1
        self.seq_blocks.setdefault(seq_id, []).extend(block_ids)

    def free_seq(self, seq_id: str) -> list[int]:
        """释放序列持有的所有块，返回真正回到空闲池的块 id。"""
        block_ids = self.seq_blocks.pop(seq_id, [])
        freed: list[int] = []
        for block_id in block_ids:
            if self._release(block_id):
                freed.append(block_id)
        return freed

    def _release(self, block_id: int) -> bool:
        """把某个块的引用计数减 1；归零则放回空闲池。

        返回值：是否真正回到了空闲池。
        """
        block = self.blocks[block_id]
        block.ref_count -= 1
        if block.ref_count < 0:
            raise AssertionError(f"块 {block_id} 的引用计数变成负数，说明重复释放了")

        if block.ref_count == 0:
            # 没有任何序列/缓存再引用它，清空元数据并放回池子
            block.hash = None
            block.cached = False
            block.full = False
            self.free_blocks.append(block_id)
            return True
        return False

    # ------------------------------------------------------------------
    # 前缀缓存专用：缓存自己也算一个引用者
    # ------------------------------------------------------------------
    def add_cache_ref(self, block_id: int, prefix_hash: str) -> None:
        """把块登记进前缀缓存：缓存持有一份引用，块标记为 cached/full。

        这样即使创建它的序列结束了，块仍然留在缓存里，后续相同前缀可以直接命中。
        """
        block = self.blocks[block_id]
        if block.ref_count <= 0:
            raise AssertionError(f"不能缓存未分配的块 {block_id}")
        if block.cached:
            # 已经缓存过就不重复加引用，避免计数虚高
            return
        block.ref_count += 1
        block.cached = True
        block.hash = prefix_hash
        block.full = True

    def release_cache_ref(self, block_id: int) -> None:
        """缓存驱逐时调用：只减缓存那一份引用，序列的引用不受影响。"""
        block = self.blocks[block_id]
        if not block.cached:
            return
        block.cached = False
        block.hash = None
        block.full = False
        self._release(block_id)

    # ------------------------------------------------------------------
    # 自检：测试和导出前都可以调用，尽早发现不变量被破坏
    # ------------------------------------------------------------------
    def validate(self) -> None:
        if len(set(self.free_blocks)) != len(self.free_blocks):
            raise AssertionError("空闲池里出现重复块")
        if len(self.free_blocks) + self.num_used != self.num_blocks:
            raise AssertionError("空闲块数 + 已用块数 != 总块数")

        for block_id in self.free_blocks:
            if self.blocks[block_id].ref_count != 0:
                raise AssertionError(f"空闲块 {block_id} 的 ref_count 不为 0")

        for seq_id, block_ids in self.seq_blocks.items():
            if len(set(block_ids)) != len(block_ids):
                raise AssertionError(f"序列 {seq_id} 的块表里有重复块")
            for block_id in block_ids:
                if self.blocks[block_id].ref_count <= 0:
                    raise AssertionError(f"序列 {seq_id} 引用了未分配的块 {block_id}")

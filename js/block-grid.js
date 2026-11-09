/**
 * 物理块池视图。
 *
 * 一个格子 = 一个物理 KV block（默认 16 token）。
 * 颜色 = 持有它的请求；多个请求共享时用斜向双色渐变；
 * 灰色虚线斜纹 = 只被前缀缓存引用、没有序列在用的块。
 *
 * 这里只负责“把某一步的 blocks 快照画出来”，不计算任何调度逻辑。
 */

import { PALETTE, create } from "./util.js";

export function createBlockGrid(container, detailEl, trace) {
  const numBlocks = trace.config.num_blocks;

  // 请求 -> 颜色。按 trace.requests 的顺序分配，保证同一场景内颜色稳定。
  const colorById = new Map();
  trace.requests.forEach((request, index) => {
    colorById.set(request.id, PALETTE[index % PALETTE.length]);
  });

  container.replaceChildren();
  const cells = [];

  for (let id = 0; id < numBlocks; id += 1) {
    const cell = create("div", "block free", String(id));
    cell.dataset.id = String(id);
    container.appendChild(cell);
    cells.push(cell);
  }

  let snapshot = null;
  let usedById = new Map();

  /** 刷新块池。返回本步的统计，方便右侧指标卡复用。 */
  function update(stepSnapshot) {
    snapshot = stepSnapshot;
    usedById = new Map(stepSnapshot.blocks.used.map((block) => [block.id, block]));

    let cachedCount = 0;
    let sharedCount = 0;

    for (let id = 0; id < numBlocks; id += 1) {
      const cell = cells[id];
      const block = usedById.get(id);

      // 每次先重置，避免上一步的类名/内联颜色残留。
      cell.className = "block";
      cell.style.background = "";
      cell.style.borderColor = "";
      cell.textContent = String(id);

      if (!block) {
        cell.classList.add("free");
        cell.title = `块 #${id}：空闲`;
        continue;
      }

      if (block.cached) cachedCount += 1;

      const owners = block.owners || [];
      if (owners.length === 0) {
        // 只被前缀缓存引用：序列已经结束，但块还留着供后续请求复用。
        cell.classList.add("cache-only");
        cell.title = `块 #${id}：只被前缀缓存引用（ref ${block.ref}）`;
        continue;
      }

      cell.classList.add("used");
      if (block.cached) cell.classList.add("cached");
      if (owners.length > 1) {
        cell.classList.add("shared");
        sharedCount += 1;
      }

      const colors = owners.map((owner) => colorById.get(owner) || "#94a3b8");
      if (owners.length === 1) {
        cell.style.background = colors[0];
      } else {
        // 两个请求共享：左上到右下的双色；超过两个时再给一圈白边提示。
        cell.style.background = `linear-gradient(135deg, ${colors[0]} 0 50%, ${colors[1]} 50% 100%)`;
        if (owners.length > 2) cell.style.borderColor = "#f8fafc";
      }

      cell.title =
        `块 #${id}\n` +
        `持有者: ${owners.join(", ")}\n` +
        `引用计数: ${block.ref}\n` +
        `已进前缀缓存: ${block.cached ? "是" : "否"}\n` +
        `前缀哈希: ${block.hash || "-"}`;

      // ref > 1 说明这个块被多个序列或“序列 + 缓存”同时引用，用 ×N 标出来。
      if (block.ref > 1) {
        cell.appendChild(create("span", "ref", `×${block.ref}`));
      }
    }

    if (detailEl && !detailEl.dataset.pinned) {
      detailEl.textContent = "把鼠标移到块上，查看它被谁引用、是否已进前缀缓存。";
    }

    return {
      used: stepSnapshot.blocks.used.length,
      free: stepSnapshot.blocks.free.length,
      cached: cachedCount,
      shared: sharedCount,
    };
  }

  // 事件委托：块是动态改 class 的，但容器不变，所以监听容器即可。
  container.addEventListener("mouseover", (event) => {
    const cell = event.target.closest(".block");
    if (!cell || !snapshot || !detailEl) return;

    const id = Number(cell.dataset.id);
    const block = usedById.get(id);
    detailEl.dataset.pinned = "1";

    if (!block) {
      detailEl.textContent = `块 #${id} · 空闲`;
      return;
    }

    const owners = block.owners && block.owners.length ? block.owners.join("、") : "（无序列，仅缓存）";
    detailEl.textContent =
      `块 #${id} · 持有者 ${owners} · ref ${block.ref} · ` +
      `缓存 ${block.cached ? "是" : "否"} · hash ${block.hash || "-"}`;
  });

  container.addEventListener("mouseleave", () => {
    if (!detailEl) return;
    delete detailEl.dataset.pinned;
    detailEl.textContent = "把鼠标移到块上，查看它被谁引用、是否已进前缀缓存。";
  });

  return { update };
}

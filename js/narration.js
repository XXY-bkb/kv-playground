/**
 * 把模拟器产生的事件翻译成一句中文解释。
 *
 * 事件是“机器可读”的（type + 字段），前端直接展示 type 对学习没有帮助，
 * 所以这里集中维护一份“事件 -> 人话”的映射。新增事件类型时只需要改这一处。
 */

export function narrate(event) {
  switch (event.type) {
    case "ARRIVE":
      return `${event.seq} 到达，进入 waiting 队列。`;

    case "ADMIT":
      return `接纳 ${event.seq}：占用 ${event.blocks} 个块，前缀命中 ${event.prefix_hit} token。`;

    case "PREFIX_HIT":
      return `${event.seq} 命中前缀 ${event.tokens} token，直接共享块 #${event.blocks.join(
        "、#",
      )}。`;

    case "ALLOC":
      return `${event.seq} 分配 ${event.count} 个新块（#${event.blocks.join("、#")}）。`;

    case "PREFILL_START":
      return `${event.seq} 开始 prefill：token ${event.start} → ${event.end}。`;

    case "PREFILL_END":
      return `${event.seq} prefill 完成（累计 ${event.tokens} token），采样出第 1 个输出 token。`;

    case "DECODE":
      return `${event.seq} decode 出第 ${event.output_index} 个 token。`;

    case "PREEMPT":
      return `块不够，抢占 ${event.seq}：交还块引用（实际回到空闲池 ${event.freed_blocks} 块），回到 waiting；这是第 ${event.preemptions} 次被抢。`;

    case "EVICT":
      return `前缀缓存按 LRU 驱逐块 #${event.block}。`;

    case "FINISH":
      return `${event.seq} 完成（第 ${event.finish_step} 步），实际释放 ${event.freed_blocks} 个块。`;

    default:
      // 未知事件不要静默吞掉：保留原始字段，方便调试新加的调度逻辑。
      return `${event.type}：${JSON.stringify(event)}`;
  }
}

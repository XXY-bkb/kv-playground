/**
 * 请求时间线。
 *
 * 每一行是一条请求，每一列是一个模拟步；颜色表示这一步它在干什么：
 * 等待 / prefill / decode / 完成 / 被抢占。
 *
 * 有了这张图，就能直观看到“连续批处理”到底省在哪：
 * 并发场景里 decode 的绿色几乎不断，静态场景里则是一条一条排队。
 */

import { create } from "./util.js";

const COLORS = {
  "not-arrived": "#0b1324",
  waiting: "#475569",
  prefill: "#60a5fa",
  decode: "#34d399",
  finished: "#0f766e",
  running: "#1d4ed8",
  preempt: "#fbbf24",
};

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

export function createTimeline(container, trace) {
  const requests = trace.requests;
  const steps = trace.steps;
  const rowCount = requests.length;
  const stepCount = steps.length;
  const height = Math.max(56, rowCount * 20);

  // 先标出所有“被抢占”的时刻：同样是 waiting，但被抢占更值得注意。
  const preemptAt = new Set();
  for (const step of steps) {
    for (const event of step.events) {
      if (event.type === "PREEMPT") preemptAt.add(`${event.seq}:${step.step}`);
    }
  }

  const wrap = create("div", "timeline-wrap");

  // 左侧请求标签。用 grid 让每行高度和 SVG 的 viewBox 行高对齐。
  const labels = create("div", "timeline-labels");
  labels.style.height = `${height}px`;
  labels.style.gridTemplateRows = `repeat(${rowCount}, 1fr)`;
  for (const request of requests) {
    labels.appendChild(create("div", "timeline-label", request.id));
  }

  const svg = svgEl("svg", {
    class: "timeline-svg",
    viewBox: `0 0 ${stepCount} ${rowCount}`,
    preserveAspectRatio: "none",
    role: "img",
    "aria-label": "请求状态时间线",
  });
  svg.style.height = `${height}px`;

  // 当前步的竖向高亮；放在最底层，避免盖住状态格子。
  const cursorColumn = svgEl("rect", {
    class: "cursor-column",
    x: 0,
    y: 0,
    width: 1,
    height: rowCount,
    fill: "#ffffff",
    opacity: 0.08,
  });
  svg.appendChild(cursorColumn);

  for (let row = 0; row < rowCount; row += 1) {
    const request = requests[row];
    for (let step = 0; step < stepCount; step += 1) {
      const snapshot = steps[step];
      let status = "not-arrived";

      if (step >= request.arrival_step) {
        const state = snapshot.requests[request.id];
        if (state) {
          // prefill 是对象数组（带 start/end），decode 是请求 id 数组，两者形状不同。
          const inPrefill = (snapshot.batch.prefill || []).some(
            (chunk) => chunk.seq === request.id,
          );
          if (preemptAt.has(`${request.id}:${step}`)) {
            status = "preempt";
          } else if (state.status === "finished") {
            status = "finished";
          } else if (inPrefill) {
            status = "prefill";
          } else if (snapshot.batch.decode.includes(request.id)) {
            status = "decode";
          } else if (state.status === "waiting") {
            status = "waiting";
          } else {
            status = "running";
          }
        }
      }

      svg.appendChild(
        svgEl("rect", {
          x: step,
          y: row,
          width: 1,
          height: 1,
          fill: COLORS[status],
          "shape-rendering": "crispEdges",
        }),
      );
    }
  }

  // 播放头：一根竖线，随当前 step 移动。
  const playhead = svgEl("line", {
    x1: 0.5,
    x2: 0.5,
    y1: 0,
    y2: rowCount,
    stroke: "#f8fafc",
    "stroke-width": 1,
    opacity: 0.9,
    "vector-effect": "non-scaling-stroke",
  });
  svg.appendChild(playhead);

  wrap.appendChild(labels);
  wrap.appendChild(svg);

  const legend = create("div", "timeline-legend");
  const legendItems = [
    ["等待", COLORS.waiting],
    ["prefill", COLORS.prefill],
    ["decode", COLORS.decode],
    ["完成", COLORS.finished],
    ["被抢占", COLORS.preempt],
  ];
  for (const [text, color] of legendItems) {
    const item = create("span");
    const swatch = create("i");
    swatch.style.background = color;
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(text));
    legend.appendChild(item);
  }

  container.replaceChildren(wrap, legend);

  return {
    setCursor(step) {
      // viewBox 的 x 直接就是 step；cursor 宽 1 个 step，所以从 step 开始画。
      cursorColumn.setAttribute("x", String(step));
      playhead.setAttribute("x1", String(step + 0.5));
      playhead.setAttribute("x2", String(step + 0.5));
    },
  };
}

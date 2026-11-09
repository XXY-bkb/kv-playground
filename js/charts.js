/**
 * 极简折线图：只用 SVG，不引图表库。
 *
 * 需要画的东西很少（KV 利用率、每步吞吐），手写比引入 ECharts 更轻，
 * 也避免 GitHub Pages 上多一个 CDN 依赖。曲线一次性画完，
 * 之后每步只移动“当前点”和竖线，不重绘整张图。
 */

import { create } from "./util.js";

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

const WIDTH = 320;
const HEIGHT = 100;
const PAD = 6;

export function createLineChart(container, { title, values, color, format, max: fixedMax }) {
  const card = create("div", "chart-card");
  const head = create("div", "chart-head");
  const titleEl = create("span", "chart-title", title);
  const valueEl = create("span", "chart-value", "");
  head.appendChild(titleEl);
  head.appendChild(valueEl);
  card.appendChild(head);

  const svg = svgEl("svg", {
    class: "chart-svg",
    viewBox: `0 0 ${WIDTH} ${HEIGHT}`,
    preserveAspectRatio: "none",
    role: "img",
    "aria-label": title,
  });

  const count = values.length;
  // `max` 只决定纵轴刻度；标题里的“峰值”必须用真实数据算，
  // 不能拿纵轴上限冒充峰值（否则单请求的 KV 峰值会被写成 100%）。
  const max = fixedMax ?? Math.max(1e-9, ...values);
  const peak = Math.max(0, ...values);

  // 横坐标：把第 i 步映射到画布 x；只有一个点时放中间。
  const x = (index) =>
    count <= 1 ? WIDTH / 2 : PAD + (index / (count - 1)) * (WIDTH - 2 * PAD);
  // 纵坐标：0 在底部，max 在顶部。
  const y = (value) => HEIGHT - PAD - (value / max) * (HEIGHT - 2 * PAD);

  // 背景网格线（0%、50%、100%），帮助判断数值量级。
  for (const ratio of [0, 0.5, 1]) {
    const line = svgEl("line", {
      x1: PAD,
      x2: WIDTH - PAD,
      y1: y(max * ratio),
      y2: y(max * ratio),
      stroke: "#23324d",
      "stroke-width": 1,
      "stroke-dasharray": ratio === 0 ? "0" : "2 3",
      "vector-effect": "non-scaling-stroke",
    });
    svg.appendChild(line);
  }

  const points = values.map((value, index) => `${x(index)},${y(value)}`).join(" ");

  // 曲线下方淡淡的填充，让“高/低”更容易一眼看出。
  const area = svgEl("polygon", {
    points: `${x(0)},${HEIGHT - PAD} ${points} ${x(count - 1)},${HEIGHT - PAD}`,
    fill: color,
    opacity: 0.1,
  });
  svg.appendChild(area);

  const line = svgEl("polyline", {
    points,
    fill: "none",
    stroke: color,
    "stroke-width": 1.6,
    "stroke-linejoin": "round",
    "stroke-linecap": "round",
    "vector-effect": "non-scaling-stroke",
  });
  svg.appendChild(line);

  // 当前步的竖线和圆点。
  const cursor = svgEl("line", {
    x1: x(0),
    x2: x(0),
    y1: 0,
    y2: HEIGHT,
    stroke: "#f8fafc",
    "stroke-width": 1,
    opacity: 0.35,
    "stroke-dasharray": "2 3",
    "vector-effect": "non-scaling-stroke",
  });
  svg.appendChild(cursor);

  const dot = svgEl("circle", {
    cx: x(0),
    cy: y(values[0] || 0),
    r: 2.4,
    fill: color,
    stroke: "#0b1324",
    "stroke-width": 1,
  });
  svg.appendChild(dot);

  card.appendChild(svg);
  card.title = `${title} · 峰值 ${format(peak)}`;
  container.appendChild(card);

  return {
    setCursor(step) {
      const px = x(step);
      const py = y(values[step] || 0);
      cursor.setAttribute("x1", String(px));
      cursor.setAttribute("x2", String(px));
      dot.setAttribute("cx", String(px));
      dot.setAttribute("cy", String(py));
      valueEl.textContent = format(values[step] || 0);
    },
  };
}

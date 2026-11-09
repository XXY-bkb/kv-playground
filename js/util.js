/**
 * 小工具集合。
 *
 * 这个项目刻意不引入任何前端框架：
 * - 状态很少（一个 trace + 当前 step），用普通对象就够；
 * - 没有构建步骤，浏览器直接加载 ES module；
 * - 所有 DOM 都用 createElement/textContent 生成，不拼 HTML 字符串，避免 XSS。
 */

/** 请求配色：按请求出现顺序循环使用。颜色都偏亮，保证深色底上可读。 */
export const PALETTE = [
  "#34d399",
  "#60a5fa",
  "#f87171",
  "#fbbf24",
  "#a78bfa",
  "#f472b6",
  "#22d3ee",
  "#fb923c",
];

/** 查询单个元素。 */
export function $(selector, root = document) {
  return root.querySelector(selector);
}

/** 查询多个元素，返回真正的数组（方便 forEach/map）。 */
export function $$(selector, root = document) {
  return Array.from(root.querySelectorAll(selector));
}

/** 创建一个带 class/文本的元素；文本走 textContent，不解析 HTML。 */
export function create(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

/** 百分比：0.7969 -> "79.7%"。 */
export function fmtPct(value) {
  return `${(value * 100).toFixed(1)}%`;
}

/** 整数千分位：12345 -> "12,345"。 */
export function fmtInt(value) {
  return Number(value).toLocaleString("en-US");
}

/** 保留固定小数位；用于吞吐这类小数。 */
export function fmtFloat(value, digits = 2) {
  return Number(value).toFixed(digits);
}

/** 把 0..1 的比例限制在区间内，防止进度条宽度溢出。 */
export function clamp01(value) {
  return Math.min(1, Math.max(0, value));
}

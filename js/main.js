/**
 * 页面入口。
 *
 * 数据流很简单：
 *   1. 读 data/scenarios/index.json，拿到所有场景；
 *   2. 用户点某个场景，读对应的 trace JSON；
 *   3. 前端只做“回放”：把第 N 步的快照交给块池、时间线、图表、请求卡分别渲染。
 *
 * 这里不包含任何调度逻辑——调度在 Python 模拟器里，
 * 这样前端刷新不会改变结果，同一个 trace 每次播放都完全一样。
 */

import { PALETTE, $, $$, create, clamp01, fmtFloat, fmtInt, fmtPct } from "./util.js";
import { createBlockGrid } from "./block-grid.js";
import { createTimeline } from "./timeline.js";
import { createLineChart } from "./charts.js";
import { narrate } from "./narration.js";

/** 播放速度 1× 时，每一步停留的毫秒数。 */
const BASE_DELAY = 520;

const state = {
  index: null, // index.json 的内容
  trace: null, // 当前场景的完整 trace
  step: 0, // 当前播放到第几步（0-based）
  playing: false,
  speed: 1,
  timer: null,
  blockGrid: null,
  timeline: null,
  charts: [],
  reqCards: new Map(),
  metricRefs: {},
  tabButtons: [],
};

const els = {
  tabs: $("#scenarioTabs"),
  title: $("#scenarioTitle"),
  desc: $("#scenarioDesc"),
  chips: $("#summaryChips"),
  playBtn: $("#playBtn"),
  prevBtn: $("#prevBtn"),
  nextBtn: $("#nextBtn"),
  resetBtn: $("#resetBtn"),
  speed: $("#speed"),
  speedLabel: $("#speedLabel"),
  stepNow: $("#stepNow"),
  stepTotal: $("#stepTotal"),
  progress: $("#progressBar"),
  reqMeta: $("#reqMeta"),
  reqList: $("#reqList"),
  blockGrid: $("#blockGrid"),
  memoryMeta: $("#memoryMeta"),
  blockDetail: $("#blockDetail"),
  waitingList: $("#waitingList"),
  runningList: $("#runningList"),
  batchInfo: $("#batchInfo"),
  metricsGrid: $("#metricsGrid"),
  eventList: $("#eventList"),
  eventCount: $("#eventCount"),
  narration: $("#narration"),
  timeline: $("#timeline"),
  charts: $("#charts"),
};

/** 统一的 fetch 包装：失败时给出可读错误，而不是只抛一个 TypeError。 */
async function fetchJSON(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`读取 ${url} 失败（HTTP ${response.status}）`);
  }
  return response.json();
}

/** 场景按钮：按 group 分组，保持 index.json 里的顺序。 */
function renderTabs(scenarios) {
  els.tabs.replaceChildren();
  const groups = new Map();
  for (const scenario of scenarios) {
    if (!groups.has(scenario.group)) groups.set(scenario.group, []);
    groups.get(scenario.group).push(scenario);
  }

  for (const [group, items] of groups) {
    const groupEl = create("div", "tab-group");
    groupEl.appendChild(create("span", "group-label", group));

    for (const item of items) {
      const button = create("button", "tab", item.title);
      button.type = "button";
      button.dataset.id = item.id;
      button.title = item.description;
      button.addEventListener("click", () => loadScenario(item.id));
      groupEl.appendChild(button);
    }
    els.tabs.appendChild(groupEl);
  }

  state.tabButtons = $$(".tab", els.tabs);
}

/** 场景切换时禁用所有按钮，防止重复点击导致两个 fetch 竞争。 */
function setTabsDisabled(disabled) {
  for (const button of state.tabButtons) button.disabled = disabled;
}

function updateActiveTab(id) {
  for (const button of state.tabButtons) {
    button.classList.toggle("active", button.dataset.id === id);
  }
}

/** 标题下方的一排汇总数字。 */
function renderScenarioIntro(trace) {
  const meta = trace.scenario;
  const summary = trace.summary;
  els.title.textContent = meta.title;
  els.desc.textContent = meta.description;
  els.reqMeta.textContent = `${summary.total_requests} 条请求 · page_size ${trace.config.page_size}`;

  const chips = [
    ["请求", fmtInt(summary.total_requests)],
    ["模拟步数", fmtInt(summary.total_steps)],
    ["KV 峰值", fmtPct(summary.peak_kv_util)],
    ["前缀命中", `${fmtInt(summary.prefix_hit_tokens)} tok`],
    ["抢占", fmtInt(summary.preemptions)],
  ];
  els.chips.replaceChildren();
  for (const [label, value] of chips) {
    const chip = create("span", "chip");
    chip.appendChild(document.createTextNode(label));
    chip.appendChild(create("strong", "", value));
    els.chips.appendChild(chip);
  }
}

/** 请求卡片只建一次，之后每步只改文本和进度条宽度，避免整页重建。 */
function buildRequestCards(trace) {
  els.reqList.replaceChildren();
  state.reqCards = new Map();

  trace.requests.forEach((request, index) => {
    const card = create("div", "req-card waiting");
    card.dataset.seq = request.id;

    const head = create("div", "req-head");
    const dot = create("span", "req-dot");
    dot.style.background = PALETTE[index % PALETTE.length];
    const idEl = create("span", "req-id", request.id);
    const badge = create("span", "badge waiting", "等待");
    head.append(dot, idEl, badge);

    const bars = create("div", "req-bars");

    // prompt 进度条：computed 可能已经包含输出 token，所以这里要截断到 prompt_len。
    const promptRow = create("div", "bar-row");
    const promptLabel = create("span", "", "prompt");
    const promptTrack = create("div", "track");
    const promptFill = create("i");
    promptTrack.appendChild(promptFill);
    const promptCount = create("span", "", `0/${request.prompt_len}`);
    promptRow.append(promptLabel, promptTrack, promptCount);

    // output 进度条：已经生成了多少 token。
    const outputRow = create("div", "bar-row");
    const outputLabel = create("span", "", "output");
    const outputTrack = create("div", "track");
    const outputFill = create("i", "out");
    outputTrack.appendChild(outputFill);
    const outputCount = create("span", "", `0/${request.output_len}`);
    outputRow.append(outputLabel, outputTrack, outputCount);

    bars.append(promptRow, outputRow);

    const foot = create("div", "req-foot");
    const ttft = create("span", "", "TTFT –");
    const blocks = create("span", "", "blocks 0");
    const hit = create("span", "", "hit 0");
    const preempt = create("span", "", "抢占 0");
    foot.append(ttft, blocks, hit, preempt);

    card.append(head, bars, foot);
    els.reqList.appendChild(card);

    state.reqCards.set(request.id, {
      card,
      badge,
      promptFill,
      promptCount,
      outputFill,
      outputCount,
      ttft,
      blocks,
      hit,
      preempt,
    });
  });
}

/** 每步更新所有请求卡的状态、进度和脚注。 */
function renderRequests(snapshot) {
  const statusText = { waiting: "等待", running: "运行", finished: "完成" };

  for (const request of state.trace.requests) {
    const nodes = state.reqCards.get(request.id);
    const current = snapshot.requests[request.id];
    if (!nodes || !current) continue;

    const status = current.status || "waiting";
    nodes.card.className = `req-card ${status}`;
    nodes.badge.className = `badge ${status}`;
    nodes.badge.textContent = statusText[status] || status;

    const promptDone = Math.min(current.computed || 0, request.prompt_len);
    nodes.promptFill.style.width = `${clamp01(promptDone / request.prompt_len) * 100}%`;
    nodes.promptCount.textContent = `${promptDone}/${request.prompt_len}`;

    nodes.outputFill.style.width = `${clamp01((current.output || 0) / request.output_len) * 100}%`;
    nodes.outputCount.textContent = `${current.output || 0}/${request.output_len}`;

    const ttft = current.ttft_step;
    nodes.ttft.textContent = `TTFT ${ttft === null || ttft === undefined ? "–" : `${ttft} 步`}`;
    nodes.blocks.textContent = `blocks ${current.blocks.length}`;
    nodes.hit.textContent = `hit ${current.prefix_hit}`;
    nodes.preempt.textContent = `抢占 ${current.preemptions}`;
  }
}

/** 把 waiting/running 请求画成一排小标签。 */
function fillChips(container, ids) {
  container.replaceChildren();
  if (!ids.length) {
    container.appendChild(create("span", "muted", "空"));
    return;
  }
  for (const id of ids) container.appendChild(create("span", "seq-chip", id));
}

function renderQueues(snapshot) {
  fillChips(els.waitingList, snapshot.waiting);
  fillChips(els.runningList, snapshot.running);
}

/** 本步 batch 的内容：prefill 是带区间的对象，decode 是请求 id。 */
function renderBatch(snapshot) {
  els.batchInfo.replaceChildren();

  const prefillText = (snapshot.batch.prefill || [])
    .map((chunk) => `${chunk.seq}[${chunk.start},${chunk.end})`)
    .join("  ");
  const decodeText = (snapshot.batch.decode || []).join("  ");

  const prefillRow = create("div", "row");
  prefillRow.textContent = `prefill：${prefillText || "—"}`;
  const decodeRow = create("div", "row");
  decodeRow.textContent = `decode：${decodeText || "—"}`;

  els.batchInfo.append(
    document.createTextNode("本步批次"),
    prefillRow,
    decodeRow,
  );
}

/** 右侧 6 个指标卡，只建一次。 */
function buildMetrics() {
  const defs = [
    ["KV 利用率", "kv"],
    ["吞吐", "throughput"],
    ["已完成", "finished"],
    ["前缀命中", "hit"],
    ["累计抢占", "preempt"],
    ["空闲块", "free"],
  ];

  els.metricsGrid.replaceChildren();
  state.metricRefs = {};
  for (const [label, key] of defs) {
    const box = create("div", "metric");
    box.appendChild(create("span", "", label));
    const value = create("strong", "", "–");
    box.appendChild(value);
    els.metricsGrid.appendChild(box);
    state.metricRefs[key] = value;
  }
}

function renderMetrics(snapshot, blockStats) {
  const total = state.trace.summary.total_requests;
  state.metricRefs.kv.textContent = fmtPct(snapshot.metrics.kv_util);
  state.metricRefs.throughput.textContent = `${fmtFloat(snapshot.metrics.throughput, 2)} tok/步`;
  state.metricRefs.finished.textContent = `${snapshot.metrics.finished}/${total}`;
  state.metricRefs.hit.textContent = fmtInt(snapshot.metrics.prefix_hit_tokens);
  state.metricRefs.preempt.textContent = fmtInt(snapshot.metrics.preemptions);
  state.metricRefs.free.textContent = String(blockStats.free);
}

/** 本步事件列表 + 一句话总结。 */
function renderEvents(snapshot) {
  const events = snapshot.events || [];
  els.eventList.replaceChildren();
  els.eventCount.textContent = `${events.length} 条`;

  if (!events.length) {
    els.eventList.appendChild(create("li", "muted", "本步没有事件"));
    els.narration.textContent = "本步没有事件：所有 running 请求都在正常 decode。";
    return;
  }

  for (const event of events) {
    const item = create("li");
    item.appendChild(create("span", "ev-type", event.type));
    item.appendChild(create("span", "", narrate(event)));
    els.eventList.appendChild(item);
  }
  els.narration.textContent = events.map(narrate).join(" ");
}

/** 一步的完整渲染。所有视图都只读 snapshot，不改状态。 */
function renderStep(index) {
  const trace = state.trace;
  if (!trace) return;

  const clamped = Math.max(0, Math.min(trace.steps.length - 1, index));
  const snapshot = trace.steps[clamped];
  state.step = clamped;

  const blockStats = state.blockGrid.update(snapshot);
  state.timeline.setCursor(clamped);
  for (const chart of state.charts) chart.setCursor(clamped);

  renderRequests(snapshot);
  renderQueues(snapshot);
  renderBatch(snapshot);
  renderMetrics(snapshot, blockStats);
  renderEvents(snapshot);

  els.stepNow.textContent = String(clamped + 1);
  els.stepTotal.textContent = String(trace.steps.length);
  const ratio = trace.steps.length <= 1 ? 0 : clamped / (trace.steps.length - 1);
  els.progress.style.width = `${ratio * 100}%`;

  els.prevBtn.disabled = clamped === 0;
  els.nextBtn.disabled = clamped === trace.steps.length - 1;
  els.memoryMeta.textContent =
    `已用 ${blockStats.used}/${blockStats.used + blockStats.free} · ` +
    `缓存 ${blockStats.cached} · 共享 ${blockStats.shared}`;
}

/** 切换场景：先暂停、禁用按钮，再读 trace 并重建全部视图。 */
async function loadScenario(id) {
  if (!state.index) return;
  const meta =
    state.index.scenarios.find((item) => item.id === id) || state.index.scenarios[0];

  pause();
  setTabsDisabled(true);

  try {
    const trace = await fetchJSON(`data/scenarios/${meta.file}`);
    state.trace = trace;
    renderScenarioIntro(trace);

    state.blockGrid = createBlockGrid(els.blockGrid, els.blockDetail, trace);
    state.timeline = createTimeline(els.timeline, trace);

    els.charts.replaceChildren();
    state.charts = [
      createLineChart(els.charts, {
        title: "KV 利用率",
        values: trace.steps.map((step) => step.metrics.kv_util),
        color: "#5eead4",
        format: fmtPct,
        max: 1,
      }),
      createLineChart(els.charts, {
        title: "吞吐（token/步）",
        values: trace.steps.map((step) => step.metrics.throughput),
        color: "#60a5fa",
        format: (value) => fmtFloat(value, 2),
      }),
    ];

    buildRequestCards(trace);
    buildMetrics();
    renderStep(0);

    updateActiveTab(meta.id);
    const url = new URL(location.href);
    url.searchParams.set("scenario", meta.id);
    history.replaceState(null, "", url);
  } catch (error) {
    showError(error);
  } finally {
    setTabsDisabled(false);
  }
}

function showError(error) {
  console.error(error);
  els.title.textContent = "加载失败";
  els.desc.textContent =
    `${error.message}。请确认在仓库根目录运行 \`python -m http.server 8000\`，` +
    "而不是直接双击 index.html（file:// 下浏览器禁止读取 JSON）。";
  els.chips.replaceChildren();
}

/* -------------------------------------------------------------------------- */
/* 播放控制                                                                    */
/* -------------------------------------------------------------------------- */

function updatePlayLabel() {
  els.playBtn.textContent = state.playing ? "⏸ 暂停" : "▶ 播放";
}

function scheduleNext() {
  clearTimeout(state.timer);
  state.timer = setTimeout(() => {
    if (!state.playing || !state.trace) return;
    if (state.step >= state.trace.steps.length - 1) {
      pause();
      return;
    }
    renderStep(state.step + 1);
    scheduleNext();
  }, BASE_DELAY / state.speed);
}

function play() {
  if (!state.trace || state.playing) return;
  if (state.step >= state.trace.steps.length - 1) renderStep(0);
  state.playing = true;
  updatePlayLabel();
  scheduleNext();
}

function pause() {
  state.playing = false;
  clearTimeout(state.timer);
  state.timer = null;
  updatePlayLabel();
}

function togglePlay() {
  if (state.playing) pause();
  else play();
}

function stepBy(delta) {
  if (!state.trace) return;
  pause();
  renderStep(state.step + delta);
}

function bindControls() {
  els.playBtn.addEventListener("click", togglePlay);
  els.prevBtn.addEventListener("click", () => stepBy(-1));
  els.nextBtn.addEventListener("click", () => stepBy(1));
  els.resetBtn.addEventListener("click", () => {
    pause();
    renderStep(0);
  });

  els.speed.addEventListener("input", () => {
    state.speed = Number(els.speed.value) || 1;
    els.speedLabel.textContent = `${state.speed}×`;
    if (state.playing) scheduleNext();
  });

  // 空格播放/暂停，左右方向键单步；输入框和按钮获得焦点时不拦截，
  // 否则空格会既触发按钮点击又触发全局快捷键，造成“按一次切两次”。
  document.addEventListener("keydown", (event) => {
    const tag = event.target && event.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON") return;
    if (event.code === "Space") {
      event.preventDefault();
      togglePlay();
    } else if (event.code === "ArrowLeft") {
      event.preventDefault();
      stepBy(-1);
    } else if (event.code === "ArrowRight") {
      event.preventDefault();
      stepBy(1);
    }
  });
}

/* -------------------------------------------------------------------------- */
/* 启动                                                                        */
/* -------------------------------------------------------------------------- */

async function boot() {
  bindControls();

  if (location.protocol === "file:") {
    showError(new Error("当前是 file:// 协议，浏览器不会允许读取本地 JSON"));
    return;
  }

  state.index = await fetchJSON("data/scenarios/index.json");
  renderTabs(state.index.scenarios);

  const wanted = new URLSearchParams(location.search).get("scenario");
  const initial =
    state.index.scenarios.find((item) => item.id === wanted) || state.index.scenarios[0];
  await loadScenario(initial.id);
}

boot().catch((error) => showError(error));

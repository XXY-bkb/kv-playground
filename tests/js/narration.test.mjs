/**
 * 事件翻译测试：保证页面上的解释文字不会因为改字段而悄悄变空/变成 undefined。
 * 只挑几个最常用的事件，未知事件则要求保留原始 JSON 方便排查。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { narrate } from "../../js/narration.js";

test("ARRIVE 说清楚谁进了 waiting 队列", () => {
  assert.equal(
    narrate({ type: "ARRIVE", seq: "r1" }),
    "r1 到达，进入 waiting 队列。",
  );
});

test("PREFIX_HIT 列出命中的 token 数与块号", () => {
  assert.equal(
    narrate({ type: "PREFIX_HIT", seq: "r2", tokens: 128, blocks: [0, 1] }),
    "r2 命中前缀 128 token，直接共享块 #0、#1。",
  );
});

test("未知事件保留原始字段，不静默吞掉", () => {
  const text = narrate({ type: "MYSTERY", seq: "r9" });
  assert.match(text, /^MYSTERY：/);
  assert.match(text, /r9/);
});

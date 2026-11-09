/**
 * 前端纯函数测试：不依赖浏览器 DOM，只测能被 node:test 直接跑的部分。
 *
 * 运行：`node --test tests/js`
 * 这些函数看起来简单，但格式化错了会让页面上的数字误导人，
 * 所以用测试把“输入 -> 字符串”的约定固定下来。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { fmtPct, fmtInt, fmtFloat, clamp01 } from "../../js/util.js";

test("fmtPct 保留一位小数并加百分号", () => {
  assert.equal(fmtPct(0), "0.0%");
  assert.equal(fmtPct(0.375), "37.5%");
  assert.equal(fmtPct(1), "100.0%");
});

test("fmtInt 使用千分位", () => {
  assert.equal(fmtInt(0), "0");
  assert.equal(fmtInt(12345), "12,345");
});

test("fmtFloat 固定小数位", () => {
  assert.equal(fmtFloat(1), "1.00");
  // 1.23456 才能稳定四舍五入到 1.235；1.2345 在二进制里略小于 1.2345。
  assert.equal(fmtFloat(1.23456, 3), "1.235");
});

test("clamp01 把比例限制在 0..1", () => {
  assert.equal(clamp01(-1), 0);
  assert.equal(clamp01(0.5), 0.5);
  assert.equal(clamp01(2), 1);
});

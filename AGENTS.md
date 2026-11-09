# 项目约定（给未来的自己 / 给 AI 协作者）

## 沟通与文档

- 代码注释一律用中文，解释“这段在干什么、为什么这么写”；不要只写“初始化”“循环”这种废话。
- 敲定的方案、接口、不变量先写进 `README.md` 或 `docs/design.md`，再写代码；不要只留在对话里。
- 简历/博客里只写能复现的数字；模拟步数不能写成真实 GPU 吞吐。

## 代码

- **零第三方依赖**：Python 只用标准库，前端只用原生 HTML/CSS/JS。
  原因：这台 Windows 机器的 npm 缓存目录在沙箱外（`npm` 会 EPERM），
  少一层依赖就少一类环境问题，GitHub Pages 也能直接托管。
- 前端不做构建步骤：`index.html` 用 `<script type="module">` 直接加载 `js/*.js`。
- Python 3.10+；`from __future__ import annotations`；类型标注尽量写全。
- 数据结构/调度器的行为改动，必须同步改测试；测试是验收标准。

## 命令

```bash
python -m sim.export                 # 重新生成 data/scenarios/*.json
python -m unittest discover -s tests -v
python -m http.server 8000           # 本地看前端
```

## 提交

- 小步提交：模拟器、测试、trace、前端、文档分开提交。
- 提交信息用 `feat:` / `fix:` / `docs:` / `test:` / `chore:` 前缀。
- 不提交 `tests/_tmp_export/`、`__pycache__/`、`node_modules/`、`dist/`。

## 评审

- 每完成一个可运行的阶段，提醒用户开一个新对话做审核（干净上下文，避免自审偏差）。

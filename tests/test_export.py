"""导出层的测试：确保“重复导出结果完全一致”，并且 manifest 和 trace 对得上。

为什么单独测这一层：
- 前端完全依赖导出的 JSON，字段错了页面就白屏；
- 如果导出不确定（比如 dict 顺序、时间戳、随机数），每次 git diff 都会变，
  后面就没法用“diff 为空”来判断代码有没有改变行为。
"""

import json
import shutil
import unittest
from pathlib import Path

from sim.export import SCHEMA, build_trace, export_all
from sim.scenarios import SCENARIO_DEFS


# 测试用的临时目录放在仓库内（tests/_tmp_export），不要用系统 temp：
# 1) Windows 沙箱对系统 temp 的清理权限不稳定；
# 2) 放在仓库内方便出问题时直接看导出的 JSON。
_TMP_ROOT = Path(__file__).resolve().parent / "_tmp_export"


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
        _TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)

    def test_export_all_writes_every_scenario_and_manifest(self) -> None:
        target = _TMP_ROOT / "all"
        written = export_all(target)

        # 6 个场景 + 1 个 index.json
        self.assertEqual(len(written), len(SCENARIO_DEFS) + 1)

        index = json.loads((target / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schema"], SCHEMA)
        self.assertEqual(len(index["scenarios"]), len(SCENARIO_DEFS))

        for entry in index["scenarios"]:
            path = target / entry["file"]
            self.assertTrue(path.exists(), f"manifest 指向的文件不存在：{path}")
            trace = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(trace["schema"], SCHEMA)
            self.assertEqual(trace["scenario"]["id"], entry["id"])
            self.assertEqual(trace["summary"], entry["summary"])
            self.assertGreater(len(trace["steps"]), 0)

    def test_export_is_deterministic(self) -> None:
        first_dir = _TMP_ROOT / "first"
        second_dir = _TMP_ROOT / "second"
        export_all(first_dir)
        export_all(second_dir)

        first_files = sorted(first_dir.glob("*.json"))
        second_files = sorted(second_dir.glob("*.json"))
        self.assertEqual([p.name for p in first_files], [p.name for p in second_files])

        for first, second in zip(first_files, second_files):
            self.assertEqual(
                first.read_bytes(),
                second.read_bytes(),
                f"{first.name} 两次导出内容不一致",
            )

    def test_build_trace_is_stable(self) -> None:
        first = build_trace("single")
        second = build_trace("single")
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()

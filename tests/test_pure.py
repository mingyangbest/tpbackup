#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯函数单测：分片数 / 排除匹配 / 路径白名单 / 空文件计数 + 一处接线回归。

只覆盖「不需要云盘、不需要起服务」的逻辑——这些正是 v0.2.1 改动的部分，
也是最容易在重构里悄悄退化、又只能靠真机才能发现的地方。

跑法：python -m pytest -q
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import engine  # noqa: E402
from app.paths import within  # noqa: E402

PART = engine.PART


class FakeCloud:
    """collect 只用到 ls / mkdir 两个方法。"""

    def __init__(self):
        self.made = []

    def ls(self, cid):
        return []

    def mkdir(self, parent, name):
        self.made.append(name)
        return "d:%s" % name


def _stats():
    return {"todo": 0, "exist": 0, "skip": 0, "err": 0, "empty": 0, "empty_names": []}


# ── 分片数：向上取整，末片不得超过 PART ─────────────────────
@pytest.mark.parametrize("size,parts", [
    (0, 1),
    (1, 1),
    (PART - 1, 1),
    (PART, 1),
    (PART + 1, 2),          # 老逻辑这里是 1 片，整包发 8MiB+1 字节
    (10 << 20, 2),          # 发版门用的 10MiB
    (20 << 20, 3),          # 老逻辑 8+12MiB，末片比标准片还大
    (15 << 20, 2),
    (22 << 30, 2816),
])
def test_part_count_is_ceil(size, parts):
    assert engine.part_count(size) == parts


def test_part_slices_sum_and_cap():
    for size in (1, PART - 1, PART, PART + 1, 10 << 20, 20 << 20, (15 << 20) + 7):
        total = engine.part_count(size)
        head = min(PART, size)
        slices = [head] * (total - 1) + [size - head * (total - 1)]
        assert sum(slices) == size
        assert max(slices) <= PART
        assert min(slices) > 0


# ── 排除匹配 ───────────────────────────────────────────────
def test_match_exclude_glob():
    assert engine.match_exclude("a.tmp", False, {"*.tmp"})
    assert engine.match_exclude("a.tmp", True, {"*.tmp"})
    assert not engine.match_exclude("a.txt", False, {"*.tmp"})
    assert engine.match_exclude("#recycle", True, {"#recycle"})
    assert not engine.match_exclude("keep", False, set())


@pytest.mark.parametrize("excludes_files,expect_tmp", [
    ({"*.tmp"}, False),     # 修复后的行为：任务排除项对文件生效
    (set(), True),          # v0.2.0 的行为：sync 只把默认三项给了文件（这里空集等价）
])
def test_collect_respects_file_excludes(tmp_path, excludes_files, expect_tmp):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "excluded").mkdir()
    (src / "a.txt").write_text("hello")
    (src / "skip.tmp").write_text("abc")
    (src / "excluded" / "x.txt").write_text("x")
    (src / "sub" / "b.tmp").write_text("abcd")
    (src / "sub" / "c.txt").write_text("ok")
    (src / "empty.gitkeep").write_text("")

    jobs, stats = [], _stats()
    engine.collect(FakeCloud(), str(src), "root", jobs, stats,
                   {"excluded"}, excludes_files)

    # 相对路径带前导 "/" 是现状（v0.2.0 起就没改），这里只钉住集合
    names = sorted(j[4] for j in jobs)
    assert ("/skip.tmp" in names) is expect_tmp
    assert ("/sub/b.tmp" in names) is expect_tmp
    assert "/a.txt" in names and "/sub/c.txt" in names
    assert "/excluded/x.txt" not in names


# ── 空文件：单独计数，不并进 skip ──────────────────────────
def test_collect_counts_empty_separately(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.txt").write_text("abc")
    (src / ".gitkeep").write_text("")
    (src / "also-empty").write_text("")

    jobs, stats = [], _stats()
    engine.collect(FakeCloud(), str(src), "root", jobs, stats, set(), set())

    assert stats["empty"] == 2
    assert stats["skip"] == 0              # 关键：不再冒充「被排除」
    assert stats["empty_names"] == [".gitkeep", "also-empty"]
    assert [j[4] for j in jobs] == ["/keep.txt"]


# ── 路径白名单：realpath + 按路径段比较 ────────────────────
def test_within_normalises_and_bounds(tmp_path):
    root = tmp_path / "vol1"
    (root / "photos").mkdir(parents=True)
    assert within(str(root / "photos"), [str(root)]) == os.path.realpath(root / "photos")
    assert within(str(root), [str(root)]) == os.path.realpath(root)
    # 前缀不放行：/vol1 不能收 /vol10/...
    assert within(str(tmp_path / "vol10" / "photos"), [str(root)]) is None
    # 不存在的尾部只是规范化，不报错（建任务时目录可能还没挂上来）
    assert within(str(root / "photos" / "new"), [str(root)]) == \
        os.path.realpath(root / "photos" / "new")
    assert within("", [str(root)]) is None


def test_within_blocks_symlink_escape(tmp_path):
    root = tmp_path / "vol1"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    link = root / "escape"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不支持软链")
    assert within(str(link), [str(root)]) is None
    assert within(str(link / "secret"), [str(root)]) is None


# ── 接线回归：runner 必须把同一份排除规则同时喂给目录与文件 ──
def test_runner_feeds_excludes_to_both_sides(tmp_path, monkeypatch):
    from app import runner as runner_mod

    captured = {}

    def fake_sync(**kw):
        captured.update(kw)
        return {"ok": True, "todo": 0, "ok_n": 0, "fail": 0, "bytes": 0,
                "elapsed": 0.1, "reason": "", "detail": ""}

    monkeypatch.setattr(runner_mod.engine, "sync", fake_sync)

    class FakeDB:
        def __init__(self):
            self.finished = []

        def start_run(self, tid, name):
            return 1

        def finish_run(self, rid, res, status, logtext=""):
            self.finished.append((status, res))

        def prune_runs(self, *a, **k):
            pass

    logs = tmp_path / "logs"
    r = runner_mod.Runner(FakeDB(), object(), str(logs))
    task = {"id": 1, "name": "t", "src": str(tmp_path), "cloud": '["A"]',
            "excludes": '["*.tmp", "#recycle"]', "workers": 2, "mode": "dir"}
    assert r.start(task) == 1

    deadline = time.time() + 5
    while r.is_running(1) and time.time() < deadline:
        time.sleep(0.02)
    assert not r.is_running(1), "任务没有在 5 秒内结束"

    assert captured["excludes_dirs"] == ["*.tmp", "#recycle"]
    # v0.2.0 这里传的是 None：界面承诺的「排除文件」根本没生效
    assert captured["excludes_files"] == ["*.tmp", "#recycle"]

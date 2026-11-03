#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传后复核与统计口径的单测。

这里钉的是「报表数字对不对」，不是「文件传没传上去」：三处都是 v0.2.2 修的
诊断口径问题，改坏不会让备份失败，只会让人看错账——

  · push 的哈希复核以前遍历 items（含上传失败的），现在只遍历 uploaded；
  · push 的哈希复核以前没有体积上限，与 sync 不一致；
  · push 的云端索引以前用原始大小写，云盘本身大小写不敏感；
  · sync 的一个文件复核失败时会同时计入成功和失败。

网络层用一个只记账的假云盘替掉，不建连接、不发请求。
"""
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import engine  # noqa: E402


class FakeCloud:
    """最小可用的假云盘：一棵两层目录树 + 上传记账。

    故意不做任何网络行为，也不做重试——重试逻辑有单独的测试，这里只关心
    「复核看了哪些文件、拿什么名字去比」。
    """

    def __init__(self, wrong_size=(), wrong_hash=(), fail_names=(),
                 lower_names=False, preload=()):
        self.tree = {"0": [{"type": 0, "name": "root", "id": "D1"}], "D1": []}
        self.wrong_size = set(wrong_size)
        self.wrong_hash = set(wrong_hash)
        self.fail_names = set(fail_names)
        self.lower_names = lower_names
        self.at, self.key = "T" * 32, b"0123456789abcdef"
        self.uploaded = []
        for e in preload:
            self.tree["D1"].append(dict(e))

    # ── 协议面（只实现用到的那几个）───────────────
    def ensure_family(self):
        return None

    def refresh(self):
        return None

    def ls(self, cid):
        return list(self.tree.get(str(cid), []))

    def mkdir(self, parent, name):
        nid = "D-%s" % name
        self.tree[str(parent)].append({"type": 0, "name": name, "id": nid})
        self.tree[nid] = []
        return nid

    def upload(self, path, dirid, name=None, on_part=None):
        n = name or os.path.basename(path)
        if n in self.fail_names:
            raise RuntimeError("模拟上传失败: %s" % n)
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        entry = {
            "type": 1,
            "name": (n.lower() if self.lower_names else n),
            "id": "F-%s" % n,
            "size": (sz + 1) if n in self.wrong_size else sz,
            "sha256": ("0" * 64) if n in self.wrong_hash else h,
        }
        self.tree[str(dirid)].append(entry)
        self.uploaded.append(n)
        return entry["id"]


class StubStore:
    """TokenStore 的最小替身：只提供 sync/push 会碰的三个方法。"""

    def load(self):
        return ("A" * 32, "R" * 32)

    def save(self, at, rt):
        return None

    def maybe_renew(self, w, log):
        return None


def _use_sync_cloud(monkeypatch, cloud):
    """sync 里是直接 Wopan(at, rt) 建客户端的，所以替掉类本身。"""
    monkeypatch.setattr(engine, "Wopan",
                        lambda at, rt, on_token=None: cloud)


def _use_push_cloud(monkeypatch, cloud):
    """push 走 _connect(store, log)，替掉连接函数即可。"""
    monkeypatch.setattr(engine, "_connect", lambda store, log: (cloud, {}))


def _mk(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ── sync：统计口径 ────────────────────────────────────────
def test_sync_verify_failure_not_counted_as_success(tmp_path, monkeypatch):
    """上传成功但复核不过的文件，只能算失败，不能同时算成功。"""
    _mk(tmp_path, "a.txt", b"aaa")
    _mk(tmp_path, "b.txt", b"bbbb")
    cloud = FakeCloud(wrong_size={"b.txt"})       # b 的云端体积对不上
    _use_sync_cloud(monkeypatch, cloud)

    res = engine.sync(StubStore(), str(tmp_path), ["root"], log=lambda m: None)

    assert res["todo"] == 2
    assert res["ok"] is False
    assert res["ok_n"] == 1, "a 成功、b 复核失败，成功数只能有 1"
    assert res["fail"] == 1
    assert res["ok_n"] + res["fail"] == res["todo"], "成功 + 失败不能超过实际文件数"


def test_sync_all_good_keeps_counts(tmp_path, monkeypatch):
    """全传对时口径不变：ok_n == todo，fail == 0（改口径别改坏正常路径）。"""
    _mk(tmp_path, "a.txt", b"aaa")
    _mk(tmp_path, "b.txt", b"bbbb")
    cloud = FakeCloud()
    _use_sync_cloud(monkeypatch, cloud)

    res = engine.sync(StubStore(), str(tmp_path), ["root"], log=lambda m: None)
    assert res["ok"] is True and res["ok_n"] == 2 and res["fail"] == 0


# ── push：哈希复核的范围 ──────────────────────────────────
def test_push_hash_check_skips_failed_uploads(tmp_path, monkeypatch):
    """上传失败的文件不该再被拿去和云端旧记录比哈希。

    以前遍历 items：bad.bin 上传失败，云端还留着上一轮的旧条目（旧哈希），
    于是多报一条「内容哈希不符」，hashed 也会虚高。
    """
    good = _mk(tmp_path, "good.bin", b"g" * 32)
    bad = _mk(tmp_path, "bad.bin", b"b" * 32)
    cloud = FakeCloud(fail_names={"bad.bin"},
                      preload=[{"type": 1, "name": "bad.bin", "id": "old",
                                "size": 32, "sha256": "0" * 64}])
    _use_push_cloud(monkeypatch, cloud)
    logs = []

    res = engine.push(StubStore(), [(good, "good.bin"), (bad, "bad.bin")],
                      ["root"], log=logs.append)

    assert res["ok"] is False and res["fail"] == 1
    assert not any("内容哈希不符" in m for m in logs), "失败项不该参与哈希复核"
    assert any("SHA256 一致 1 个" in m for m in logs), "只该复核真正传上去的 1 个"


def test_push_hash_check_respects_size_cap(tmp_path, monkeypatch):
    """超过 VERIFY_SHA256_MAX 的文件只比体积，与 sync 同口径。"""
    monkeypatch.setattr(engine, "VERIFY_SHA256_MAX", 1)
    big = _mk(tmp_path, "big.bin", b"x" * 64)
    cloud = FakeCloud(wrong_hash={"big.bin"})
    _use_push_cloud(monkeypatch, cloud)
    logs = []

    res = engine.push(StubStore(), [(big, "big.bin")], ["root"], log=logs.append)

    assert res["ok"] is True, "大文件不该因为云端哈希不同就判失败"
    assert not any("内容哈希不符" in m for m in logs)


def test_push_hash_mismatch_still_caught(tmp_path, monkeypatch):
    """加体积上限之后，小文件的内容哈希仍然要真比、真报错。"""
    small = _mk(tmp_path, "s.bin", b"x" * 64)
    cloud = FakeCloud(wrong_hash={"s.bin"})
    _use_push_cloud(monkeypatch, cloud)
    logs = []

    res = engine.push(StubStore(), [(small, "s.bin")], ["root"], log=logs.append)

    assert res["ok"] is False
    assert any("内容哈希不符" in m for m in logs)


# ── push：大小写 ──────────────────────────────────────────
def test_push_verify_is_case_insensitive(tmp_path, monkeypatch):
    """云端回小写、本地文件名大写时不能报「云端未找到」。

    云盘本身大小写不敏感，sync 和孤儿扫描早就按小写比对，push 这条漏了。
    """
    p = _mk(tmp_path, "A.TXT", b"hello")
    cloud = FakeCloud(lower_names=True)
    _use_push_cloud(monkeypatch, cloud)
    logs = []

    res = engine.push(StubStore(), [(p, "A.TXT")], ["root"], log=logs.append)

    assert res["ok"] is True, logs
    assert not any("云端未找到" in m for m in logs)

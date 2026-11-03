#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断点续传的纯逻辑单测：分片偏移 / 会话落盘 / 判废条件。

不碰网络：_upload_part 被替换成一个只记账的假实现，所以这里测的是
「第几片、发多少字节、uniqueId 与 fileInfo 有没有换」这些真正容易错的地方。
真机路径另由 tools/probe_resume.py 回归。
"""
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import engine  # noqa: E402
from app.store import SessionStore  # noqa: E402


class FakeUpload(engine.Wopan):
    """只替换 _upload_part 的 Wopan：不建连接、不发请求。

    part_fields 用真的——字段构造是这次要保护的对象，不能用假的替掉。
    """

    def __init__(self, token="T" * 8, fail_at=None):
        self.at = token
        self.key = b"0123456789abcdef"      # 16 字节，够 aes_enc 用
        self.calls = []
        self.fail_at = fail_at

    def _upload_part(self, fields, filename, blob):
        idx = int(fields["partIndex"])
        if self.fail_at and idx == self.fail_at:
            raise RuntimeError("模拟断线@%d" % idx)
        self.calls.append({"idx": idx, "n": len(blob),
                           "uid": fields["uniqueId"],
                           "info": fields["fileInfo"],
                           "at": fields["accessToken"]})
        return {"code": "0000", "data": {"fid": "F%d" % idx}}


@pytest.fixture
def small_part(monkeypatch):
    """把分片大小临时调小，好在一屏内写完测试。

    顺带把重试的 sleep 掐掉：失败用例要跑满 3 次重试，真睡就是 12 秒。
    """
    monkeypatch.setattr(engine, "PART", 1000)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    return 1000


def _file(tmp_path, size):
    p = tmp_path / "blob.bin"
    p.write_bytes(bytes((i * 37) % 256 for i in range(size)))
    return str(p)


def _session(size, total, uid="U1", name="blob.bin", dirid="D1"):
    ses = engine.new_upload_session(dirid, name, size)
    ses["total"] = total            # 小文件场景下由测试指定
    ses["uid"] = uid
    return ses


def test_upload_parts_full_range(small_part, tmp_path):
    """不续传时：1..total 全发，末片按剩余字节算。"""
    p = _file(tmp_path, 5400)
    ses = _session(5400, 6)
    w = FakeUpload()
    with open(p, "rb") as f:
        fid = w.upload_parts(ses, "INFO", f, 1)
    assert [c["idx"] for c in w.calls] == [1, 2, 3, 4, 5, 6]
    assert [c["n"] for c in w.calls] == [1000, 1000, 1000, 1000, 1000, 400]
    assert fid == "F6"


def test_upload_parts_resumes_and_seeks(small_part, tmp_path):
    """从第 4 片续：文件指针必须落到 offset=3000，且只发 4..6。"""
    p = _file(tmp_path, 5400)
    ses = _session(5400, 6)
    w = FakeUpload()
    with open(p, "rb") as f:
        fid = w.upload_parts(ses, "INFO", f, 4)
    assert [c["idx"] for c in w.calls] == [4, 5, 6]
    assert [c["n"] for c in w.calls] == [1000, 1000, 400]
    # 末片大小 = size - (total-1)*PART，与整传一致
    assert sum(c["n"] for c in w.calls) == 5400 - 3000
    assert fid == "F6"


def test_upload_parts_start_beyond_total_is_noop(small_part, tmp_path):
    """done==total 时（传完但没验过）再来一次：一片都不发，不报错。"""
    p = _file(tmp_path, 5400)
    ses = _session(5400, 6)
    w = FakeUpload()
    with open(p, "rb") as f:
        fid = w.upload_parts(ses, "INFO", f, 7)
    assert w.calls == []
    assert fid == ""


def test_upload_parts_short_read_raises(small_part, tmp_path):
    """文件被截短：宁可当场失败，也不要发一份 partSize 不对的片。"""
    p = _file(tmp_path, 5400)
    ses = _session(9000, 9)          # 声称 9000，实际只有 5400
    w = FakeUpload()
    with open(p, "rb") as f:
        with pytest.raises(RuntimeError, match="本地读取不足"):
            w.upload_parts(ses, "INFO", f, 1)


def test_session_key_changes_with_mtime():
    a = engine.session_key("D1", "x/y.bin", 100, 111)
    b = engine.session_key("D1", "x/y.bin", 100, 111)
    c = engine.session_key("D1", "x/y.bin", 100, 112)
    assert a == b
    assert a != c


def test_token_fingerprint_stable_and_sensitive():
    assert engine.token_fingerprint("abc") == engine.token_fingerprint("abc")
    assert engine.token_fingerprint("abc") != engine.token_fingerprint("abd")


def test_should_resume_threshold(monkeypatch):
    assert engine.should_resume(1024, enabled=True, threshold=0) is True
    assert engine.should_resume(engine.RESUME_MIN_SIZE - 1,
                                enabled=True, threshold=engine.RESUME_MIN_SIZE) is False
    assert engine.should_resume(engine.RESUME_MIN_SIZE,
                                enabled=True, threshold=engine.RESUME_MIN_SIZE) is True
    assert engine.should_resume(10 ** 9, enabled=False) is False


def test_session_store_roundtrip(tmp_path):
    s = SessionStore(str(tmp_path / "t.db"))
    ses = engine.new_upload_session("D1", "a.bin", 5000)
    ses.update(skey="K1", mtime_ns=42, token_fp="fp1", done=7)
    s.save(ses)

    got = s.get("K1")
    assert got["done"] == 7
    assert got["uid"] == ses["uid"]
    assert got["batch_no"] == ses["batch_no"]
    # info_plain 必须回来是 dict：续传时要拿它按当前 key 重加密
    assert isinstance(got["info_plain"], dict)
    assert got["info_plain"]["batchNo"] == ses["batch_no"]
    assert got["token_fp"] == "fp1"

    s.drop("K1")
    assert s.get("K1") is None


def test_session_store_ttl_drops_stale_row(tmp_path):
    db = str(tmp_path / "t.db")
    s = SessionStore(db)
    ses = engine.new_upload_session("D1", "a.bin", 5000)
    ses.update(skey="K2", mtime_ns=1, token_fp="fp", done=3)
    s.save(ses)
    # 手动把 updated_at 推到 2 天前
    c = sqlite3.connect(db)
    c.execute("update upload_sessions set updated_at=datetime('now','localtime','-2 days')")
    c.commit()
    c.close()

    assert s.get("K2", ttl=24 * 3600) is None, "超过 TTL 的会话必须判废"
    assert s.count() == 0, "判废时应顺手删掉，别留垃圾"


def _upload(sessions, path, dirid="D1", name="a.bin", w=None, skey="K9"):
    w = w or FakeUpload()
    return engine.upload_resumable(w, sessions, skey, path, dirid, name), w


def test_resumable_keeps_progress_then_continues(small_part, tmp_path):
    """断在第 4 片：done 必须落成 3；下一次从第 4 片接上，且会话三件套不变。"""
    p = _file(tmp_path, 5400)
    s = SessionStore(str(tmp_path / "t.db"))

    # 第一次：第 4 片断线
    with pytest.raises(RuntimeError, match="模拟断线"):
        _upload(s, p, w=FakeUpload(fail_at=4))
    row = s.get("K9")
    assert row is not None, "失败后会话必须留着，否则下次只能从零重传"
    assert row["done"] == 3
    first_uid, first_batch = row["uid"], row["batch_no"]

    # 第二次：应当从第 4 片接上
    _, w2 = _upload(s, p)
    assert [c["idx"] for c in w2.calls] == [4, 5, 6]
    assert all(c["uid"] == first_uid for c in w2.calls), "uniqueId 必须沿用"
    assert all(c["info"] for c in w2.calls)
    # 传给服务端的仍是同一份 fileInfo（同一 batchNo、同一 key）
    assert len({c["info"] for c in w2.calls}) == 1
    assert s.get("K9")["batch_no"] == first_batch, "batchNo 必须沿用，否则就是另一次上传"
    assert s.get("K9")["done"] == 6, "跑完后 done 必须等于 total"


def test_resumable_restarts_on_token_change(small_part, tmp_path):
    """token 换过 → 按保守策略重开会话（新 uniqueId），不从旧片续。"""
    p = _file(tmp_path, 5400)
    s = SessionStore(str(tmp_path / "t.db"))

    with pytest.raises(RuntimeError):
        _upload(s, p, w=FakeUpload(token="A" * 8, fail_at=3))
    old_uid = s.get("K9")["uid"]

    _, w2 = _upload(s, p, w=FakeUpload(token="B" * 8))
    assert s.get("K9")["uid"] != old_uid, "换 token 后必须是新会话"
    assert [c["idx"] for c in w2.calls] == [1, 2, 3, 4, 5, 6], "新会话要从头传"
    assert all(c["uid"] == s.get("K9")["uid"] for c in w2.calls)


def test_resumable_restarts_when_file_changes(small_part, tmp_path):
    """文件 mtime 变了（内容可能也变了）→ 旧会话判废，从头传。"""
    p = _file(tmp_path, 5400)
    s = SessionStore(str(tmp_path / "t.db"))
    with pytest.raises(RuntimeError):
        _upload(s, p, w=FakeUpload(fail_at=5))
    old_uid = s.get("K9")["uid"]

    os.utime(p, ns=(0, 999_999_999_999))       # 改 mtime，内容不变
    _, w2 = _upload(s, p)
    assert s.get("K9")["uid"] != old_uid
    assert [c["idx"] for c in w2.calls] == [1, 2, 3, 4, 5, 6]

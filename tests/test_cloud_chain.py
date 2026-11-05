#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务云端路径的解析。

`tasks.cloud` 落库是 JSON 字符串。对它直接 `"/".join(...)` 会把字符串按
字符拆开，得到 `[/"/A/l/b/u/m/...` 这种垃圾——2026-10-28 和 2026-11-05
各踩过一次。这里钉住解析规则，顺带钉住"展示时不该出现双斜杠"。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.store import cloud_chain  # noqa: E402


def show(task):
    """业务代码里展示用的拼法：段之间一个斜杠，首尾不带。"""
    return "/".join(x.strip("/") for x in cloud_chain(task))


def test_json_string_is_parsed_not_split():
    t = {"cloud": '["/AlbumBackup/", "/photos/"]'}
    assert cloud_chain(t) == ["/AlbumBackup/", "/photos/"]
    # 回归：以前这里是 "/".join(字符串)，结果是逐字符
    assert show(t) == "AlbumBackup/photos"
    assert "/A/l/b" not in show(t)


def test_already_parsed_list_passes_through():
    assert cloud_chain({"cloud": ["A", "B"]}) == ["A", "B"]


def test_empty_and_missing():
    assert cloud_chain({}) == []
    assert cloud_chain(None) == []
    assert cloud_chain({"cloud": None}) == []
    assert cloud_chain({"cloud": "[]"}) == []
    assert show({"cloud": ""}) == ""


def test_broken_json_degrades_to_empty():
    # 库里被手工改坏时不能抛异常，静静当空处理，任务会被标"云端目录不存在"
    assert cloud_chain({"cloud": "not-json"}) == []
    assert cloud_chain({"cloud": '{"a":1}'}) == []


def test_non_string_entries_are_stringified():
    assert cloud_chain({"cloud": '["a", 1, null, ""]'}) == ["a", "1"]

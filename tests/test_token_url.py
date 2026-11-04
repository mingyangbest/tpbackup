#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取令牌地址（TP_TOKEN_URL）的规整。

这个值会被前端拿去填 href，所以它不是普通配置：填成 javascript: 之类
就成了一条注入路径。这里钉住「只放行 http(s)，其余一律回默认」。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.store import TOKEN_URL_DEFAULT, token_url  # noqa: E402


def test_default_when_env_missing(monkeypatch):
    monkeypatch.delenv("TP_TOKEN_URL", raising=False)
    assert token_url() == TOKEN_URL_DEFAULT


def test_custom_https_and_http(monkeypatch):
    for u in ("https://example.com/token", "http://192.168.1.9:9000/t"):
        monkeypatch.setenv("TP_TOKEN_URL", u)
        assert token_url() == u


def test_other_schemes_fall_back():
    # 任何非 http(s) 的都不能漏到前端
    for bad in ("javascript:alert(1)", "data:text/html,<script>", "//evil.com", "vbscript:x", ""):
        assert token_url(bad) == TOKEN_URL_DEFAULT


def test_whitespace_is_trimmed(monkeypatch):
    monkeypatch.setenv("TP_TOKEN_URL", "  https://example.com/x  ")
    assert token_url() == "https://example.com/x"

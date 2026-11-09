#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""访问控制：按来源网段放行。

2026-11-09 起默认不要密码，唯一的安全边界就是这份白名单——所以它的
解析必须容错（写错一项不能把所有人锁在门外），判断必须严格（公网地址
不能被顺手放进来）。
"""
import os
import sys
import tempfile

# app.main 在 import 时会建库、建目录，先把它指到临时目录去
os.environ.setdefault("TP_DATA", tempfile.mkdtemp(prefix="tp-test-"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.main import ip_allowed, parse_cidrs  # noqa: E402

DEFAULT = "127.0.0.1,::1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"


def test_default_allows_loopback_and_private_lan():
    nets = parse_cidrs(DEFAULT)
    for ip in ("127.0.0.1", "192.168.124.4", "192.168.0.1", "10.1.2.3"):
        assert ip_allowed(ip, nets), ip


def test_docker_gateway_allowed():
    # 宿主机经 docker-proxy 访问时容器看到的源地址，必须在白名单里，
    # 否则 push.py / smoke_test.py 这类本机调用会被自己挡掉。
    nets = parse_cidrs(DEFAULT)
    for ip in ("172.17.0.1", "172.18.0.1", "172.31.255.254"):
        assert ip_allowed(ip, nets), ip


def test_public_addresses_rejected():
    nets = parse_cidrs(DEFAULT)
    for ip in ("8.8.8.8", "223.5.5.5", "100.64.0.1", "169.254.1.1"):
        assert not ip_allowed(ip, nets), ip


def test_empty_or_malformed_client_ip_rejected():
    nets = parse_cidrs(DEFAULT)
    for ip in ("", None, "not-an-ip", "192.168.1"):
        assert not ip_allowed(ip, nets), ip


def test_custom_cidr_limits_to_one_subnet():
    nets = parse_cidrs("192.168.1.0/24,127.0.0.1")
    assert ip_allowed("192.168.1.9", nets)
    assert ip_allowed("127.0.0.1", nets)
    assert not ip_allowed("192.168.2.9", nets)
    assert not ip_allowed("10.0.0.1", nets)


def test_typos_are_skipped_not_fatal():
    # 中间那个是非法网段，应被跳过；其余照常生效
    nets = parse_cidrs("999.1.1.1, ,192.168.5.0/24")
    assert len(nets) == 1
    assert ip_allowed("192.168.5.20", nets)


def test_bare_ip_without_prefix():
    nets = parse_cidrs("192.168.9.9")
    assert ip_allowed("192.168.9.9", nets)
    assert not ip_allowed("192.168.9.10", nets)


def test_ipv6_loopback():
    nets = parse_cidrs("::1")
    assert ip_allowed("::1", nets)
    assert not ip_allowed("fe80::1", nets)

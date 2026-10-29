#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""心跳推送：把每次备份的结果告诉 Uptime Kuma

为什么复用 Kuma 而不是自己发消息：
  备份失败需要一双外部的眼睛。Kuma 已有监控、告警策略、通知渠道这一整套，
  本应用只做一件事——每次运行结束往任务的 push URL 推一次状态，
  告警间隔、失败判定、发到哪，全部由 Kuma 那边配置，应用侧零状态。

  推送是「发后不理」：另起线程，失败只记日志，绝不影响备份本身。
"""
import logging
import threading
import urllib.parse
import urllib.request

log = logging.getLogger("tpbackup.notify")


def _push(url, ok, msg, ping_ms):
    sep = "&" if "?" in url else "?"
    q = "%s%sstatus=%s&msg=%s" % (url, sep, "up" if ok else "down",
                                  urllib.parse.quote(str(msg)[:180]))
    if ok and ping_ms:
        q += "&ping=%d" % int(ping_ms)
    try:
        req = urllib.request.Request(q, headers={"User-Agent": "tpbackup"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read(64)
    except Exception as e:
        log.warning("Kuma 心跳推送失败: %s", e)


def kuma(url, ok, msg="", ping_ms=0):
    """异步推送一次心跳。url 为空则什么都不做。"""
    if not url:
        return
    threading.Thread(target=_push, args=(url, ok, msg, ping_ms),
                     daemon=True).start()

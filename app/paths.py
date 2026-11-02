#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径白名单：所有「本地路径」入口的唯一校验口。

v0.2.0 里这段逻辑在 main.py 抄了四遍（任务源目录、文件浏览、推送、取回），
每处都写成 `real == r or real.startswith(r + "/")`。抄四遍的后果是口径一定
会走样：create_task 那处连 realpath 都没做，update_task 干脆一行校验都没有。

两个坑记在这里，别再各写一遍：

  · 必须 realpath —— 源目录里放一个指向 /etc 的软链，collect() 会照样跟进去
    （它只防循环链接，不防越界）；
  · 必须按路径段比较 —— 字符串前缀会让 /vol10/photos 被 /vol1 放行。

不要求路径存在：建任务时目录可能还没挂上来；realpath 对不存在的尾部只是
做规范化，仍然能挡住「已存在的软链段」造成的越界。
"""
import os

# 与 main.py 的 TP_ALLOWED_ROOTS 是同一个环境变量，这里只解析一次
ROOTS = [p for p in os.environ.get("TP_ALLOWED_ROOTS", "/vol1,/vol00").split(",") if p]


def within(path, roots=None):
    """path 落在白名单内则返回规范化后的真实路径，否则返回 None。"""
    real = os.path.realpath(path or "")
    for r in (ROOTS if roots is None else roots):
        root = os.path.realpath(r).rstrip("/") or "/"
        if real == root or real.startswith(root + "/"):
            return real
    return None

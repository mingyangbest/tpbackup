#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""孤儿扫描：云端有、本地没了的文件/目录。

只列清单、不删除——云端多余文件可能是改名、挪目录、本地误删留下的，
删之前必须由人先看清单。删除/镜像模式是下一步，刻意不在这里做。

语义：
- 以云端树为准递归：云端条目在本地没有同名（大小写不敏感）对应物即孤儿；
- 孤儿文件逐条记 {path, size}；孤儿目录整棵记一条，并用 walk_usage
  算出子树的文件数与字节数，不把子树每个文件都摊开；
- 双边都有的目录递归进入；任务的排除规则双边都适用（被排除的
  本地目录即使云端有对应物也不列为孤儿）；
- 扫描是只读的：云端根目录用 find_dir 解析，找不到不建目录。
"""
import os

from .engine import DEFAULT_EXCLUDE_DIRS, DEFAULT_EXCLUDE_FILES
from .engine import find_dir as engine_find_dir
from .engine import match_exclude, walk_usage


def scan_orphans(w, src, cloud_chain, excludes, log=lambda m: None,
                 max_entries=2000):
    """返回 {files, dirs, total_files, total_bytes, truncated, note}。

    files: [{path, size}]；dirs: [{path, files, bytes}]；
    total_* 是孤儿总量（含孤儿目录子树）；truncated 表示清单被截断。
    """
    files, dirs = [], []
    state = {"total_files": 0, "total_bytes": 0, "truncated": False}
    exd = set(DEFAULT_EXCLUDE_DIRS) | set(excludes or [])
    exf = set(DEFAULT_EXCLUDE_FILES) | set(excludes or [])

    cid = engine_find_dir(w, cloud_chain)
    if not cid:
        return {"files": [], "dirs": [], "total_files": 0, "total_bytes": 0,
                "truncated": False,
                "note": "云端目录不存在（可能从未备份过）"}

    def walk(local_dir, dirid, rel):
        try:
            local_names = {n.lower(): n for n in os.listdir(local_dir)}
        except OSError as e:
            log("无法列出本地目录 %s：%s" % (rel or local_dir, e))
            local_names = {}
        for f in w.ls(dirid):
            name = f.get("name") or ""
            is_dir = f.get("type") == 0
            if match_exclude(name, is_dir, exd if is_dir else exf):
                continue
            if name.lower() in local_names:
                if is_dir:
                    walk(os.path.join(local_dir, local_names[name.lower()]),
                         str(f.get("id")),
                         (rel + "/" + name) if rel else name)
                continue
            if len(files) + len(dirs) >= max_entries:
                state["truncated"] = True
                continue
            path = (rel + "/" + name) if rel else name
            if is_dir:
                n, b = walk_usage(w, str(f.get("id")))
                dirs.append({"path": path, "files": n, "bytes": b})
                state["total_files"] += n
                state["total_bytes"] += b
            else:
                try:
                    sz = int(f.get("size") or 0)
                except Exception:
                    sz = 0
                files.append({"path": path, "size": sz})
                state["total_files"] += 1
                state["total_bytes"] += sz

    walk(src, cid, "")
    return {"files": files, "dirs": dirs,
            "total_files": state["total_files"],
            "total_bytes": state["total_bytes"],
            "truncated": state["truncated"], "note": ""}

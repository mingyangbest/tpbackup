#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询 tpbackup 里某个任务最近一次运行的状态。

供 check.sh 巡检使用：原来是读各脚本的 *_status.log 文件，
统一到应用后，运行记录在应用的数据库里，这里是读取入口。

输出（单行，空格分隔）：<状态> <距今小时数> <完成时间>
  例：success 5 2026-10-28 00:14:50
  从未运行过：none -1 -
退出码：0 正常查询到；1 任务不存在或应用不可达（stderr 给出原因）
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--conf", default=os.path.join(HERE, "api.conf"))
    a = ap.parse_args()

    try:
        conf = {}
        for line in open(a.conf, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
        base = conf["API"].rsplit("/api/", 1)[0]
    except Exception as e:
        print("凭据读取失败: %s" % e, file=sys.stderr)
        return 1

    tok = base64.b64encode(("%s:%s" % (conf.get("API_USER", ""),
                                       conf.get("API_PASS", ""))).encode()).decode()
    try:
        req = urllib.request.Request(base + "/api/tasks",
                                     headers={"Authorization": "Basic " + tok})
        with urllib.request.urlopen(req, timeout=60) as r:
            tasks = json.loads(r.read().decode())
    except Exception as e:
        print("应用不可达: %s" % e, file=sys.stderr)
        return 1

    t = next((x for x in tasks if x.get("name") == a.task), None)
    if not t:
        print("任务不存在: %s" % a.task, file=sys.stderr)
        return 1

    # 2026-10-28 修正：只认「真实运行」。预览(dry-run)不传输任何数据，
    # 若拿它当最近一次状态，用户当天点过一次预览就会让每日巡检误报「备份异常」。
    last = None
    tid = t.get("id")
    if tid is not None:
        try:
            req = urllib.request.Request(
                base + "/api/runs?task_id=%d&limit=50" % int(tid),
                headers={"Authorization": "Basic " + tok})
            with urllib.request.urlopen(req, timeout=60) as r:
                runs = json.loads(r.read().decode())
            for x in runs or []:
                if (x.get("status") or "") != "dry-run":
                    last = x
                    break
        except Exception:
            last = None
    if last is None:
        cand = t.get("last") or {}
        if (cand.get("status") or "") != "dry-run":
            last = cand
    if not last:
        print("none -1 -")
        return 0

    fin = last.get("finished_at") or last.get("started_at") or ""
    h = -1.0
    if fin:
        try:
            h = round((time.time() - time.mktime(time.strptime(fin, "%Y-%m-%d %H:%M:%S"))) / 3600.0, 1)
        except Exception:
            h = -1.0
    print("%s %s %s" % (last.get("status") or "unknown", h, fin or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

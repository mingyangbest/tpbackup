#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""恢复演练：从云端随机抽样，真的取回来校验。

为什么需要它：界面上「成功」只证明传上去了，不证明取得回来。
每月抽几个文件真下载一遍，比盯一百次绿色勾更让人踏实。

用法：
  python3 drill.py                 # 默认抽 5 个，覆盖尽量多的任务
  python3 drill.py --samples 8
  python3 drill.py --task 4        # 只演练某个任务
输出：
  health/drill.json  最近一次结果（check.sh 会读它判断是否过期/失败）
  health/drill.log   追加式流水
  演练完还会把同一份报告 POST 到管理台 /api/drill/report（面板展示用），
  上报失败只记 warning，绝不改变演练退出码
退出码：0 全部通过；1 有失败
"""
import argparse
import base64
import hashlib
import json
import os
import random
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 项目根目录：默认 tools/ 的上一级，可用环境变量 TP_DIR 覆盖
HERE = os.environ.get("TP_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(HERE, "health", "drill-tmp")
REPORT = os.path.join(HERE, "health", "drill.json")
LOG = os.path.join(HERE, "health", "drill.log")


def conf():
    c = {}
    for line in open(os.path.join(HERE, "api.conf"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            c[k.strip()] = v.strip()
    return c["API"].rsplit("/api/", 1)[0], c.get("API_USER", ""), c.get("API_PASS", "")


def log(msg):
    line = "%s %s" % (time.strftime("%F %T"), msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class Api:
    def __init__(self, base, user, pw):
        self.base = base
        self.tok = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()

    def call(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": "Basic " + self.tok,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw.decode()) if raw else None)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, None


def walk_files(api, task_id, path="", depth=0, out=None, rnd=None):
    """递归收集云端文件（最多 7 层、120 个就够抽样了）。
    相册那种 MobileBackup/iPhone/年/月/日 的结构有 5~6 层，层数给小了会一个都捞不到。"""
    out = out if out is not None else []
    if depth > 7 or len(out) >= 120:
        return out
    st, d = api.call("GET", "/api/cloud/ls?task_id=%d&path=%s" % (task_id, urllib.parse.quote(path)))
    if st != 200 or not d:
        return out
    entries = d.get("entries") or []
    dirs = [e for e in entries if e.get("dir")]
    files = [e for e in entries if not e.get("dir") and (e.get("size") or 0) >= 1024]
    for e in files:
        out.append({"task_id": task_id, "path": (path + "/" + e["name"]) if path else e["name"],
                    "name": e["name"], "size": e["size"]})
    rnd.shuffle(dirs)
    for e in dirs[:8]:
        walk_files(api, task_id, (path + "/" + e["name"]) if path else e["name"], depth + 1, out, rnd)
        if len(out) >= 120:
            break
    return out


def wait_job(api, jid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, d = api.call("GET", "/api/restore/" + jid)
        if st == 200 and d and d.get("state") in ("done", "failed"):
            return d
        time.sleep(3)
    return {"state": "timeout", "error": "等待超时"}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def hash_check(api, task_by_id, it, out):
    """体积通过后再比 SHA256。云端没给哈希时记 unknown，不推翻体积结论；
    哈希不一致则该样本判失败。返回要并入结果记录的字段。"""
    t = task_by_id.get(it["task_id"]) or {}
    chain = list(t.get("cloud") or [])
    parts = [p for p in it["path"].split("/") if p]
    chain += parts[:-1]
    name = parts[-1] if parts else it["name"]
    try:
        st, d = api.call("GET", "/api/jobs/stat?cloud=%s&name=%s" % (
            urllib.parse.quote("/".join(chain)), urllib.parse.quote(name)))
    except Exception as e:
        return {"hash": "unknown", "hash_note": "查询失败：%s" % e}
    cloud_hash = (d or {}).get("sha256") if st == 200 else None
    if not cloud_hash:
        return {"hash": "unknown"}
    try:
        local = sha256_file(out)
    except Exception as e:
        return {"hash": "unknown", "hash_note": "本地算哈希失败：%s" % e}
    if local == cloud_hash:
        return {"hash": "ok"}
    return {"hash": "mismatch", "ok": False, "error": "哈希不符"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--task", type=int, default=0, help="只演练该任务 id")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    rnd = random.Random(a.seed or time.time())
    base, user, pw = conf()
    api = Api(base, user, pw)

    st, tasks = api.call("GET", "/api/tasks")
    if st != 200 or not isinstance(tasks, list):
        log("读取任务列表失败（%s）" % st)
        return 1
    tasks = [t for t in tasks if t.get("enabled") and (not a.task or t["id"] == a.task)]
    if not tasks:
        log("没有可演练的任务")
        return 1
    task_by_id = {t["id"]: t for t in tasks}

    log("===== 恢复演练开始：候选任务 %d 个，抽样 %d 个 =====" % (len(tasks), a.samples))
    cand = {}
    for t in tasks:
        fs = walk_files(api, t["id"], "", 0, [], rnd)
        log("  任务「%s」云端可抽样文件 %d 个" % (t["name"], len(fs)))
        if fs:
            rnd.shuffle(fs)
            cand[t["id"]] = fs
    if not cand:
        log("云端没有可抽样的文件，演练无法进行")
        return 1

    # 轮转取：每个任务先各来一个，再回头拿第二个，保证覆盖面而不是全压在一个任务上
    picked = []
    round_i = 0
    while len(picked) < a.samples and round_i < 50:
        got = False
        for tid, fs in cand.items():
            if len(picked) >= a.samples:
                break
            if round_i < len(fs):
                picked.append(fs[round_i])
                got = True
        if not got:
            break
        round_i += 1

    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    results, ok_n = [], 0
    for i, it in enumerate(picked):
        dest = os.path.join(TMP, "s%d" % i)
        os.makedirs(dest, exist_ok=True)
        st, r = api.call("POST", "/api/restore",
                         {"task_id": it["task_id"], "path": it["path"], "dest": dest})
        rec = {"task_id": it["task_id"], "path": it["path"], "expect": it["size"]}
        if st != 200 or not r:
            rec.update({"ok": False, "error": "提交失败 %s %s" % (st, (r or {}).get("detail", ""))})
        else:
            j = wait_job(api, r["id"])
            if j.get("state") != "done":
                rec.update({"ok": False, "error": j.get("error") or j.get("state")})
            else:
                out = os.path.join(dest, it["name"])
                got = os.path.getsize(out) if os.path.exists(out) else -1
                rec.update({"ok": got == it["size"], "got": got})
                if got != it["size"]:
                    rec["error"] = "体积不符：期望 %s，实际 %s" % (it["size"], got)
                else:
                    # 体积通过后、临时目录清理前，再比一次 SHA256
                    rec.update(hash_check(api, task_by_id, it, out))
        ok_n += 1 if rec.get("ok") else 0
        log("  [%s] %s :: %s（%s 字节）%s%s" % (
            "OK" if rec.get("ok") else "FAIL", it.get("path"), it["name"], it["size"],
            "" if rec.get("ok") else " -> " + str(rec.get("error")),
            (" [hash:%s]" % rec["hash"]) if rec.get("hash") else ""))
        results.append(rec)

    shutil.rmtree(TMP, ignore_errors=True)
    report = {"at": time.strftime("%F %T"), "samples": len(picked),
              "ok": ok_n, "fail": len(picked) - ok_n, "results": results,
              "hash_ok": sum(1 for r in results if r.get("hash") == "ok"),
              "hash_mismatch": sum(1 for r in results if r.get("hash") == "mismatch")}
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    log("===== 演练结束：通过 %d / %d，哈希 ok %d / 不符 %d（报告 %s）=====" % (
        ok_n, len(picked), report["hash_ok"], report["hash_mismatch"], REPORT))
    try:
        st, _ = api.call("POST", "/api/drill/report", report)
        if st != 200:
            log("演练报告上报面板失败（HTTP %s），本地报告不受影响" % st)
    except Exception as e:
        log("演练报告上报面板失败（%s），本地报告不受影响" % e)
    return 0 if ok_n == len(picked) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("演练异常：%s" % e)
        sys.exit(1)

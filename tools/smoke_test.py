#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tpbackup 每日冒烟测试

完整版在源码库 tests/api_test.py（19 项）。这里挑最关键、且不需要写入任何
真实任务的 7 项，供 check.sh 每天跑一遍 —— 核心逻辑（引擎、调度、校验）改动后
如果有回归，第二天就会被巡检抓住，而不是等到第一次真实备份失败。

退出码：0 全过；1 有失败（check.sh 会转成告警）
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.environ.get("TP_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))   # 默认 = tools/ 的上一级（项目根）
CONTAINER = os.environ.get("TP_CONTAINER", "tpbackup")   # 容器名，可用环境变量覆盖
PASS, FAIL = 0, []


def conf():
    c = {}
    for line in open(os.path.join(HERE, "api.conf"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            c[k.strip()] = v.strip()
    return c["API"], c.get("API_USER", ""), c.get("API_PASS", "")


def req(method, path, body=None, auth=True):
    api, user, pw = conf()
    h = {"Content-Type": "application/json"}
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(
            ("%s:%s" % (user, pw)).encode()).decode()
    r = urllib.request.Request(api.rsplit("/api/", 1)[0] + path,
                               data=json.dumps(body).encode() if body is not None else None,
                               headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def get_json(path):
    """GET 并解析 JSON。"""
    api, user, pw = conf()
    h = {"Authorization": "Basic " + base64.b64encode(
        ("%s:%s" % (user, pw)).encode()).decode()}
    with urllib.request.urlopen(
            urllib.request.Request(api.rsplit("/api/", 1)[0] + path, headers=h),
            timeout=20) as resp:
        return json.loads(resp.read().decode())


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print("OK   %s" % name)
    else:
        FAIL.append("%s %s" % (name, detail))
        print("FAIL %s %s" % (name, detail))


def main():
    print("── tpbackup 冒烟测试 ──")

    # 允许根从服务端取，不写死路径：穿越/越权用例都要构造「允许根内」的路径
    try:
        roots = [e["path"] for e in get_json("/api/fs/list").get("entries", [])
                 if e.get("root")]
    except Exception:
        roots = []
    root = roots[0] if roots else ""
    check("允许根已配置", bool(root),
          "TP_ALLOWED_ROOTS 指向的目录都不存在？或服务不可达")
    if not root:
        print("── 没有可用允许根，中止 ──")
        return 1

    check("无凭据被拒(401)", req("GET", "/api/tasks", auth=False) == 401)
    check("路径穿越被拒(403)", req("GET", "/api/fs/list?path=/etc") == 403)
    check("编码穿越被拒(403)",
          req("GET", "/api/fs/list?path=" + urllib.parse.quote(root + "/../etc")) == 403)
    check("push 越权路径(403)",
          req("POST", "/api/jobs/push",
              {"task": "smoke", "cloud": ["X"], "files": [{"path": "/etc/passwd"}]}) == 403)
    check("push 缺文件(404)",
          req("POST", "/api/jobs/push",
              {"task": "smoke", "cloud": ["X"],
               "files": [{"path": root + "/__no__.bin"}]}) == 404)
    check("无效 cron 保存被拒(400)",
          req("POST", "/api/tasks",
              {"name": "zz-smoke", "src": root,
               "cloud": ["ZZSmoke"], "cron": "not-a-cron"}) == 400)
    check("运行不存在任务(404)", req("POST", "/api/tasks/999999/run") == 404)

    # 关键数据接口必须存在（2026-10-28 事故：一次文件覆盖导致 /api/stats/transfer
    # 整个路由消失，前端 KPIs 与趋势图全空，而冒烟测试当时只查了任务类接口）
    check("统计接口可用(/api/stats/transfer)", req("GET", "/api/stats/transfer?days=7") == 200)
    check("容量接口可用(/api/stats/storage)", req("GET", "/api/stats/storage") == 200)

    # ── 静态检查：未定义名字 / 未用导入 ──
    # 2026-10-28 事故：verify 变量未定义，py_compile 查不出（NameError 是运行时
    # 错误），dry-run 又走不到那段代码，直到当天第一次定时真实上传才炸。
    # pyflakes 能在静态阶段抓住这一整类问题。
    try:
        r = subprocess.run(
            ["docker", "exec", CONTAINER, "python3", "-m", "pyflakes",
             "/app/app/engine.py", "/app/app/main.py", "/app/app/store.py",
             "/app/app/runner.py", "/app/app/scheduler.py", "/app/app/notify.py"],
            capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip()
        # pyflakes 在开发依赖里（requirements-dev.txt），运行时容器没装，跳过不算失败
        if "No module named pyflakes" in out:
            print("SKIP pyflakes（容器内未安装；开发机 pip install -r requirements-dev.txt）")
        else:
            check("pyflakes 静态检查（未定义名字等）", r.returncode == 0, out[:200])
    except FileNotFoundError:
        print("SKIP pyflakes（docker 不可用？仅限在 NAS 宿主机上运行）")
    except subprocess.TimeoutExpired:
        check("pyflakes 静态检查", False, "超时")

    print("── 结果：通过 %d，失败 %d ──" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 tpbackup 持有的联通云盘 token 同步给 OpenList，让两边共用一个凭证。

背景
  OpenList 的 WoPan 存储只有一份 token 在内存里，它启动时读一次数据库，
  之后本应用刷新了 token，它并不知道 —— 于是继续用旧 token，直到报 400/403。
  这就是 10-21 起 OpenList 那条链路全线失败的原因。

分工（重要）
  tpbackup  = 唯一权威刷新者。它提前 2 天主动续期，刷新后写回 OpenList 库。
  本脚本    = 兜底 + 让 OpenList 重新加载。每 10 分钟跑一次：
              1) 若库里 token 与 tpbackup 不同 → 写入
              2) 若 access_token 与「上次已应用」的不同 → 重启 OpenList 让它重读

  为什么用重启：OpenList 没有免鉴权的「重载存储」接口，重启是唯一可靠方式。
  只在 token 真正变化时才重启（约 5~7 天一次），不影响日常浏览。

安全阀
  若 tpbackup 的 token 距签发已超过 6.9 天（接近 7 天有效期），说明它自己也很久
  没能续期了，这时**不覆盖** OpenList 里的副本 —— 免得把还能用的凭证写成废的。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

# 路径因机器而异，全部可用环境变量覆盖；默认值按飞牛 NAS 的通用布局
_TP_DIR = os.environ.get("TP_DIR", "/vol1/docker/tpbackup")
TOKEN = os.environ.get("TPB_TOKEN", os.path.join(_TP_DIR, "data/state/token.json"))
OPENLIST_DB = os.environ.get("TPB_OPENLIST_DB", "/vol1/docker/openlist/data/data.db")
STATE = os.environ.get("TPB_STATE",
                       os.path.join(_TP_DIR, "data/state/openlist_applied"))
LOG = os.environ.get("TPB_SYNC_LOG", os.path.join(_TP_DIR, "health/openlist_sync.log"))
TTL = 604799          # 联通云盘 token 有效期 7 天
GUARD = 6.9 * 86400   # 超过这个年龄就不覆盖对方


def log(msg):
    """同时打到 stdout 与日志文件。文件内置轮转，避免每 10 分钟一行无限增长。"""
    line = "%s %s" % (time.strftime("%F %T"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(LOG) > 120000:
            keep = open(LOG, encoding="utf-8").read().splitlines()[-400:]
            open(LOG, "w", encoding="utf-8").write("\n".join(keep) + "\n")
    except Exception:
        pass


def read_local():
    try:
        d = json.load(open(TOKEN, encoding="utf-8"))
        return d.get("access_token") or "", d.get("refresh_token") or "", d.get("updated") or ""
    except Exception as e:
        log("读取 %s 失败: %s" % (TOKEN, e))
        return "", "", ""


def read_db():
    try:
        c = sqlite3.connect("file:%s?mode=ro" % OPENLIST_DB, uri=True)
        row = c.execute("select addition from x_storages where id=1").fetchone()
        c.close()
        if not row:
            return None
        return json.loads(row[0] or "{}")
    except Exception as e:
        log("读取 OpenList 库失败: %s" % e)
        return None


def write_db(addition):
    c = sqlite3.connect(OPENLIST_DB, timeout=20)
    try:
        c.execute("update x_storages set addition=?, status='' where id=1",
                  (json.dumps(addition, ensure_ascii=False),))
        c.commit()
    finally:
        c.close()


def applied_hash():
    try:
        return open(STATE, encoding="utf-8").read().strip()
    except Exception:
        return ""


def save_hash(h):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            f.write(h)
    except Exception as e:
        log("写状态文件失败: %s" % e)


def restart_openlist():
    r = subprocess.run(["docker", "restart", "openlist"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        log("重启 OpenList 失败: %s" % (r.stderr or r.stdout).strip()[:200])
        return False
    return True


def check():
    """只自检不写库，供月度巡检调用。

    一行输出，退出码 0=正常、1=有问题，方便 check.sh 直接用。
    """
    at, rt, updated = read_local()
    add = read_db()
    if not at:
        print("tpbackup 无 token")
        return 1
    if add is None:
        print("读不到 OpenList 存储配置")
        return 1

    problems = []
    if (add.get("access_token") or "") != at:
        problems.append("两边 token 不一致")
    st = (add.get("status") or "").strip()
    if st and st != "work":
        problems.append("存储状态异常「%s」" % st[:60])
    if not (add.get("refresh_token") or ""):
        problems.append("OpenList 侧缺 refresh_token")

    age_h = -1.0
    if updated:
        try:
            age_h = (time.time() - time.mktime(
                time.strptime(updated, "%Y-%m-%d %H:%M:%S"))) / 3600.0
        except Exception:
            age_h = -1.0
    if age_h < 0:
        problems.append("token 签发时间无法解析")
    elif age_h > 7 * 24:
        problems.append("token 已签发 %.1f 天，疑似长期未续期" % (age_h / 24.0))

    if problems:
        print("；".join(problems))
        return 1
    print("一致，remaining≈%.1f天，存储=%s" % (7 - age_h / 24.0, st or "work"))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只自检不写入")
    a = ap.parse_args()
    if a.check:
        return check()

    at, rt, updated = read_local()
    if not at:
        log("tpbackup 还没有 token，跳过")
        return 0

    age = -1
    if updated:
        try:
            age = time.time() - time.mktime(time.strptime(updated, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            age = -1

    add = read_db()
    if add is None:
        log("OpenList 库读不到存储配置，跳过")
        return 1

    db_at = add.get("access_token") or ""
    changed = False

    if db_at != at:
        if age > GUARD:
            log("tpbackup 的 token 已签发 %.1f 天（接近过期），不覆盖 OpenList 里的副本" % (age / 86400.0))
        else:
            add["access_token"] = at
            add["refresh_token"] = rt
            write_db(add)
            log("已把 token 写入 OpenList（签发于 %s，%.1f 天前）" % (updated, age / 86400.0))
            changed = True
    else:
        log("两边 token 一致（签发于 %s）" % updated)

    # 让 OpenList 重新加载：仅当 access_token 与上次已应用的不同
    h = at[-12:]
    if applied_hash() != h:
        if restart_openlist():
            save_hash(h)
            log("OpenList 已重启以加载新 token")
        else:
            log("OpenList 重启失败，下次再试")
            return 1
    elif changed:
        log("token 内容未变（只是 refresh 更新），无需重启")
    return 0


if __name__ == "__main__":
    sys.exit(main())

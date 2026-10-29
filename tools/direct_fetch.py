#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不依赖容器的直连取件

为什么单独做这个：
  push.py / fetch.py 走的是 tpbackup 的 HTTP 接口，前提是容器在跑。
  但**灾难恢复时容器往往还没起来**——比如 NAS 重装后只想先把云端包拉下来看看。
  这个脚本直接读本机 token，用联通云盘协议取文件，只需要宿主机 python3，
  不需要 tpbackup 容器、不需要 OpenList、不需要 rclone。

依赖：宿主机 python3 + cryptography（旧直连脚本一直在用，已具备）

用法：
  direct_fetch.py --cloud HomeNetBackup --name homenet-config.tar.gz.enc --stat
  direct_fetch.py --cloud HomeNetBackup --name homenet-config.tar.gz.enc --out /tmp/a.enc

退出码：0 成功；1 失败；2 参数问题
"""
import argparse
import os
import sys

TP_DIR = os.environ.get("TP_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TP_DIR)

try:
    from app.engine import Wopan, find_file, download_file
    from app.store import TokenStore
except Exception as e:  # 缺 cryptography 之类
    print("无法加载直连引擎: %s" % e, file=sys.stderr)
    sys.exit(2)

TOKEN = os.path.join(TP_DIR, "data", "state", "token.json")


def fmt_time(s):
    s = str(s or "")
    if len(s) == 14 and s.isdigit():
        return "%s-%s-%s %s:%s:%s" % (s[0:4], s[4:6], s[6:8], s[8:10], s[10:12], s[12:14])
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud", required=True, help="云端目录，支持 A/B 形式")
    ap.add_argument("--name", required=True, help="云端文件名")
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    if not a.stat and not a.out:
        print("必须指定 --stat 或 --out", file=sys.stderr)
        return 2
    if not os.path.isfile(TOKEN):
        print("找不到 token：%s（NAS 重装后需重新授权）" % TOKEN, file=sys.stderr)
        return 1

    logs = []
    st = TokenStore(TOKEN, openlist_db=None, log=logs.append)
    at, rt = st.load()
    if not at:
        print("token 文件里没有可用凭证", file=sys.stderr)
        return 1

    w = Wopan(at, rt, on_token=st.save)
    try:
        w.ensure_family()
    except Exception as e:
        # 凭证可能过期：尝试续期后重试一次
        logs.append("首次连接失败：%s" % e)
        if not st.recover(w):
            for l in logs:
                print("  " + l, file=sys.stderr)
            print("云端连接失败: %s" % str(e)[:150], file=sys.stderr)
            return 1

    chain = [s for s in a.cloud.split("/") if s]
    try:
        item = find_file(w, chain, a.name)
    except Exception as e:
        print("查询失败: %s" % str(e)[:150], file=sys.stderr)
        return 1
    if not item:
        print("云端没有该文件: %s/%s" % ("/".join(chain), a.name), file=sys.stderr)
        return 1

    if a.stat:
        t = fmt_time(item.get("modifyTime") or item.get("createTime"))
        print("%s %s.000000000 %s" % (item.get("size"), t, item.get("name")))
        return 0

    tmp = a.out + ".part"
    try:
        n = download_file(w, item, tmp)
    except Exception as e:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        print("下载失败: %s" % str(e)[:150], file=sys.stderr)
        return 1
    os.replace(tmp, a.out)
    print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

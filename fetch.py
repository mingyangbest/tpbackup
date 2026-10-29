#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主机取件客户端：向 tpbackup 查询/下载云端文件。

替代原来经 OpenList 的 rclone lsl / rclone copyto，让巡检脚本不再依赖 OpenList。

用法：
  # 查文件信息（输出格式对齐 rclone lsl，便于复用原有 awk 解析）
  fetch.py --stat --cloud HomeNetBackup --name homenet-config.tar.gz.enc
  → 17170560 2026-10-28 00:14:50.000000000 homenet-config.tar.gz.enc

  # 下载
  fetch.py --out /tmp/pkg.enc --cloud HomeNetBackup --name homenet-config.tar.gz.enc

退出码：0 成功；1 文件不存在或下载失败；2 参数/凭据问题
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_conf(path):
    conf = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    # API 形如 http://127.0.0.1:8088/api/jobs/push，取出 base
    api = conf.get("API", "")
    if api.endswith("/api/jobs/push"):
        conf["BASE"] = api[: -len("/api/jobs/push")]
    else:
        conf["BASE"] = api.rsplit("/api/", 1)[0] if "/api/" in api else api
    return conf


def auth_header(conf):
    tok = base64.b64encode(("%s:%s" % (conf.get("API_USER", ""),
                                       conf.get("API_PASS", ""))).encode()).decode()
    return {"Authorization": "Basic " + tok}


def fmt_time(s):
    s = str(s or "")
    if len(s) == 14 and s.isdigit():
        return "%s-%s-%s %s:%s:%s" % (s[0:4], s[4:6], s[6:8], s[8:10], s[10:12], s[12:14])
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--stat", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--conf", default=os.path.join(HERE, "api.conf"))
    a = ap.parse_args()

    if not a.stat and not a.out:
        print("必须指定 --stat 或 --out", file=sys.stderr)
        return 2
    if not os.path.isfile(a.conf):
        print("缺少凭据文件 %s" % a.conf, file=sys.stderr)
        return 2
    try:
        conf = load_conf(a.conf)
        base = conf["BASE"]
    except Exception as e:
        print("凭据文件不完整: %s" % e, file=sys.stderr)
        return 2

    q = urllib.parse.urlencode({"cloud": a.cloud, "name": a.name})
    hdr = auth_header(conf)

    if a.stat:
        url = "%s/api/jobs/stat?%s" % (base, q)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr),
                                        timeout=120) as r:
                d = json.loads(r.read().decode())
        except Exception as e:
            print("查询失败: %s" % e, file=sys.stderr)
            return 1
        if not d.get("exists"):
            return 1
        # 必须用 modify_time：同名覆盖上传不会更新 create_time，
        # 用 create_time 判断新鲜度会把刚传上去的包误报成很久以前的。
        t = fmt_time(d.get("modify_time") or d.get("create_time"))
        print("%s %s.000000000 %s" % (d.get("size"), t, d.get("name")))
        return 0

    url = "%s/api/jobs/file?%s" % (base, q)
    tmp = a.out + ".part"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr),
                                    timeout=1800) as r, open(tmp, "wb") as f:
            n = 0
            while True:
                b = r.read(262144)
                if not b:
                    break
                f.write(b)
                n += len(b)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
            msg = json.loads(body).get("detail") or body
        except Exception:
            msg = "HTTP %s" % e.code
        print("下载失败: %s" % msg, file=sys.stderr)
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return 1
    except Exception as e:
        print("下载失败: %s" % e, file=sys.stderr)
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return 1

    os.replace(tmp, a.out)
    print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

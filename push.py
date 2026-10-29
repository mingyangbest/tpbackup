#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主备份脚本的上传客户端

把已打包好的文件交给 tpbackup 上传，并回显应用侧的日志。
宿主机脚本只管打包，传输、体积复核、云端保留、运行记录全部由应用完成。

用法：
  push.py --task "Vaultwarden 密码库" --cloud VaultwardenBackup \\
          --keep 14 --pattern 'vaultwarden-*.tar.gz' -- \
          /path/README.txt /path/vaultwarden-20261028T034001.tar.gz

  --conf    凭据文件，默认与本脚本同目录的 api.conf
  --dry-run 只让应用校验文件，不真正上传
退出码：0 成功；1 上传失败；2 参数/凭据问题
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
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
    return conf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--cloud", required=True)
    ap.add_argument("--keep", type=int, default=0)
    ap.add_argument("--pattern", default="")
    ap.add_argument("--conf", default=os.path.join(HERE, "api.conf"))
    ap.add_argument("--name", default="", help="云端文件名（仅单文件时可用）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()

    if a.name and len(a.files) != 1:
        print("PUSH_FAIL --name 只能用于单个文件", file=sys.stderr)
        return 2

    if not os.path.isfile(a.conf):
        print("PUSH_FAIL 缺少凭据文件 %s" % a.conf, file=sys.stderr)
        return 2
    try:
        conf = load_conf(a.conf)
        api = conf["API"]
    except Exception as e:
        print("PUSH_FAIL 凭据文件不完整: %s" % e, file=sys.stderr)
        return 2

    body = json.dumps({
        "task": a.task, "cloud": [a.cloud],
        "files": [{"path": os.path.abspath(p), "name": (a.name or None)} for p in a.files],
        "keep": a.keep, "pattern": a.pattern, "dry_run": bool(a.dry_run),
    }, ensure_ascii=False).encode()

    auth = base64.b64encode(("%s:%s" % (conf.get("API_USER", ""),
                                        conf.get("API_PASS", ""))).encode()).decode()
    req = urllib.request.Request(
        api, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})

    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
        except Exception:
            print("PUSH_FAIL HTTP %s" % e.code, file=sys.stderr)
            return 1
    except Exception as e:
        print("PUSH_FAIL 应用不可达: %s" % e, file=sys.stderr)
        return 1

    for line in d.get("log_tail") or []:
        print("    " + line)

    if d.get("ok"):
        print("    PUSH_OK 文件=%s 字节=%s 清理旧包=%s 耗时=%.1fs" % (
            d.get("ok_n"), d.get("bytes"), d.get("pruned"), d.get("elapsed") or 0))
        return 0
    print("    PUSH_FAIL %s %s" % (d.get("reason"), d.get("detail")))
    return 1


if __name__ == "__main__":
    sys.exit(main())

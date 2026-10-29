# -*- coding: utf-8 -*-
"""verify 代码路径的端到端测试（真实上传，非 dry-run）。

2026-10-28 事故：verify 未定义导致 NameError，dry-run 走不到该行所以逃过
所有测试。本脚本专测真实上传 → 复核块执行 → SHA256 一致 → 清理，全链路。
"""
import os
import sys
import time

sys.path.insert(0, "/app")
from app.engine import Wopan, delete_entries, sync  # noqa: E402
from app.store import TokenStore  # noqa: E402

SRC = "/tmp/verify-e2e"
CLOUD = "_verify_e2e_test"

# 1) 准备临时文件（几 KB，≤64MB → 会走 SHA256 校验分支）
os.makedirs(SRC, exist_ok=True)
with open(os.path.join(SRC, "hello.txt"), "wb") as f:
    f.write(b"tpbackup verify e2e %s" % str(time.time()).encode())

# 2) 真实同步到一次性云端目录
logs = []
res = sync(store=TokenStore("/app/data/state/token.json", log=lambda m: None),
           src=SRC, cloud_chain=[CLOUD], dry_run=False, workers=2,
           log=logs.append)
print("结果: ok=%s todo=%s ok_n=%s fail=%s reason=%r"
      % (res["ok"], res["todo"], res["ok_n"], res["fail"], res["reason"]))
for l in logs:
    print("  |", l)

ok = (res["ok"] and res["todo"] == 1
      and any("复核通过" in l for l in logs)
      and any("SHA256 一致 1 个" in l for l in logs))
print("校验块执行且 SHA256 一致:", "✅" if ok else "❌")

# 3) 清理云端测试目录（留一句日志证明清理动作）
try:
    st = TokenStore("/app/data/state/token.json", log=lambda m: None)
    at, rt = st.load()
    w = Wopan(at, rt)
    w.ensure_family()
    for f in w.ls("0"):
        if f.get("name") == CLOUD and f.get("type") == 0:
            delete_entries(w, dir_ids=[f.get("id")])
            print("云端测试目录已删除:", CLOUD)
            break
except Exception as e:
    print("⚠ 云端清理失败（不影响测试结论，需手工删 %s）: %s" % (CLOUD, e))

os.remove(os.path.join(SRC, "hello.txt"))
os.rmdir(SRC)
print("本地临时文件已清理")

sys.exit(0 if ok else 1)

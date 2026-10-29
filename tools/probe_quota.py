import base64
import json
import sys

sys.path.insert(0, "/app")
from app.engine import CLIENT_ID, Wopan  # noqa: E402
from app.store import TokenStore  # noqa: E402

st = TokenStore("/app/data/state/token.json", log=lambda m: None)
at, rt = st.load()

# 1) 从 JWT 里找手机号线索
print("== token 载荷字段 ==")
try:
    parts = at.split(".")
    if len(parts) == 3:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        pl = json.loads(base64.urlsafe_b64decode(pad))
        print("  keys:", sorted(pl.keys()))
        for k in pl:
            if any(s in k.lower() for s in ("phone", "mobile", "user", "name", "id")):
                v = str(pl[k])
                print("  %s = %s" % (k, v[:30]))
    else:
        print("  非 JWT，长度", len(at))
except Exception as e:
    print("  JWT 解析失败:", e)

w = Wopan(at, rt)
w.ensure_family()

# 2) 试常见用户信息接口，找手机号
print("\n== 试探用户信息接口 ==")
for m, p in (("QueryUserInfo", {"clientId": CLIENT_ID}),
             ("GetUserInfo", {"clientId": CLIENT_ID}),
             ("QueryUserBasicInfo", {"clientId": CLIENT_ID}),
             ("QueryUserAttr", {"clientId": CLIENT_ID})):
    try:
        d = w.call("wohome", m, p, retries=0)
        s = json.dumps(d, ensure_ascii=False)[:200] if d else "空"
        print("  %-20s OK  %s" % (m, s))
    except Exception as e:
        print("  %-20s 失败 %s" % (m, str(e)[:80]))

# 3) 容量接口：先空手机号，再从上面的结果里取
print("\n== QueryCloudUsageInfo ==")
for phone in ("",):
    try:
        d = w.call("wohome", "QueryCloudUsageInfo",
                   {"phoneNum": phone, "clientId": CLIENT_ID}, retries=0)
        print("  phone=%r -> %s" % (phone, json.dumps(d, ensure_ascii=False)[:400]))
    except Exception as e:
        print("  phone=%r 失败 %s" % (phone, str(e)[:140]))

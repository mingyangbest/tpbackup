#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断点续传可行性探针：先问服务端一个问题——**它还认不认几小时前的那个 uniqueId**。

为什么要单开一个脚本：分片级续传的全部前提是「同一个 uniqueId + 同一份
fileInfo，隔一段时间继续传后面的片，服务端接受」。这一点文档里没有，只能实测。

**必须复用生产路径**：fileInfo 明文/密文、uniqueId、单片字段全部来自
app/engine.py（file_info_plain / enc_file_info / new_unique_id /
Wopan.part_fields / Wopan._upload_part）。本脚本只负责把上传循环切成
start / resume 两段，以及判定。探针自己手抄一份字段 = 测探针自己的实现，
阳性结果迁移不到真实续传上。

分两阶段，中间隔 ≥2 小时（不在进程里 sleep，杀掉了也不白等）：

  docker cp tools/probe_resume.py tpbackup:/app/probe_resume.py
  docker exec -it tpbackup python3 /app/probe_resume.py start    # 传前 100 片
  # 等 2 小时
  docker exec -it tpbackup python3 /app/probe_resume.py resume   # 从第 101 片续
  docker exec -it tpbackup python3 /app/probe_resume.py cleanup  # 收尾（可单独跑）

默认规模：总 150 片（1.2GiB），start 传 1..100 → 云端残片 800MiB，
resume 续 101..150（400MiB）。

判定与退出码：
  0 接受     第 101 片 code=0000；传完后云端 size 与 SHA256 都跟本地一致
             → 做分片级续传（进度 journal 落 SQLite）
  2 报错     第 101 片（或后续片）返回 code≠0000：服务端不接受跨会话续传
             → 退化为文件级续传（超大文件先本地切块，逐块当独立文件传）
  3 不完整   片都「收下了」，但拼出来的 size/SHA256 对不上
             → 服务端假装收下，先查它对跨会话分片的处理，再定方案
  4 未观测到 网络三次都失败 / 拿不到内容哈希：**没有结论**，别当成拒收
  1 脚本自身错误（凭据、文件、参数）
  91 探针自身缺陷：跑完了但 journal 的 done ≠ total（判定结果仍然有效，
             缺的是账本那一笔）。见文件末尾的回归断言。

SHA256 从哪来（写死，不含糊）：优先用云端列表返回的 sha256 字段；
该字段缺失时，**把文件下载回来自己算**（1.2GiB 一次可接受），报告里记
hash_source=cloud|download。两者都拿不到 → 判 4，不判 0 也不判 3。

journal 在 TP_DATA/state/probe_resume.json（原子写：tmp + fsync + replace），
start 与 resume 必须用同一个 TP_DATA，否则 resume 找不到 journal。
"""
import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/app")

from app.engine import PART, _connect, delete_entries, download_file  # noqa: E402
from app.engine import enc_file_info, ensure_chain, file_info_plain  # noqa: E402
from app.engine import find_dir, find_file, new_unique_id, sha256_of  # noqa: E402
from app.store import TokenStore  # noqa: E402

DATA = os.environ.get("TP_DATA", "/app/data")
STATE = os.path.join(DATA, "state")
JOURNAL = os.path.join(STATE, "probe_resume.json")
LOG = os.path.join(STATE, "probe_resume.log")
LOCAL = os.path.join(STATE, "probe_resume", "probe_resume.bin")
NAME = "probe_resume.bin"
CLOUD = ["_probe_resume"]
BLOCK = 4 * 1024 * 1024          # 造文件用的随机块：非全零，排除任何「全零优化」干扰
JOURNAL_VERSION = 1

V_ACCEPT, V_ERROR, V_INCOMPLETE, V_UNKNOWN = "接受", "报错", "不完整", "未观测到"
EXIT_CODE = {V_ACCEPT: 0, V_ERROR: 2, V_INCOMPLETE: 3, V_UNKNOWN: 4}
# 探针自身缺陷（不是服务端结论）单独一个码：报告里要区分「服务端怎么答」和
# 「我们自己的账本记错了」，混用会让下一次判读把缺陷当成结论。
EXIT_ASSERT = 91

# 每 32 片把 done 刷一次盘。1.2GiB 一轮 ~70s，32 片约 15s——
# 崩在这一刻最多重传 256MiB，比每片 fsync 划算，也比只在 finish 刷一次安全。
DONE_FLUSH_EVERY = 32


def log(msg):
    line = "%s %s" % (time.strftime("%F %T"), msg)
    print(line, flush=True)
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def die(msg, code=1):
    log("ERROR " + msg)
    sys.exit(code)


def token_fp(w):
    """access_token 指纹，只用来判断「这 2 小时里 token 换没换」。

    token 一换，fileInfo 的加密 key 也跟着换。若第 101 片报错，而这段时间
    token 续期过，「会话超时」和「会话绑旧 token」两种解释就同时成立——
    指纹进报告，归因时才不会张冠李戴。
    """
    return hashlib.sha256((w.at or "").encode()).hexdigest()[:12]


def connect():
    # 与 sync() 同一条凭据准备：load → maybe_renew（提前续期）→ 连通自检。
    # 不自己做一套，否则「这 2 小时里 token 过期」会以 credential 报错的形式
    # 混进第 101 片的判定里，被误读成「服务端拒绝会话」。
    store = TokenStore(os.path.join(STATE, "token.json"), log=log)
    w, res = _connect(store, log)
    if not w:
        die("云端连接失败：%s %s" % (res.get("reason"), res.get("detail")), 1)
    inf = store.info()
    if inf.get("remain_days") is not None:
        log("凭据剩余 %.2f 天（updated %s，refresh=%s）" % (
            inf["remain_days"], inf.get("updated"), inf.get("has_refresh")))
        if inf["remain_days"] < 0.2:
            log("⚠️ 令牌 5 小时内到期：这期间很可能发生续期，第 101 片若被拒，"
                "归因会多一个变量（token_fp 会记下是否真的换了）")
    return w


# ── journal：原子写，start 被杀也不留半截文件 ────────────────
def save_journal(j):
    os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
    tmp = JOURNAL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())          # 先落盘，再原子换名
    # 里面没有 token 明文，但有 token 指纹与会话标识（uniqueId/batchNo），
    # 按 token.json 同标准收权限，不值得为它留个 644。
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, JOURNAL)


def load_journal():
    if not os.path.exists(JOURNAL):
        die("找不到 %s。journal 在 TP_DATA/state 下，先确认 start 与 resume 用了"
            "同一个 TP_DATA（当前 %s）" % (JOURNAL, DATA))
    try:
        with open(JOURNAL, encoding="utf-8") as f:
            j = json.load(f)
    except Exception as e:
        die("journal 读不出来（%s）：%s" % (JOURNAL, e))
    if j.get("version") != JOURNAL_VERSION:
        die("journal 版本不符：%r" % j.get("version"))
    if j.get("tp_data") != DATA:
        log("注意：journal 记录的 TP_DATA 是 %s，当前是 %s" % (j.get("tp_data"), DATA))
    return j


# ── 造文件 ──────────────────────────────────────────────────
def make_local(parts):
    want = parts * PART
    if os.path.exists(LOCAL) and os.path.getsize(LOCAL) == want:
        log("本地文件已存在且大小正确：%s（%.1f MB）" % (LOCAL, want / 1048576.0))
        return
    os.makedirs(os.path.dirname(LOCAL), exist_ok=True)
    block = os.urandom(BLOCK)
    t0 = time.time()
    with open(LOCAL, "wb") as f:
        for _ in range(want // BLOCK):
            f.write(block)
    log("已生成 %s（%.1f MB，%.1fs）" % (LOCAL, want / 1048576.0, time.time() - t0))


def read_part(idx):
    with open(LOCAL, "rb") as f:
        f.seek((idx - 1) * PART)
        return f.read(PART)


# ── 单片上传：只有「网络重试」是本脚本自己的策略，字段全来自生产 ──
class NetError(Exception):
    """三次都拿不到响应（curl 空输出 / 连接失败）——没有观测到服务端的回答。"""


def put_part(w, cid, size, total, uid, info, idx, attempts=3, retry_code=False):
    """传一片。

    网络异常总是重试（三次都拿不到响应 → NetError → 判「未观测到」）。
    retry_code=True 时，非 0000 也按 upload() 的节奏重试三次——只给续传的
    后续片用：第 101 片才是观测点，必须一次定论；102..150 让一次 9999 抖动
    毁掉整个 2 小时探针不划算。三次都非 0000 时返回最后一次响应，由上层
    按协议码判「报错」，part/code 照样进 evidence，信号不会被重试掩盖。
    """
    blob = read_part(idx)
    fields = w.part_fields(cid, NAME, size, total, uid, info, idx, len(blob))
    last_err, last_rsp = None, None
    for a in range(attempts):
        try:
            r = w._upload_part(fields, NAME, blob)
        except Exception as e:
            last_err, last_rsp = e, None
            log("  第 %d 片网络异常（第 %d/%d 次）：%s" % (idx, a + 1, attempts, e))
        else:
            if r.get("code") == "0000" or not retry_code:
                return r
            last_err, last_rsp = None, r
            log("  第 %d 片 code=%s（第 %d/%d 次），稍后重试" % (
                idx, r.get("code"), a + 1, attempts))
        if a < attempts - 1:
            time.sleep(2 * (a + 1))
    if last_rsp is not None:
        return last_rsp
    raise NetError("第 %d 片 %d 次都没响应：%s" % (idx, attempts, last_err))


def brief(r):
    return json.dumps(r, ensure_ascii=False)[:400]


# ── 阶段一 ──────────────────────────────────────────────────
def cmd_start(args):
    total, first = args.parts, args.first
    if first >= total:
        die("--first 必须小于 --parts（否则没有「剩下的片」可续）")

    make_local(total)
    size = os.path.getsize(LOCAL)
    log("计算本地 SHA256（%.1f MB，大文件要几秒）…" % (size / 1048576.0))
    sha = sha256_of(LOCAL)
    log("本地 SHA256 = %s" % sha)

    w = connect()
    cid = ensure_chain(w, CLOUD, log)
    plain = file_info_plain(cid, NAME, size)
    uid = new_unique_id()
    info = enc_file_info(w.key, plain)
    log("云端目录 id=%s，总片数=%d，本阶段传 1..%d；uniqueId=%s，batchNo=%s" % (
        cid, total, first, uid, plain["batchNo"]))

    t0 = time.time()
    for idx in range(1, first + 1):
        try:
            # 这一步不是观测点，跟着 upload() 的重试走：频控 9999 不该废掉整个探针。
            r = put_part(w, cid, size, total, uid, info, idx, retry_code=True)
        except NetError as e:
            die("%s（阶段一中止，残片留在云端，可用 cleanup 收尾）" % e, 4)
        if r.get("code") != "0000":
            die("第 %d 片重试 3 次仍失败，探针中止：%s" % (idx, brief(r)), 2)
        if idx == 1 or idx % 10 == 0:
            log("  第 %d/%d 片 OK（累计 %.0fs）" % (idx, total, time.time() - t0))

    j = {"version": JOURNAL_VERSION, "tp_data": DATA,
         "uid": uid, "batch_no": plain["batchNo"], "info_plain": plain,
         "name": NAME, "size": size, "total": total, "done": first,
         "sha256": sha, "cloud": CLOUD, "dir_id": cid,
         "token_fp": token_fp(w), "started": time.strftime("%F %T")}
    save_journal(j)
    log("阶段一完成：已传 %d/%d 片，云端残片约 %.0f MB" % (
        first, total, first * PART / 1048576.0))
    log("等 ≥2 小时后：docker exec -it tpbackup python3 /app/probe_resume.py resume")
    return 0


# ── 校验：SHA256 优先云端字段，缺了就下载回算 ────────────────
def verify_cloud(w, j):
    """返回 (ok, evidence, hash_source)。ok=None 表示没观测到，不下结论。"""
    f = None
    for _ in range(3):                       # 列表接口可能延迟，别一把判定「没有」
        f = find_file(w, j["cloud"], j["name"])
        if f:
            break
        time.sleep(10)
    if not f:
        return None, "三次列目录都没看到这个文件（可能列表延迟，也可能没落盘）", None

    if str(f.get("size")) != str(j["size"]):
        # 服务端可能还没把最后几片拼完（生产路径传完立刻验一直能过，这里只是保险）。
        # 不复查就判「不完整」，会把「拼得慢」冤枉成「拼错了」。
        log("size 不符（云端 %s / 本地 %s），10 秒后复查一次…" % (
            f.get("size"), j["size"]))
        time.sleep(10)
        f2 = find_file(w, j["cloud"], j["name"])
        if f2:
            f = f2
        if str(f.get("size")) != str(j["size"]):
            return False, "size 不符（复查后）：云端 %s / 本地 %s" % (
                f.get("size"), j["size"]), None

    remote = (f.get("sha256") or "").lower()
    if remote:
        return (remote == j["sha256"]), "云端返回 sha256=%s" % remote, "cloud"

    log("云端没返回 sha256，下载回来自己算（%.1f MB）…" % (j["size"] / 1048576.0))
    tmp = LOCAL + ".download"
    try:
        n = download_file(w, f, tmp)
        got = sha256_of(tmp)
        log("下载 %.1f MB 完成，SHA256 = %s" % (n / 1048576.0, got))
        return (got == j["sha256"]), "下载回算 sha256=%s" % got, "download"
    except Exception as e:
        return None, "拿不到内容哈希：%s" % e, None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ── 阶段二 ──────────────────────────────────────────────────
def cmd_resume(args):
    j = load_journal()
    if not os.path.exists(LOCAL) or os.path.getsize(LOCAL) != j["size"]:
        die("本地文件不在或大小不符（%s），续传语义不成立" % LOCAL)
    log("校验本地文件内容未变…")
    if sha256_of(LOCAL) != j["sha256"]:
        die("本地文件 SHA256 已变，续传语义不成立")

    w = connect()
    cid = find_dir(w, j["cloud"])
    if not cid:
        die("云端目录不存在（可能已被清理），先跑 start")
    if str(cid) != str(j["dir_id"]):
        log("注意：云端目录 id 变化 %s → %s（不影响本次判定）" % (j["dir_id"], cid))

    fp = token_fp(w)
    renewed = (fp != j["token_fp"])
    if renewed:
        log("⚠️ token 在这 2 小时里换过了（%s → %s）。若第 101 片报错，"
            "「会话超时」和「会话绑旧 token」两种解释同时成立，报告里记下了这一点。"
            % (j["token_fp"], fp))

    plain = dict(j["info_plain"])
    plain["directoryId"] = str(cid)          # 目录 id 以当前解析为准
    info = enc_file_info(w.key, plain)       # 用当前 key 重加密，batchNo 不变

    idx = j["done"] + 1
    done = j["done"]               # 账本口径：最后一个确认成功的片号
    log("关键一步：第 %d 片，沿用 uniqueId=%s / batchNo=%s（阶段一于 %s 启动）" % (
        idx, j["uid"], plain["batchNo"], j["started"]))
    t0 = time.time()
    try:
        r = put_part(w, cid, j["size"], j["total"], j["uid"], info, idx)
    except NetError as e:
        log(str(e))
        return finish(j, V_UNKNOWN, {"reason": "关键片无响应", "detail": str(e),
                                     "token_renewed": renewed}, t0, done=done)
    log("第 %d 片返回：%s" % (idx, brief(r)))

    if r.get("code") != "0000":
        return finish(j, V_ERROR, {
            "reason": "关键片被拒", "part": idx, "code": r.get("code"), "msg": r.get("msg"),
            "token_renewed": renewed}, t0, done=done)
    done = idx

    log("第 %d 片被接受，继续把剩下的片传完（%.0f MB）…" % (
        idx, (j["total"] - j["done"] - 1) * PART / 1048576.0))
    for k in range(idx + 1, j["total"] + 1):
        try:
            rr = put_part(w, cid, j["size"], j["total"], j["uid"], info, k,
                          retry_code=True)
        except NetError as e:
            log(str(e))
            return finish(j, V_UNKNOWN, {"reason": "续传中断网", "part": k,
                                         "detail": str(e), "token_renewed": renewed},
                          t0, done=done)
        if rr.get("code") != "0000":
            return finish(j, V_ERROR, {
                "reason": "续传中途被拒（已重试 3 次）", "part": k,
                "code": rr.get("code"), "msg": rr.get("msg"),
                "token_renewed": renewed}, t0, done=done)
        done = k
        j["done"] = done
        # 定期刷盘：进程被 OOM/重启打断时，下一次 resume 从这里接着走，
        # 而不是退回阶段一的 100 重传一遍已经传过的片。
        if done % DONE_FLUSH_EVERY == 0:
            save_journal(j)
            log("  进度已落盘：done=%d/%d（累计 %.0fs）" % (
                done, j["total"], time.time() - t0))
        if k % 10 == 0:
            log("  第 %d/%d 片 OK（累计 %.0fs）" % (k, j["total"], time.time() - t0))

    ok, evidence, source = verify_cloud(w, j)
    log("校验：%s" % evidence)
    if ok is None:
        return finish(j, V_UNKNOWN, {"reason": evidence, "token_renewed": renewed,
                                     "hash_source": source}, t0)
    if ok:
        return finish(j, V_ACCEPT, {"reason": evidence, "token_renewed": renewed,
                                    "hash_source": source}, t0)
    return finish(j, V_INCOMPLETE, {"reason": evidence, "token_renewed": renewed,
                                    "hash_source": source}, t0)


def finish(j, verdict, evidence, t0, done=None):
    """收尾：先把账本写对，再落判定。

    done 必须由调用方传进来（最后一个确认成功的片号），不能沿用 journal
    里的旧值——v0.2.1 那次实测就是这里漏了：resume 跑完 150 片，finish
    只补了 verdict/evidence/finished/elapsed_s，done 还停在阶段一的 100。
    日志和下载回算都证明传完了，错的只是账本。
    """
    if done is not None:
        j["done"] = done
    j["verdict"] = verdict
    j["evidence"] = evidence
    j["finished"] = time.strftime("%F %T")
    j["elapsed_s"] = round(time.time() - t0, 1)
    save_journal(j)
    log("── 判定：%s ──" % verdict)
    for k, v in evidence.items():
        log("   %s: %s" % (k, v))
    log("结论已写入 %s；云端残片用 cleanup 收（%.0fs）" % (JOURNAL, j["elapsed_s"]))
    if verdict == V_ACCEPT:
        log("下一步：分片级续传可行，进度 journal 落 SQLite，用 resume_key 复用 uniqueId。")
    elif verdict == V_ERROR:
        log("下一步：服务端不接受跨会话续传 → 文件级续传（本地切块，逐块独立上传）。")
    elif verdict == V_INCOMPLETE:
        log("下一步：先查服务端怎么合并跨会话的分片，再定方案；别急着写续传。")
    else:
        log("下一步：没有观测到结论。换个时间/网络重跑一次，不要据此改设计。")

    # ── 回归断言 ────────────────────────────────────────────────
    # 「片都传完了」这个前提成立时（接受 / 不完整两种判定都是传完才验的），
    # 账本上的 done 必须等于 total。不等就是进度持久化有缺陷，用 91 单独报，
    # 不要把它和服务端的结论混成一个退出码。
    if verdict in (V_ACCEPT, V_INCOMPLETE) and j.get("done") != j["total"]:
        log("‼️ 断言失败：片已传完，但 journal 的 done=%r ≠ total=%s" % (
            j.get("done"), j["total"]))
        log("   这是探针账本缺陷，不是服务端结论；判定本身仍然有效。")
        return EXIT_ASSERT
    return EXIT_CODE[verdict]


# ── 清理（独立子命令，事后也能收尾）────────────────────────
def cmd_cleanup(args):
    j = {}
    try:
        with open(JOURNAL, encoding="utf-8") as f:
            j = json.load(f)
        log("读到 journal，按里面记录的路径清理")
    except Exception:
        log("没有可用的 journal，按默认路径清理")

    cloud = j.get("cloud") or CLOUD
    name = j.get("name") or NAME
    w = connect()
    cid = find_dir(w, cloud)
    if not cid:
        log("云端目录 %s 不存在，跳过" % "/".join(cloud))
    else:
        f = find_file(w, cloud, name)
        if f:
            delete_entries(w, file_ids=[f.get("id")])
            log("已删除云端文件 %s" % name)
        else:
            log("云端没有 %s，可能没落盘" % name)
        left = w.ls(cid)
        if not left:
            delete_entries(w, dir_ids=[str(cid)])
            log("目录已空，已删除 %s" % "/".join(cloud))
        else:
            log("目录里还剩 %d 项，保留不删：%s" % (
                len(left), "、".join((e.get("name") or "") for e in left[:5])))

    if args.keep_local:
        log("保留本地文件 %s" % LOCAL)
    else:
        try:
            os.remove(LOCAL)
            log("已删除本地 %s" % LOCAL)
        except FileNotFoundError:
            pass
    if args.keep_journal:
        log("保留 journal %s（含判定结果）" % JOURNAL)
    else:
        try:
            os.remove(JOURNAL)
            log("已删除 journal %s" % JOURNAL)
        except FileNotFoundError:
            pass
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="断点续传可行性探针（两阶段；退出码 0/2/3/4 见文件头）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="传前 N 片，留下残片")
    s.add_argument("--parts", type=int, default=150, help="总片数（默认 150 = 1.2GiB）")
    s.add_argument("--first", type=int, default=100, help="本阶段传多少片（默认 100）")
    s.set_defaults(func=cmd_start)

    r = sub.add_parser("resume", help="隔 ≥2 小时后续传并判定")
    r.set_defaults(func=cmd_resume)

    c = sub.add_parser("cleanup", help="删除云端残片与本地文件（读 journal，可单独跑）")
    c.add_argument("--keep-local", action="store_true", help="保留本地 1.2GiB 文件")
    c.add_argument("--keep-journal", action="store_true", help="保留结论 json")
    c.set_defaults(func=cmd_cleanup)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

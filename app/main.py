#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tpbackup —— 联通云盘备份管理台"""
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .engine import _connect as engine_connect
from .engine import _fix_perm as engine_fix_perm
from .engine import download_file as engine_download
from .engine import download_tree as engine_download_tree
from .engine import find_dir as engine_find_dir
from .engine import find_file as engine_find
from .engine import push as engine_push
from .engine import quota as engine_quota
from .engine import walk_usage as engine_walk
from .orphans import scan_orphans
from .notify import kuma
from apscheduler.triggers.cron import CronTrigger
from .runner import Runner
from .scheduler import Scheduler
from .store import DB, TokenStore

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("tpbackup")

# ── 配置 ────────────────────────────────────────────────────
DATA = os.environ.get("TP_DATA", "/app/data")
WEB_USER = os.environ.get("TP_USER", "admin")
WEB_PASS = os.environ.get("TP_PASS", "tpbackup")
OPENLIST_DB = os.environ.get("TP_OPENLIST_DB", "")
SYNC_OPENLIST = os.environ.get("TP_SYNC_OPENLIST", "0") == "1"
ALLOWED_ROOTS = [p for p in os.environ.get(
    "TP_ALLOWED_ROOTS", "/vol1,/vol00").split(",") if p]
# 推送型任务清理旧包时永不删除的文件名，逗号分隔。比如云端目录里放了
# 一份说明文件，不参与 --keep 的滚动清理，就把它列在这里。
PROTECT_FILES = tuple(s.strip() for s in
                      os.environ.get("TP_PROTECT_FILES", "").split(",") if s.strip())

os.makedirs(DATA, exist_ok=True)
os.makedirs(os.path.join(DATA, "state"), exist_ok=True)
os.makedirs(os.path.join(DATA, "logs"), exist_ok=True)

db = DB(os.path.join(DATA, "tpbackup.db"))
store = TokenStore(os.path.join(DATA, "state", "token.json"),
                   openlist_db=OPENLIST_DB or None,
                   sync_openlist=SYNC_OPENLIST, log=log.info)


def _notify():
    try:
        scheduler.reload()
    except Exception:
        pass


runner = Runner(db, store, os.path.join(DATA, "logs"), on_change=_notify)
scheduler = Scheduler(db, runner)

app = FastAPI(title="tpbackup", docs_url=None, redoc_url=None,
              lifespan=lambda app: _lifespan(app))
security = HTTPBasic()


@asynccontextmanager
async def _lifespan(app):
    # 生成器写法：yield 前是启动逻辑，yield 后是关闭逻辑。
    # 替换已弃用的 @app.on_event，行为不变。
    seed_tasks()
    # 容器重启会把跑一半的记录永久留在 running，界面上一直显示"运行中"，
    # 还会被 is_running() 当成在跑而挡住后续调度。启动时清扫。
    try:
        n = db.clear_stale_runs()
        if n:
            log.info("已把 %d 条僵尸 running 记录标记为 interrupted", n)
    except Exception as e:
        log.warning("清扫僵尸运行记录失败: %s", e)
    # 主动初始化 token：state 为空时会从 OpenList 库导入，避免第一次运行才发现没凭证
    try:
        at, rt = store.load()
        if at:
            log.info("token 已就绪（来源：%s）",
                     "本地 state" if store.info().get("updated") else "OpenList 库导入")
        else:
            log.warning("尚无可用 token —— 需完成联通云盘授权，或手工放置 state/token.json")
    except Exception as e:
        log.warning("token 初始化失败: %s", e)
    # 默认弱口令是给首次体验用的。这个服务能改文件、能读云盘凭证，
    # 公网暴露 + 默认密码等于把云盘交出去，启动时喊一嗓子。
    if WEB_PASS == "tpbackup":
        log.warning("=" * 66)
        log.warning("TP_PASS 仍是默认密码 tpbackup —— 本服务可读写文件与云盘凭证，")
        log.warning("请尽快在 compose 里修改 TP_PASS 并重启容器；勿将 8088 直接暴露公网")
        log.warning("=" * 66)
    _load_cloud_cache()
    scheduler.start()
    # 云端占用遍历很贵（6000+ 文件要跑几分钟），只在凌晨统计一次，界面读缓存
    try:
        scheduler.sched.add_job(
            _refresh_cloud,
            CronTrigger(hour=5, minute=35, timezone="Asia/Shanghai"),
            id="cloud-usage", replace_existing=True, coalesce=True,
            misfire_grace_time=3600)
    except Exception as e:
        log.warning("注册云端占用统计任务失败: %s", e)
    log.info("tpbackup 启动完成 | data=%s | 任务=%d", DATA, len(db.list_tasks()))
    yield
    scheduler.shutdown()


def auth(c: HTTPBasicCredentials = Depends(security)):
    ok_u = secrets.compare_digest(c.username.encode(), WEB_USER.encode())
    ok_p = secrets.compare_digest(c.password.encode(), WEB_PASS.encode())
    if not (ok_u and ok_p):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "认证失败",
                            {"WWW-Authenticate": "Basic"})
    return c.username


# ── 启动 ────────────────────────────────────────────────────
def seed_tasks():
    """首次启动导入示例任务，免得从零配。

    默认关闭：TP_SEED=1 才生效。下面的 seeds 只是示例结构，
    首次启动前请把 src / cloud 改成你自己的路径，否则导入的是
    三条指向不存在目录的脏任务。
    """
    if os.environ.get("TP_SEED", "0") != "1":
        return
    if db.list_tasks():
        return
    seeds = [
        dict(name="照片", src="/vol1/photo",          # ← 改成你的源目录
             cloud=["PhotosBackup", "photos"], cron="10 4 * * *",
             excludes=["#recycle", "@eaDir"], workers=4,
             note="示例：目录增量同步"),
        dict(name="媒体库", src="/vol1/media",        # ← 改成你的源目录
             cloud=["MediaBackup"], cron="10 4 * * *",
             excludes=["thumbs", "encoded-video"], workers=4,
             note="示例：媒体库增量同步"),
        dict(name="文档", src="/vol1/docs",           # ← 改成你的源目录
             cloud=["DocsBackup"], cron="40 4 * * *",
             excludes=[], workers=4,
             note="示例：文档目录"),
    ]
    for s in seeds:
        try:
            db.upsert_task(s)
            log.info("已导入初始任务: %s", s["name"])
        except Exception as e:
            log.warning("导入 %s 失败: %s", s["name"], e)


# ── 模型 ────────────────────────────────────────────────────
class TaskIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    src: str = Field(..., min_length=1)
    cloud: list[str] = Field(default_factory=list)
    cron: str = ""
    excludes: list[str] = Field(default_factory=list)
    workers: int = 4
    enabled: bool = True
    note: str = ""
    mode: str | None = None          # dir / push；不传则保持原值
    keep: int = 0
    pattern: str = ""


class PushFile(BaseModel):
    path: str = Field(..., min_length=1)
    name: str | None = None


class PushIn(BaseModel):
    task: str = Field(..., min_length=1, max_length=64)
    cloud: list[str] = Field(default_factory=list)
    files: list[PushFile] = Field(default_factory=list)
    keep: int = 0
    pattern: str = ""
    dry_run: bool = False
    note: str = ""


# 同一任务的推送串行化：宿主机 cron 与手动触发撞车时，后来的直接返回 409
_push_guard = {}
_push_guard_lk = threading.Lock()


# ── 视图 ────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"ok": True, "ts": time.strftime("%F %T")}


@app.get("/api/overview")
def overview(_=Depends(auth)):
    tasks = db.list_tasks()
    runs = db.list_runs(limit=200)
    today = time.strftime("%F")
    ok_today = sum(1 for r in runs if r["status"] == "success"
                   and (r["finished_at"] or "").startswith(today))
    fail_today = sum(1 for r in runs if r["status"] == "failed"
                     and (r["finished_at"] or "").startswith(today))
    last_ok = next((r["finished_at"] for r in runs if r["status"] == "success"), None)
    return {
        "tasks": len(tasks), "enabled": sum(1 for t in tasks if t["enabled"]),
        "running": len(runner.running_list()),
        "ok_today": ok_today, "fail_today": fail_today, "last_success": last_ok,
        "token": store.info(),
    }


@app.get("/api/tasks")
def list_tasks(_=Depends(auth)):
    nxt = scheduler.next_runs()
    out = []
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    for t in db.list_tasks():
        t = dict(t)
        t["cloud"] = json.loads(t["cloud"] or "[]")
        t["excludes"] = json.loads(t["excludes"] or "[]")
        t["enabled"] = bool(t["enabled"])
        t["next_run"] = nxt.get(t["id"])
        # push 型任务的调度在宿主机 cron，应用调度器里没有它——
        # 但表达式存在，可以本地算出下次触发时间，界面上就不用显示「—」
        if t["next_run"] is None and (t.get("mode") or "dir") == "push" and (t.get("cron") or "").strip():
            try:
                nt = CronTrigger.from_crontab(t["cron"], timezone="Asia/Shanghai") \
                                 .get_next_fire_time(None, now)
                t["next_run"] = nt.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%F %T") if nt else None
            except Exception:
                pass
        t["running"] = runner.is_running(t["id"])
        t["running_dry"] = runner.is_dry_running(t["id"])
        t["last"] = _decorate_run(db.last_run(t["id"]))
        # 预览不传输数据，不该顶替「上次备份」的结论；界面用 last_real 显示状态
        t["last_real"] = _decorate_run(db.last_real_run(t["id"]))
        out.append(t)
    return out


def _check_cron(cron: str):
    """无效的 cron 以前能保存成功，但调度器会静默跳过——任务看起来正常却永不运行。
    在入口处拒绝，把错误提前到用户保存的那一刻。"""
    cron = (cron or "").strip()
    if not cron:
        return
    try:
        CronTrigger.from_crontab(cron, timezone="Asia/Shanghai")
    except Exception as e:
        raise HTTPException(400, "cron 表达式无效（%s）：%s" % (cron, str(e)[:120]))


@app.post("/api/tasks")
def create_task(d: TaskIn, _=Depends(auth)):
    if not d.cloud:
        raise HTTPException(400, "云端路径不能为空")
    _check_cron(d.cron)
    if db.get_task_by_name(d.name):
        raise HTTPException(400, "任务名已存在")
    if not any(os.path.isdir(r) and d.src.startswith(r) for r in ALLOWED_ROOTS):
        raise HTTPException(400, "源目录必须在 %s 之内" % ", ".join(ALLOWED_ROOTS))
    tid = db.upsert_task(d.dict())
    scheduler.reload()
    return {"id": tid}


@app.put("/api/tasks/{tid}")
def update_task(tid: int, d: TaskIn, _=Depends(auth)):
    cur = db.get_task(tid)
    if not cur:
        raise HTTPException(404, "任务不存在")
    if not d.cloud:
        raise HTTPException(400, "云端路径不能为空")
    _check_cron(d.cron)
    payload = d.dict()
    payload["id"] = tid
    # 前端表单没有 mode 字段：不传就沿用原值，避免把 push 任务改回 dir
    payload["mode"] = d.mode or (cur.get("mode") or "dir")
    db.upsert_task(payload)
    scheduler.reload()
    return {"ok": True}


@app.delete("/api/tasks/{tid}")
def delete_task(tid: int, _=Depends(auth)):
    if not db.get_task(tid):
        raise HTTPException(404, "任务不存在")
    if runner.is_running(tid):
        raise HTTPException(409, "任务正在运行，先停止再删除")
    db.delete_task(tid)
    scheduler.reload()
    return {"ok": True}


@app.post("/api/tasks/{tid}/run")
def run_task(tid: int, dry_run: int = 0, _=Depends(auth)):
    t = db.get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if (t.get("mode") or "dir") == "push":
        raise HTTPException(400, "该任务由宿主机脚本打包后推送，请在宿主机执行对应脚本")
    rid = runner.start(t, dry_run=bool(dry_run))
    if rid is None:
        raise HTTPException(409, "该任务正在运行中")
    return {"run_id": rid}


@app.post("/api/tasks/{tid}/stop")
def stop_task(tid: int, _=Depends(auth)):
    return {"ok": runner.request_stop(tid)}


class KumaIn(BaseModel):
    url: str = ""


@app.put("/api/tasks/{tid}/kuma")
def set_kuma(tid: int, d: KumaIn, _=Depends(auth)):
    """设置/清除该任务的心跳地址（Uptime Kuma push URL）。"""
    if not db.get_task(tid):
        raise HTTPException(404, "任务不存在")
    u = (d.url or "").strip()
    if u and not u.startswith(("http://", "https://")):
        raise HTTPException(400, "必须是 http(s) 地址")
    db.set_kuma_url(tid, u)
    return {"ok": True, "url": u}


@app.get("/api/tasks/{tid}/live")
def live(tid: int, _=Depends(auth)):
    st = runner.get_state(tid)
    if not st:
        return {"running": False, "progress": {}, "log_tail": []}
    return st


@app.post("/api/jobs/push")
def push_job(d: PushIn, _=Depends(auth)):
    """宿主机备份脚本调用：把已打包好的文件交给本应用上传。

    同步执行——脚本要拿到真实结果才能决定这次备份算不算成功。
    同名文件会被覆盖（联通云盘该接口本身就是覆盖语义），
    所以「单文件滚动备份」不需要先删除。
    """
    if not d.files:
        raise HTTPException(400, "files 不能为空")

    # 路径白名单：必须在 TP_ALLOWED_ROOTS 内，且是已存在的非空普通文件
    files = []
    for f in d.files:
        real = os.path.realpath(f.path)
        if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in ALLOWED_ROOTS):
            raise HTTPException(403, "路径超出允许范围: %s" % f.path)
        if not os.path.isfile(real):
            raise HTTPException(404, "文件不存在: %s" % f.path)
        files.append((real, f.name or os.path.basename(real)))

    t = db.get_task_by_name(d.task)
    if not t:
        if not d.cloud:
            raise HTTPException(400, "新建推送任务必须提供 cloud 路径")
        tid = db.upsert_task(dict(
            name=d.task, src=os.path.dirname(files[0][0]), cloud=d.cloud,
            cron="", excludes=[], workers=1, enabled=True,
            note=d.note or "由宿主机脚本推送，应用负责传输与记录",
            mode="push", keep=d.keep, pattern=d.pattern))
        t = db.get_task(tid)
        scheduler.reload()
        log.info("已创建推送型任务: %s", d.task)

    cloud = d.cloud or json.loads(t["cloud"] or "[]")
    if not cloud:
        raise HTTPException(400, "任务缺少云端路径")

    with _push_guard_lk:
        if _push_guard.get(d.task):
            raise HTTPException(409, "该任务正在推送中，请稍后")
        _push_guard[d.task] = True

    run_id = db.start_run(t["id"], t["name"])
    lines = []

    def logf(m):
        lines.append("%s %s" % (time.strftime("%H:%M:%S"), m))
        log.info("[push:%s] %s", d.task, m)

    try:
        res = engine_push(store=store, files=files, cloud_chain=cloud,
                          keep=d.keep, pattern=d.pattern, dry_run=d.dry_run,
                          log=logf, protect=PROTECT_FILES)
    except Exception as e:
        res = {"ok": False, "reason": "exception", "detail": str(e)[:300],
               "todo": len(files), "ok_n": 0, "fail": len(files),
               "bytes": 0, "elapsed": 0, "pruned": 0, "files": []}
        logf("未捕获异常: %s" % e)
    finally:
        with _push_guard_lk:
            _push_guard.pop(d.task, None)

    status = "dry-run" if (d.dry_run and res.get("ok")) else \
        ("success" if res.get("ok") else "failed")
    db.finish_run(run_id, res, status, "\n".join(lines))
    log.info("推送完成 %s | %s | ok=%d fail=%d %.2fMB %.1fs", d.task, status,
             res.get("ok_n", 0), res.get("fail", 0),
             (res.get("bytes") or 0) / 1048576, res.get("elapsed") or 0)

    # 心跳：只报真实结果，试运行不喂
    if status in ("success", "failed"):
        if status == "success":
            msg = "OK 文件%s %.1fMB %.0fs" % (
                res.get("ok_n", 0), (res.get("bytes") or 0) / 1048576,
                res.get("elapsed") or 0)
        else:
            msg = "FAIL %s %s" % (res.get("reason") or "", res.get("detail") or "")
        kuma(t.get("kuma_url"), status == "success", msg,
             int((res.get("elapsed") or 0) * 1000))

    body = {"ok": bool(res.get("ok")), "status": status, "run_id": run_id,
            "task_id": t["id"], "todo": res.get("todo", 0),
            "ok_n": res.get("ok_n", 0), "fail": res.get("fail", 0),
            "bytes": res.get("bytes", 0), "elapsed": res.get("elapsed", 0),
            "pruned": res.get("pruned", 0),
            "reason": res.get("reason", ""), "detail": res.get("detail", ""),
            "log_tail": lines[-25:]}
    if not res.get("ok"):
        return JSONResponse(status_code=502, content=body)
    return body


@app.get("/api/jobs/stat")
def job_stat(cloud: str = Query(...), name: str = Query(...), _=Depends(auth)):
    """查云端某个文件是否存在及其体积/时间。巡检脚本用来代替 rclone lsl。"""
    chain = [s for s in cloud.split("/") if s]
    if not chain or not name:
        raise HTTPException(400, "cloud / name 不能为空")
    w, res = engine_connect(store, log.info)
    if not w:
        raise HTTPException(502, res.get("detail") or res.get("reason") or "云端不可达")
    try:
        f = engine_find(w, chain, name)
    except Exception as e:
        raise HTTPException(502, "查询失败: %s" % str(e)[:150])
    if not f:
        return {"exists": False, "name": name}
    return {"exists": True, "name": f.get("name"), "size": f.get("size"),
            "sha256": f.get("sha256"), "id": f.get("id"),
            "create_time": f.get("createTime"), "modify_time": f.get("modifyTime")}


@app.get("/api/jobs/file")
def job_file(cloud: str = Query(...), name: str = Query(...), _=Depends(auth)):
    """下载云端文件。巡检脚本用它做「能不能真的下回来」的演练。"""
    chain = [s for s in cloud.split("/") if s]
    if not chain or not name:
        raise HTTPException(400, "cloud / name 不能为空")
    w, res = engine_connect(store, log.info)
    if not w:
        raise HTTPException(502, res.get("detail") or res.get("reason") or "云端不可达")
    try:
        f = engine_find(w, chain, name)
    except Exception as e:
        raise HTTPException(502, "查询失败: %s" % str(e)[:150])
    if not f:
        raise HTTPException(404, "云端没有该文件: %s" % name)

    tmp = tempfile.NamedTemporaryFile(prefix="tpfetch_", suffix=".bin", delete=False)
    tmp.close()
    try:
        n = engine_download(w, f, tmp.name)
    except Exception as e:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        raise HTTPException(502, "下载失败: %s" % str(e)[:150])
    log.info("下载 %s/%s → %d 字节", cloud, name, n)

    def _cleanup():
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    return FileResponse(tmp.name, media_type="application/octet-stream",
                        filename=name, background=BackgroundTask(_cleanup))


_Q = {"at": 0.0, "data": None}


@app.get("/api/stats/storage")
def stats_storage(force: int = 0, _=Depends(auth)):
    """云盘容量。云端接口不便宜，默认 5 分钟缓存；失败时回退旧值并标记 stale。"""
    now = time.time()
    if not force and _Q["data"] and now - _Q["at"] < 300:
        return _Q["data"]
    try:
        d = engine_quota(store, log=log.info)
    except Exception as e:
        log.warning("容量查询失败: %s", e)
        if _Q["data"]:
            old = dict(_Q["data"])
            old["stale"] = True
            return old
        return {"ok": False, "error": str(e)[:140]}
    _Q["at"] = now
    _Q["data"] = d
    return d


@app.get("/api/runs")
def list_runs(task_id: int = 0, limit: int = 50, _=Depends(auth)):
    return [_decorate_run(r) for r in db.list_runs(task_id or None, min(limit, 500))]


@app.get("/api/runs/{rid}")
def get_run(rid: int, _=Depends(auth)):
    r = db.get_run(rid)
    if not r:
        raise HTTPException(404, "记录不存在")
    return _decorate_run(r)


@app.get("/api/stats/transfer")
def stats_transfer(days: int = Query(14, ge=1, le=90), _=Depends(auth)):
    """备份数据量统计（2026-10-28 界面改版新增，只读）。

    口径：只计 status='success' 的实际传输量（runs.bytes 是引擎复核后
    真正写上云端的字节）；预览/失败/停止/中断一律不计，避免把
    「比对后无新文件」的 0 字节成功运行和失败重试混进同一口锅。
    finished_at 与 date('now','localtime') 同为容器本地时间（TZ=Asia/Shanghai），
    按日切分没有时区错位。没有写库、没有外部请求，随便刷。
    """
    from datetime import date as _date, timedelta as _timedelta

    with db._conn() as c:
        per = {r["task_id"]: dict(r) for r in c.execute("""
            SELECT task_id,
              MAX(task_name) AS task_name,
              SUM(CASE WHEN substr(finished_at,1,10) = date('now','localtime')
                       THEN bytes ELSE 0 END) AS today_bytes,
              SUM(CASE WHEN substr(finished_at,1,7)  = strftime('%Y-%m','now','localtime')
                       THEN bytes ELSE 0 END) AS month_bytes,
              SUM(bytes) AS total_bytes,
              SUM(CASE WHEN substr(finished_at,1,10) = date('now','localtime')
                       THEN ok_n ELSE 0 END) AS today_files,
              SUM(ok_n) AS total_files,
              SUM(CASE WHEN substr(finished_at,1,10) = date('now','localtime')
                       THEN 1 ELSE 0 END) AS runs_today
            FROM runs
            WHERE status='success' AND finished_at IS NOT NULL
            GROUP BY task_id""")}
        daily_rows = c.execute("""
            SELECT substr(finished_at,1,10) AS d, task_id, SUM(bytes) AS b
            FROM runs
            WHERE status='success' AND finished_at IS NOT NULL
              AND substr(finished_at,1,10) >= date('now','localtime', ?)
            GROUP BY d, task_id ORDER BY d""", ("-%d days" % (days - 1),)).fetchall()
        first = c.execute(
            "SELECT MIN(substr(finished_at,1,10)) FROM runs "
            "WHERE status='success' AND finished_at IS NOT NULL").fetchone()[0]

    daily = {}
    for r in daily_rows:
        slot = daily.setdefault(r["d"], {"date": r["d"], "total": 0, "by_task": {}})
        slot["total"] += r["b"] or 0
        slot["by_task"][str(r["task_id"])] = r["b"] or 0
    # 补齐空日：图表需要连续的时间轴，缺的日子补 0
    today = _date.today()
    series = []
    for i in range(days - 1, -1, -1):
        d = (today - _timedelta(days=i)).isoformat()
        series.append(daily.get(d) or {"date": d, "total": 0, "by_task": {}})

    totals = {
        "today_bytes": sum(v["today_bytes"] or 0 for v in per.values()),
        "month_bytes": sum(v["month_bytes"] or 0 for v in per.values()),
        "total_bytes": sum(v["total_bytes"] or 0 for v in per.values()),
        "today_files": sum(v["today_files"] or 0 for v in per.values()),
        "runs_today": sum(v["runs_today"] or 0 for v in per.values()),
        "since": first,
    }
    return {"per_task": list(per.values()), "totals": totals,
            "daily": series, "days": days}


@app.get("/api/token")
def token_info(_=Depends(auth)):
    # 懒加载：state 为空时尝试初始化（首次从 OpenList 库导入）
    try:
        if not store.info().get("updated"):
            store.load()
    except Exception as e:
        log.warning("token 懒加载失败: %s", e)
    return store.info()


# 刷新节流：云盘的 token 接口对高频调用敏感（可能风控），60 秒最多放行一次
_RENEW_AT = {"t": 0.0}


@app.post("/api/token/renew")
def token_renew(_=Depends(auth)):
    if time.time() - _RENEW_AT["t"] < 60:
        raise HTTPException(429, "刷新太频繁，请一分钟后再试")
    _RENEW_AT["t"] = time.time()
    from .engine import Wopan
    at, rt = store.load()
    if not at:
        raise HTTPException(400, "没有可用 token")
    w = Wopan(at, rt, on_token=store.save)
    try:
        w.refresh()
        return {"ok": True, "info": store.info()}
    except Exception as e:
        raise HTTPException(400, "刷新失败: %s" % str(e)[:200])


@app.get("/api/fs/list")
def fs_list(path: str = Query(""), _=Depends(auth)):
    """目录浏览。只允许在 TP_ALLOWED_ROOTS 内。"""
    if not path:
        out = []
        for r in ALLOWED_ROOTS:
            if os.path.isdir(r):
                out.append({"name": r, "path": r, "type": "dir", "root": True})
        return {"path": "", "entries": out}

    real = os.path.realpath(path)
    if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in ALLOWED_ROOTS):
        raise HTTPException(403, "路径超出允许范围")
    if not os.path.isdir(real):
        raise HTTPException(404, "目录不存在")

    entries = []
    try:
        for name in sorted(os.listdir(real)):
            if name.startswith("."):
                continue
            p = os.path.join(real, name)
            try:
                is_dir = os.path.isdir(p)
                entries.append({
                    "name": name, "path": p,
                    "type": "dir" if is_dir else "file",
                    "size": 0 if is_dir else os.path.getsize(p),
                })
            except Exception:
                continue
    except PermissionError:
        raise HTTPException(403, "无权限读取该目录")
    return {"path": real, "entries": entries[:800],
            "parent": os.path.dirname(real) if real not in ALLOWED_ROOTS else ""}


@app.get("/api/stats/disk")
def disk_stats(_=Depends(auth)):
    out = []
    for r in ALLOWED_ROOTS:
        try:
            u = shutil.disk_usage(r)
            out.append({"root": r, "total": u.total, "used": u.used, "free": u.free})
        except Exception:
            pass
    return out


# ════════════════════════════════════════════════════════════
# 健康结论 / 云端占用 / 恢复（2026-10-28 新增）
# ════════════════════════════════════════════════════════════
_ERR_RULES = [
    ("auth", ("token", "401", "凭证", "授权", "refresh"), "凭证失效"),
    ("network", ("timeout", "timed out", "connection", "unreachable",
                 "dns", "ssl", "网络"), "网络异常"),
    ("permission", ("403", "permission", "denied", "forbidden"), "权限不足"),
    ("disk", ("no space", "enospc", "磁盘", "空间不足"), "磁盘不足"),
    ("conflict", ("409", "already running", "正在运行"), "任务冲突"),
]


def _err_kind(r):
    """把失败翻译成人话。日志文本千奇百怪，归成 6 类就够决策了。"""
    st = (r.get("status") or "").lower()
    if st == "success":
        return {"kind": "ok", "label": "成功"}
    if st == "dry-run":
        return {"kind": "preview", "label": "预览"}
    if st == "running":
        return {"kind": "running", "label": "运行中"}
    if st == "stopped":
        return {"kind": "stopped", "label": "已停止"}
    if st == "interrupted":
        return {"kind": "interrupted", "label": "被中断"}
    blob = " ".join(str(r.get(k) or "") for k in
                    ("reason", "detail", "log")).lower()
    for kind, keys, label in _ERR_RULES:
        if any(k in blob for k in keys):
            return {"kind": kind, "label": label}
    if (r.get("fail") or 0) > 0:
        return {"kind": "partial", "label": "部分文件失败"}
    return {"kind": "unknown", "label": "失败"}


def _decorate_run(r):
    if not r:
        return r
    r = dict(r)
    r.update(_err_kind(r))
    return r


# ── 云端占用 ────────────────────────────────────────────────
_CLOUD = {"at": 0.0, "data": None, "running": False, "err": ""}
CLOUD_CACHE = os.path.join(DATA, "state", "cloud_usage.json")


def _load_cloud_cache():
    """统计一次要 2~3 分钟，结果落盘，重启后立刻有数，不用再等一遍。"""
    try:
        d = json.load(open(CLOUD_CACHE, encoding="utf-8"))
        if d.get("tasks"):
            _CLOUD["data"] = {"tasks": d["tasks"],
                              "total_bytes": d.get("total_bytes", 0),
                              "at": d.get("at")}
            _CLOUD["at"] = float(d.get("at_ts") or 0)
            log.info("已载入云端占用缓存（%s）", d.get("at"))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("读取云端占用缓存失败: %s", e)


def _save_cloud_cache(data):
    try:
        tmp = CLOUD_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CLOUD_CACHE)
    except Exception as e:
        log.warning("写入云端占用缓存失败: %s", e)


def _refresh_cloud():
    if _CLOUD["running"]:
        return
    t0 = time.time()
    _CLOUD["running"] = True
    rows, err = [], ""
    # 注意：计算过程中绝不改 _CLOUD["data"]。
    # 之前边算边发，界面就会看到「总量从 20G 跳到 22G 再跳到 44G、横条宽度乱变」，
    # 看起来像 bug。现在算完一次性替换，期间界面继续显示上一次的完整结果。
    try:
        w, res = engine_connect(store, log.info)
        if not w:
            err = res.get("reason") or "云端连接失败"
        else:
            for t in db.list_tasks():
                cid = engine_find_dir(w, _cloud_chain(t))
                row = {"task_id": t["id"], "name": t["name"],
                       "cloud": "/".join(t.get("cloud") or [])}
                if not cid:
                    row.update({"files": None, "bytes": None, "error": "云端目录不存在"})
                else:
                    try:
                        f, b = engine_walk(w, cid)
                        row.update({"files": f, "bytes": b})
                    except Exception as e:
                        row.update({"files": None, "bytes": None, "error": str(e)[:120]})
                rows.append(row)
        _CLOUD["at"] = time.time()
        _CLOUD["data"] = {"tasks": rows,
                          "total_bytes": sum(r.get("bytes") or 0 for r in rows),
                          "at": time.strftime("%F %T")}
        _CLOUD["err"] = err
        _save_cloud_cache({"tasks": rows,
                           "total_bytes": _CLOUD["data"]["total_bytes"],
                           "at": _CLOUD["data"]["at"],
                           "at_ts": _CLOUD["at"]})
        log.info("云端占用统计完成，用时 %.0f 秒", time.time() - t0)
    except Exception as e:
        _CLOUD["err"] = str(e)[:160]
        log.warning("云端占用统计失败: %s", e)
    finally:
        _CLOUD["running"] = False


@app.get("/api/stats/cloud")
def stats_cloud(force: int = 0, _=Depends(auth)):
    """每个任务在云端占多少。遍历目录不便宜，6 小时缓存 + 后台刷新。"""
    now = time.time()
    stale = (not _CLOUD["data"]) or (now - _CLOUD["at"] > 24 * 3600)
    if (force or stale) and not _CLOUD["running"]:
        threading.Thread(target=_refresh_cloud, daemon=True).start()
    out = dict(_CLOUD["data"] or {"tasks": [], "total_bytes": 0, "at": None})
    out["computing"] = _CLOUD["running"]
    out["stale"] = stale and bool(_CLOUD["data"])
    out["error"] = _CLOUD["err"]
    # 增长与满盘预测：用最近 30 天的真实上传量推斜率
    try:
        with db._conn() as c:
            row = c.execute("""
                select coalesce(sum(case when finished_at >= date('now','localtime','-30 day')
                                         then bytes else 0 end),0) as b30
                from runs where status='success'""").fetchone()
        b30 = int(row["b30"] or 0)
        per_day = b30 / 30.0
        quota = _Q["data"] if _Q["data"] else None
        free = (quota or {}).get("free")
        days = round(free / per_day, 0) if (free and per_day > 0) else None
        out["growth"] = {"bytes_30d": b30, "bytes_per_day": int(per_day),
                         "days_to_full": days}
    except Exception as e:
        out["growth"] = {"error": str(e)[:120]}
    return out


# ── 云端浏览与恢复 ──────────────────────────────────────────
class RestoreIn(BaseModel):
    task_id: int
    path: str = ""
    dest: str


def _cloud_chain(t):
    """tasks.cloud 在库里是 JSON 字符串，直接 list() 会按字符拆开——
    2026-10-28 实测踩到：云端路径变成 ['"','A','l','b'...]。统一在这里解析。"""
    c = t.get("cloud") or []
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except Exception:
            c = []
    return [str(x) for x in c if x]


def _resolve_cloud(w, t, path):
    chain = _cloud_chain(t) + [p for p in (path or "").split("/") if p]
    return chain, engine_find_dir(w, chain)


def _restore_target(w, t, path):
    """把 path 解析成（目录 id / 文件条目）。
    目录 → 递归取回；文件 → 单文件下载。两条路径共用同一个入口，
    保证「恢复演练」和手点「取回」走的是同一段代码。"""
    chain = _cloud_chain(t) + [p for p in (path or "").split("/") if p]
    if not chain:
        return "0", None
    parent = engine_find_dir(w, chain[:-1]) if len(chain) > 1 else "0"
    name = chain[-1]
    if parent:
        for f in w.ls(parent):
            if f.get("name") == name:
                if f.get("type") == 0:
                    return str(f.get("id")), None
                return None, f
    cid = engine_find_dir(w, chain)
    return cid, None


@app.get("/api/cloud/ls")
def cloud_ls(task_id: int = Query(...), path: str = Query(""), _=Depends(auth)):
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    w, res = engine_connect(store, log.info)
    if not w:
        raise HTTPException(503, "云端连接失败：%s" % (res.get("reason") or ""))
    chain, cid = _resolve_cloud(w, t, path)
    if not cid:
        raise HTTPException(404, "云端路径不存在：%s" % "/".join(chain))
    entries = []
    for f in w.ls(cid):
        entries.append({"id": str(f.get("id")), "name": f.get("name") or "",
                        "dir": f.get("type") == 0,
                        "size": int(f.get("size") or 0)})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"path": path, "chain": "/".join(chain), "entries": entries[:800]}


_REST, _REST_LK = {}, threading.Lock()


def _do_restore(jid, t, path, dest):
    st = _REST[jid]
    st["state"] = "running"

    def _log(m):
        st["log"].append("%s %s" % (time.strftime("%H:%M:%S"), m))
        if len(st["log"]) > 400:
            del st["log"][:100]

    try:
        w, res = engine_connect(store, _log)
        if not w:
            st["state"] = "failed"
            st["error"] = res.get("reason") or "云端连接失败"
            return
        cid, fitem = _restore_target(w, t, path)
        os.makedirs(dest, exist_ok=True)
        engine_fix_perm(dest, True)
        if fitem is not None:
            out = os.path.join(dest, fitem.get("name") or "restored.bin")
            n = engine_download(w, fitem, out)
            engine_fix_perm(out, False)
            st.update({"files": 1, "bytes": n, "skipped": 0, "state": "done"})
            _log("完成：%s（%.1f MB）" % (fitem.get("name"), n / 1048576.0))
            return
        if not cid:
            st["state"] = "failed"
            st["error"] = "云端路径不存在"
            return
        files, nbytes, skipped = engine_download_tree(w, cid, dest, log=_log)
        st.update({"files": files, "bytes": nbytes, "skipped": skipped,
                   "state": "done"})
        _log("完成：%d 个文件 / %.1f MB%s" % (
            files, nbytes / 1048576.0,
            "，%d 个跳过" % skipped if skipped else ""))
    except Exception as e:
        st["state"] = "failed"
        st["error"] = str(e)[:200]
        _log("失败：%s" % str(e)[:160])


@app.post("/api/restore")
def restore(d: RestoreIn, _=Depends(auth)):
    t = db.get_task(d.task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if not d.dest:
        raise HTTPException(400, "请指定恢复到哪个目录")
    dest = os.path.realpath(d.dest)
    if not any(dest == r or dest.startswith(r.rstrip("/") + "/") for r in ALLOWED_ROOTS):
        raise HTTPException(403, "目标目录超出允许范围")
    jid = secrets.token_hex(6)
    with _REST_LK:
        _REST[jid] = {"id": jid, "task": t["name"], "path": d.path, "dest": dest,
                      "state": "queued", "files": 0, "bytes": 0, "skipped": 0,
                      "error": "", "log": [],
                      "started": time.strftime("%F %T")}
        for k in list(_REST.keys())[:-20]:
            _REST.pop(k, None)
    threading.Thread(target=_do_restore, args=(jid, t, d.path, dest),
                     daemon=True).start()
    return {"id": jid}


@app.get("/api/restore/{jid}")
def restore_status(jid: str, _=Depends(auth)):
    st = _REST.get(jid)
    if not st:
        raise HTTPException(404, "任务不存在或已过期")
    out = dict(st)
    out["log"] = st["log"][-40:]
    return out


# ── 孤儿扫描：只列清单、不删除 ──────────────────────────────
_ORPH, _ORPH_LK = {}, threading.Lock()


def _do_orphans(jid, t):
    st = _ORPH[jid]
    st["state"] = "running"

    def _log(m):
        st["log"].append("%s %s" % (time.strftime("%H:%M:%S"), m))
        if len(st["log"]) > 400:
            del st["log"][:100]

    try:
        w, res = engine_connect(store, _log)
        if not w:
            st["state"] = "failed"
            st["error"] = res.get("reason") or "云端连接失败"
            return
        excludes = json.loads(t.get("excludes") or "[]")
        chain = _cloud_chain(t)
        r = scan_orphans(w, t.get("src") or "", chain, excludes, log=_log)
        st.update({"files": r["files"], "dirs": r["dirs"],
                   "total_files": r["total_files"],
                   "total_bytes": r["total_bytes"],
                   "truncated": r["truncated"], "note": r.get("note") or "",
                   "state": "done"})
        _log("完成：孤儿文件 %d 个、孤儿目录 %d 棵，共 %d 个文件 / %.1f MB%s" % (
            len(r["files"]), len(r["dirs"]), r["total_files"],
            r["total_bytes"] / 1048576.0,
            "（清单已截断）" if r["truncated"] else ""))
    except Exception as e:
        st["state"] = "failed"
        st["error"] = str(e)[:200]
        _log("失败：%s" % str(e)[:160])


@app.post("/api/tasks/{tid}/orphans")
def start_orphans(tid: int, _=Depends(auth)):
    t = db.get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if (t.get("mode") or "dir") != "dir":
        raise HTTPException(400, "仅 dir 任务支持孤儿扫描")
    with _ORPH_LK:
        for st in _ORPH.values():
            if st.get("task_id") == tid and st["state"] in ("queued", "running"):
                raise HTTPException(409, "该任务已有扫描在进行")
        jid = secrets.token_hex(6)
        _ORPH[jid] = {"id": jid, "task_id": tid, "task": t["name"],
                      "state": "queued", "files": [], "dirs": [],
                      "total_files": 0, "total_bytes": 0, "truncated": False,
                      "note": "", "error": "", "log": [],
                      "started": time.strftime("%F %T")}
        for k in list(_ORPH.keys())[:-20]:
            _ORPH.pop(k, None)
    threading.Thread(target=_do_orphans, args=(jid, t), daemon=True).start()
    return {"id": jid}


@app.get("/api/orphans/{jid}")
def orphan_status(jid: str, _=Depends(auth)):
    st = _ORPH.get(jid)
    if not st:
        raise HTTPException(404, "任务不存在或已过期")
    out = dict(st)
    out["log"] = st["log"][-40:]
    return out


# ── 恢复演练报告：drill.py 上报，面板展示 ───────────────────
@app.post("/api/drill/report")
async def drill_report(req: Request, _=Depends(auth)):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "报告必须是 JSON 对象")
    if not isinstance(body, dict):
        raise HTTPException(400, "报告必须是 JSON 对象")
    d = os.path.join(DATA, "state")
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".drill.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(d, "drill.json"))
    return {"ok": True}


@app.get("/api/drill")
def drill_status(_=Depends(auth)):
    p = os.path.join(DATA, "state", "drill.json")
    if not os.path.exists(p):
        return {"available": False}
    try:
        with open(p, encoding="utf-8") as f:
            rep = json.load(f)
    except Exception:
        return {"available": False}
    age_days, stale = None, False
    try:
        t = datetime.strptime(rep.get("at") or "", "%F %T")
        age_days = (datetime.now() - t).days
        stale = age_days > 35
    except Exception:
        pass
    out = dict(rep)
    out.update({"available": True, "stale": stale, "age_days": age_days})
    return out


# ── 静态页 ──────────────────────────────────────────────────
STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.get("/")
def index():
    # 迭代期禁缓存：保证每次刷新都拿最新页面，避免「改完还显示旧版」的困惑
    return FileResponse(os.path.join(STATIC, "index.html"),
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")

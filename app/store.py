#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 管理与运行记录存储"""
import json
import os
import sqlite3
import threading
import time

TOKEN_TTL = 604799
TOKEN_RENEW_AHEAD = 2 * 86400


class TokenStore:
    """token 的落盘与续期。

    演进要点：旧脚本的 token 与 OpenList 双向同步（写完库还要调它的 API 重载）。
    本系统作为主管理者独立持有 token；若仍要喂给 OpenList，
    把 sync_openlist 打开即可（但 reload 需要宿主机配合，见 README）。
    """

    def __init__(self, path, openlist_db=None, sync_openlist=False, log=print):
        self.path = path
        self.openlist_db = openlist_db
        self.sync_openlist = sync_openlist
        self.log = log
        os.makedirs(os.path.dirname(path), exist_ok=True)

    # ── 读写 ──────────────────────────────────────────────
    def load(self):
        if os.path.exists(self.path):
            try:
                d = json.load(open(self.path, encoding="utf-8"))
                if d.get("access_token"):
                    return d["access_token"], d.get("refresh_token")
            except Exception:
                pass
        # 首次启动：尝试从 OpenList 库导入，免得从头授权
        if self.openlist_db and os.path.exists(self.openlist_db):
            try:
                c = sqlite3.connect("file:%s?mode=ro" % self.openlist_db, uri=True)
                row = c.execute("select addition from x_storages where id=1").fetchone()
                c.close()
                if row:
                    a = json.loads(row[0])
                    at, rt = a.get("access_token"), a.get("refresh_token")
                    if at:
                        self.log("已从 OpenList 库导入初始 token")
                        self.save(at, rt)
                        return at, rt
            except Exception as e:
                self.log("从 OpenList 导入 token 失败: %s" % e)
        return None, None

    def save(self, at, rt):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"access_token": at, "refresh_token": rt,
                       "updated": time.strftime("%F %T")}, f)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass
        if self.sync_openlist:
            self._push_openlist(at, rt)

    def _push_openlist(self, at, rt):
        """只写库，不重载。OpenList 会把存储配置读进内存，
        改库后需要它自己 reload 才生效（宿主机可加定时 reload）。"""
        if not self.openlist_db or not os.path.exists(self.openlist_db):
            return
        try:
            c = sqlite3.connect(self.openlist_db, timeout=15)
            row = c.execute("select addition from x_storages where id=1").fetchone()
            if row:
                a = json.loads(row[0])
                a["access_token"], a["refresh_token"] = at, rt
                c.execute("update x_storages set addition=? where id=1",
                          (json.dumps(a, ensure_ascii=False),))
                c.commit()
            c.close()
        except Exception as e:
            self.log("同步 token 到 OpenList 失败（不影响直传）: %s" % e)

    # ── 有效期 ────────────────────────────────────────────
    def age(self):
        try:
            d = json.load(open(self.path, encoding="utf-8"))
            return time.time() - time.mktime(
                time.strptime(d.get("updated") or "", "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return None

    def info(self):
        try:
            d = json.load(open(self.path, encoding="utf-8"))
            age = self.age()
            remain = (TOKEN_TTL - age) if age is not None else None
            return {"updated": d.get("updated"), "age_sec": age,
                    "remain_sec": remain,
                    "remain_days": round(remain / 86400.0, 2) if remain else None,
                    "has_refresh": bool(d.get("refresh_token"))}
        except Exception:
            return {"updated": None, "age_sec": None, "remain_sec": None,
                    "remain_days": None, "has_refresh": False}

    def maybe_renew(self, w, log=print):
        age = self.age()
        if age is None:
            log("token 没有签发时间记录，跳过提前续期")
            return False
        remain = TOKEN_TTL - age
        if remain > TOKEN_RENEW_AHEAD:
            log("token 剩余约 %.1f 天，无需续期" % (remain / 86400.0))
            return False
        log("token 剩余约 %.1f 天（阈值 %d 天），主动续期" % (
            remain / 86400.0, TOKEN_RENEW_AHEAD // 86400))
        try:
            w.refresh()
            return True
        except Exception as e:
            log("提前续期失败，继续用旧 token: %s" % e)
            return False

    def recover(self, w, prefer_db=True):
        """凭证报错后的恢复。

        顺序很重要：**先自己刷新，刷新不了再认 OpenList 库里那份**。
        反过来（先认库）有风险——如果 OpenList 库里的副本比本地旧
        （例如本应用刚刷新完、回写库失败），采纳它会把本来好的凭证换坏。
        只有在本地刷新失败时才说明凭证链被另一方更新过，这时库里的才是新的。
        """
        try:
            w.refresh()
            return True
        except Exception as e:
            self.log("本地刷新 token 失败: %s" % e)

        if not (self.openlist_db and os.path.exists(self.openlist_db)):
            return False
        try:
            c = sqlite3.connect("file:%s?mode=ro" % self.openlist_db, uri=True)
            row = c.execute("select addition from x_storages where id=1").fetchone()
            c.close()
            if not row:
                return False
            a = json.loads(row[0])
            at2, rt2 = a.get("access_token"), a.get("refresh_token")
            if at2 and at2 != w.at:
                self.log("采用 OpenList 库里的较新 token 重试")
                w.at, w.rt = at2, (rt2 or w.rt)
                w.key = w.at[:16].encode()
                self.save(w.at, w.rt)
                return True
        except Exception as e:
            self.log("读取 OpenList 库失败: %s" % e)
        return False


# ── SQLite ─────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  src TEXT NOT NULL,
  cloud TEXT NOT NULL,
  cron TEXT DEFAULT '',
  excludes TEXT DEFAULT '[]',
  workers INTEGER DEFAULT 4,
  enabled INTEGER DEFAULT 1,
  note TEXT DEFAULT '',
  mode TEXT DEFAULT 'dir',
  keep INTEGER DEFAULT 0,
  pattern TEXT DEFAULT '',
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,
  task_name TEXT,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  todo INTEGER DEFAULT 0,
  ok_n INTEGER DEFAULT 0,
  fail INTEGER DEFAULT 0,
  bytes INTEGER DEFAULT 0,
  elapsed REAL DEFAULT 0,
  reason TEXT DEFAULT '',
  detail TEXT DEFAULT '',
  log TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, id DESC);
"""

# 老库补列：SQLite 没有 IF NOT EXISTS 的 ADD COLUMN，只能逐列尝试
MIGRATIONS = [
    ("mode", "ALTER TABLE tasks ADD COLUMN mode TEXT DEFAULT 'dir'"),
    ("keep", "ALTER TABLE tasks ADD COLUMN keep INTEGER DEFAULT 0"),
    ("pattern", "ALTER TABLE tasks ADD COLUMN pattern TEXT DEFAULT ''"),
    # Uptime Kuma 心跳地址：每次运行结束推一次状态，失败时带上原因。
    # 单独用 set_kuma_url 写入，不走 upsert_task，避免表单保存时被清空。
    ("kuma_url", "ALTER TABLE tasks ADD COLUMN kuma_url TEXT DEFAULT ''"),
]


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self._lk = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            have = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            for col, sql in MIGRATIONS:
                if col not in have:
                    c.execute(sql)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        return c

    # ── tasks ─────────────────────────────────────────────
    def list_tasks(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute("select * from tasks order by id")]

    def get_task(self, tid):
        with self._conn() as c:
            r = c.execute("select * from tasks where id=?", (tid,)).fetchone()
            return dict(r) if r else None

    def get_task_by_name(self, name):
        with self._conn() as c:
            r = c.execute("select * from tasks where name=?", (name,)).fetchone()
            return dict(r) if r else None

    def upsert_task(self, d):
        now = time.strftime("%F %T")
        mode = d.get("mode") or "dir"
        keep = int(d.get("keep") or 0)
        pattern = d.get("pattern") or ""
        with self._lk, self._conn() as c:
            if d.get("id"):
                c.execute("""update tasks set name=?,src=?,cloud=?,cron=?,excludes=?,
                             workers=?,enabled=?,note=?,mode=?,keep=?,pattern=?,
                             updated_at=? where id=?""",
                          (d["name"], d["src"], json.dumps(d["cloud"], ensure_ascii=False),
                           d.get("cron", ""), json.dumps(d.get("excludes", []), ensure_ascii=False),
                           d.get("workers", 4), 1 if d.get("enabled", True) else 0,
                           d.get("note", ""), mode, keep, pattern, now, d["id"]))
                return d["id"]
            cur = c.execute("""insert into tasks(name,src,cloud,cron,excludes,workers,
                               enabled,note,mode,keep,pattern,created_at,updated_at)
                               values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (d["name"], d["src"], json.dumps(d["cloud"], ensure_ascii=False),
                             d.get("cron", ""), json.dumps(d.get("excludes", []), ensure_ascii=False),
                             d.get("workers", 4), 1 if d.get("enabled", True) else 0,
                             d.get("note", ""), mode, keep, pattern, now, now))
            return cur.lastrowid

    def clear_stale_runs(self):
        """容器重启会把跑一半的记录永久留在 running，挡住调度。启动时清扫。"""
        with self._lk, self._conn() as c:
            cur = c.execute("""update runs set status='interrupted',
                               finished_at=?, reason='container-restart'
                               where status='running'""",
                            (time.strftime("%F %T"),))
            return cur.rowcount

    def set_kuma_url(self, tid, url):
        """单独设置心跳地址。不放进 upsert_task —— 界面的任务表单没有这个字段，
        若跟着表单走，每次在界面上点保存都会把心跳地址清空。"""
        with self._lk, self._conn() as c:
            c.execute("update tasks set kuma_url=?, updated_at=? where id=?",
                      (url or "", time.strftime("%F %T"), tid))

    def delete_task(self, tid):
        with self._lk, self._conn() as c:
            c.execute("delete from tasks where id=?", (tid,))
            c.execute("delete from runs where task_id=?", (tid,))

    # ── runs ──────────────────────────────────────────────
    def start_run(self, task_id, task_name):
        with self._lk, self._conn() as c:
            cur = c.execute("""insert into runs(task_id,task_name,started_at,status)
                               values(?,?,?,'running')""",
                            (task_id, task_name, time.strftime("%F %T")))
            return cur.lastrowid

    def finish_run(self, rid, res, status, logtext=""):
        with self._lk, self._conn() as c:
            c.execute("""update runs set finished_at=?,status=?,todo=?,ok_n=?,fail=?,
                         bytes=?,elapsed=?,reason=?,detail=?,log=? where id=?""",
                      (time.strftime("%F %T"), status, res.get("todo", 0),
                       res.get("ok_n", 0), res.get("fail", 0), res.get("bytes", 0),
                       res.get("elapsed", 0), res.get("reason", ""),
                       res.get("detail", ""), logtext[-20000:], rid))

    def list_runs(self, task_id=None, limit=50):
        with self._conn() as c:
            if task_id:
                rows = c.execute("select id,task_id,task_name,started_at,finished_at,status,"
                                 "todo,ok_n,fail,bytes,elapsed,reason,detail from runs "
                                 "where task_id=? order by id desc limit ?", (task_id, limit))
            else:
                rows = c.execute("select id,task_id,task_name,started_at,finished_at,status,"
                                 "todo,ok_n,fail,bytes,elapsed,reason,detail from runs "
                                 "order by id desc limit ?", (limit,))
            return [dict(r) for r in rows]

    def get_run(self, rid):
        with self._conn() as c:
            r = c.execute("select * from runs where id=?", (rid,)).fetchone()
            return dict(r) if r else None

    def last_run(self, task_id):
        with self._conn() as c:
            r = c.execute("select * from runs where task_id=? order by id desc limit 1",
                          (task_id,)).fetchone()
            return dict(r) if r else None

    def last_real_run(self, task_id):
        """最近一次「真实运行」。预览(dry-run)不动云端，不该代表备份状态。"""
        with self._conn() as c:
            r = c.execute(
                "select * from runs where task_id=? and status!='dry-run' "
                "order by id desc limit 1", (task_id,)).fetchone()
            return dict(r) if r else None

    def last_success(self, task_id):
        with self._conn() as c:
            r = c.execute(
                "select * from runs where task_id=? and status='success' "
                "order by id desc limit 1", (task_id,)).fetchone()
            return dict(r) if r else None

    def prune_runs(self, keep_per_task=200):
        with self._lk, self._conn() as c:
            c.execute("""delete from runs where id in (
                           select id from runs r1 where (
                             select count(*) from runs r2
                             where r2.task_id=r1.task_id and r2.id>r1.id
                           ) >= ?)""", (keep_per_task,))

    def prune_runs_by_age(self, days=120):
        """按时间清理运行记录：runs 表只增不删，一年后会拖慢查询。
        与 prune_runs（按条数）配合，双保险。"""
        with self._lk, self._conn() as c:
            cur = c.execute(
                "delete from runs where started_at < date('now','localtime',?)",
                ("-%d day" % int(days),))
            return cur.rowcount

    def vacuum(self):
        """回收删除后的空页。必须在没有活动事务时执行。"""
        try:
            c = sqlite3.connect(self.path, timeout=30)
            c.execute("VACUUM")
            c.close()
            return True
        except Exception:
            return False

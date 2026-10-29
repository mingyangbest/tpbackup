#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务执行器：后台线程跑同步，提供实时日志与停止能力"""
import json
import os
import threading
import time
import traceback

from . import engine
from .notify import kuma


class RunState:
    def __init__(self, task_id, run_id, name, dry_run=False):
        self.task_id = task_id
        self.run_id = run_id
        self.name = name
        self.dry_run = bool(dry_run)
        self.logs = []
        self.progress = {"phase": "start", "done": 0, "total": 0, "bytes": 0, "name": ""}
        self.stop_flag = False
        self.started = time.time()
        self.finished = None

    def log(self, msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        self.logs.append(line)
        if len(self.logs) > 2000:
            del self.logs[:500]

    def to_dict(self):
        return {
            "task_id": self.task_id, "task_name": self.name, "run_id": self.run_id,
            "running": self.finished is None, "dry_run": self.dry_run,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "progress": self.progress,
            "log_tail": self.logs[-120:],
        }


class Runner:
    def __init__(self, db, store, logs_dir, on_change=None):
        self.db = db
        self.store = store
        self.logs_dir = logs_dir
        self.on_change = on_change
        self._states = {}
        self._lock = threading.Lock()
        os.makedirs(logs_dir, exist_ok=True)

    # ── 查询 ──────────────────────────────────────────────
    def is_running(self, task_id):
        with self._lock:
            st = self._states.get(task_id)
            return bool(st and st.finished is None)

    def is_dry_running(self, task_id):
        """该任务当前跑的是不是预览（dry-run）。界面用它区分「运行中」和「预览中」。"""
        with self._lock:
            st = self._states.get(task_id)
            return bool(st and st.finished is None and st.dry_run)

    def running_list(self):
        with self._lock:
            return [s.to_dict() for s in self._states.values() if s.finished is None]

    def get_state(self, task_id):
        with self._lock:
            st = self._states.get(task_id)
            return st.to_dict() if st else None

    def request_stop(self, task_id):
        with self._lock:
            st = self._states.get(task_id)
            if st and st.finished is None:
                st.stop_flag = True
                st.log("收到停止请求，等待当前文件传完后中止")
                return True
        return False

    # ── 执行 ──────────────────────────────────────────────
    def start(self, task, dry_run=False):
        """异步启动一次备份。返回 run_id；若已在跑则返回 None。"""
        tid = task["id"]
        with self._lock:
            st = self._states.get(tid)
            if st and st.finished is None:
                return None

        run_id = self.db.start_run(tid, task["name"])
        st = RunState(tid, run_id, task["name"], dry_run=dry_run)
        with self._lock:
            self._states[tid] = st

        th = threading.Thread(target=self._run, args=(task, st, dry_run), daemon=True)
        th.start()
        return run_id

    def _run(self, task, st, dry_run):
        logfile = os.path.join(self.logs_dir, "%s.log" % _safe(task["name"]))
        fh = None
        try:
            # 单文件超过 2MB 就轮转成 .old（只留一代）：无上限的日志几年后会到几十 GB
            try:
                if os.path.exists(logfile) and os.path.getsize(logfile) > 2 * 1048576:
                    os.replace(logfile, logfile + ".old")
            except Exception:
                pass
            fh = open(logfile, "a", encoding="utf-8")
            fh.write("\n===== %s 开始 dry_run=%s =====\n" % (
                time.strftime("%F %T"), dry_run))
            fh.flush()

            def log(msg):
                st.log(msg)
                if fh:
                    try:
                        fh.write(str(msg) + "\n")
                        fh.flush()
                    except Exception:
                        pass

            def progress(p):
                st.progress = p

            cloud = task["cloud"] if isinstance(task["cloud"], list) else \
                json.loads(task["cloud"] or "[]")
            excludes = task["excludes"] if isinstance(task["excludes"], list) else \
                json.loads(task["excludes"] or "[]")

            log("源目录: %s" % task["src"])
            log("云端路径: %s" % "/".join(cloud))

            res = engine.sync(
                store=self.store, src=task["src"], cloud_chain=cloud,
                excludes_dirs=excludes, excludes_files=None,
                dry_run=dry_run, workers=task.get("workers") or 4,
                log=log, progress=progress, should_stop=lambda: st.stop_flag,
            )

            if st.stop_flag:
                status = "stopped"
            elif dry_run:
                status = "dry-run"
            elif res.get("ok"):
                status = "success"
            else:
                status = "failed"

            logtext = "\n".join(st.logs)
            self.db.finish_run(st.run_id, res, status, logtext)
            log("状态: %s%s" % (status, (" / " + res.get("reason", "")) if res.get("reason") else ""))

            # 心跳只报真实结果：试运行和手动停止不喂，否则会掩盖真正的失败
            if status in ("success", "failed"):
                if status == "success":
                    msg = "OK 待传%s 成功%s %.1fMB %.0fs" % (
                        res.get("todo", 0), res.get("ok_n", 0),
                        (res.get("bytes") or 0) / 1048576, res.get("elapsed") or 0)
                else:
                    msg = "FAIL %s %s" % (res.get("reason") or "", res.get("detail") or "")
                kuma(task.get("kuma_url"), status == "success", msg,
                     int((res.get("elapsed") or 0) * 1000))
        except Exception:
            tb = traceback.format_exc()
            st.log("未捕获异常:\n" + tb)
            self.db.finish_run(st.run_id, {"todo": 0, "ok_n": 0, "fail": 0,
                                           "bytes": 0, "elapsed": 0,
                                           "reason": "exception", "detail": tb[:400]},
                               "failed", "\n".join(st.logs))
            kuma(task.get("kuma_url"), False, "FAIL exception %s" % tb.strip().splitlines()[-1][:150])
        finally:
            st.finished = time.time()
            if fh:
                try:
                    fh.write("===== %s 结束 =====\n" % time.strftime("%F %T"))
                    fh.close()
                except Exception:
                    pass
            try:
                self.db.prune_runs()
            except Exception:
                pass
            if self.on_change:
                try:
                    self.on_change()
                except Exception:
                    pass


def _safe(name):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:64]

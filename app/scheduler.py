#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置调度：从数据库读 cron，交给 APScheduler"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("tpbackup.scheduler")


class Scheduler:
    def __init__(self, db, runner):
        self.db = db
        self.runner = runner
        self.sched = BackgroundScheduler(timezone="Asia/Shanghai")
        self._jobs = {}

    def start(self):
        self.sched.start()
        self.reload()
        # 每日维护：清运行记录 + 回收数据库空间。runs 只增不删，
        # 跑满一年后列表查询和 /api/runs 都会变慢。
        try:
            self.sched.add_job(
                self._maintenance,
                CronTrigger(hour=5, minute=20, timezone="Asia/Shanghai"),
                id="maintenance", replace_existing=True, coalesce=True,
                misfire_grace_time=3600)
        except Exception as e:
            log.warning("注册每日维护任务失败: %s", e)
        log.info("调度器已启动，已注册 %d 个任务", len(self._jobs))

    def _maintenance(self):
        try:
            old = self.db.prune_runs_by_age(120)
            self.db.prune_runs(300)      # 每个任务再保留最近 300 条
            ok = self.db.vacuum()
            log.info("每日维护完成：删除 %s 条过期记录 / VACUUM=%s", old, ok)
        except Exception as e:
            log.warning("每日维护失败: %s", e)

    def shutdown(self):
        try:
            self.sched.shutdown(wait=False)
        except Exception:
            pass

    def reload(self):
        """按数据库重建全部 job。任务增删改后调用。"""
        for jid, job in list(self._jobs.items()):
            try:
                job.remove()
            except Exception:
                pass
        self._jobs.clear()

        for t in self.db.list_tasks():
            if not t.get("enabled"):
                continue
            # push 型任务由宿主机脚本主动调用接口触发，本调度器不管，
            # 否则会和宿主机 cron 抢着跑、或者对着一个目录做无意义的同步。
            if (t.get("mode") or "dir") == "push":
                continue
            cron = (t.get("cron") or "").strip()
            if not cron:
                continue
            try:
                trigger = CronTrigger.from_crontab(cron, timezone="Asia/Shanghai")
            except Exception as e:
                log.warning("任务 %s 的 cron 无效（%s）: %s", t["name"], cron, e)
                continue
            jid = "task-%d" % t["id"]
            try:
                job = self.sched.add_job(
                    self._fire, trigger, id=jid, args=[t["id"]],
                    max_instances=1, coalesce=True, misfire_grace_time=3600,
                    replace_existing=True)
                self._jobs[jid] = job
            except Exception as e:
                log.warning("注册任务 %s 失败: %s", t["name"], e)
        return len(self._jobs)

    def _fire(self, task_id):
        t = self.db.get_task(task_id)
        if not t or not t.get("enabled"):
            return
        if self.runner.is_running(task_id):
            log.info("任务 %s 上一轮仍在运行，跳过本次调度", t["name"])
            return
        log.info("定时触发: %s", t["name"])
        self.runner.start(t)

    def next_runs(self):
        """返回 {task_id: 下次运行时间}"""
        out = {}
        for jid, job in self._jobs.items():
            try:
                tid = int(jid.split("-")[1])
                nrt = job.next_run_time
                out[tid] = nrt.strftime("%F %T") if nrt else None
            except Exception:
                pass
        return out

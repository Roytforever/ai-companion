"""定时调度（对齐 Hermes Cron：「定时自动化」）。

实现要点（对齐 docs/developer-guide/cron-internals）：
- 四种调度格式：相对延迟(30m/2h/1d)、间隔(every 2h)、cron 表达式(0 9 * * *)、ISO 时间戳。
- 后台线程每 SCHEDULER_TICK_SECONDS 秒 tick：加（进程内+文件）锁 → 加载 jobs.json →
  筛选到期(next_run<=now 且 state=scheduled 且 enabled) → 每个在「全新隔离会话」(SubAgent)执行
  → 投递结果 → 更新 next_run / run_count / state。
- 存储：data/schedules/jobs.json（原子写：临时文件 + os.replace，对齐 Hermes cron/jobs.py）。
- 递归防护：cron 会话禁用 cronjob/moa/delegate_task/subagent_run。
- 技能注入：cron 任务可通过 skills 字段附加已学技能，执行时以 SKILL.md 正文作为上下文注入。
- 投递：local(写 data/schedules/output/ 文件) / ui(应用收件箱，持久化 inbox.json)。
  [本地伴侣无 Telegram/Discord 等外部网关，故 deliver 默认 ui]
"""
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta

from config import settings
from tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------- 调度格式解析 ----------------

_CRON_RANGES = {
    "m": (0, 59), "h": (0, 23), "dom": (1, 31), "mon": (1, 12), "dow": (0, 7),
}


def _timedelta_from_unit(n: int, unit: str) -> timedelta:
    return {
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[unit]


def _cron_allowed(field: str, ftype: str) -> set:
    """把单个 cron 字段展开成允许的整数集合（支持 * , - / 与 dow 7==0）。"""
    lo, hi = _CRON_RANGES[ftype]
    field = (field or "*").strip()
    if field in ("*", ""):
        vals = set(range(lo, hi + 1))
    elif "/" in field:
        left, step = field.split("/", 1)
        step = int(step)
        if "-" in left:
            a, b = left.split("-")
            start, end = int(a), int(b)
        elif left in ("*", ""):
            start, end = lo, hi
        else:
            start = end = int(left)
        vals = {v for v in range(start, end + 1) if (v - start) % step == 0}
    elif "-" in field:
        a, b = field.split("-")
        vals = set(range(int(a), int(b) + 1))
    else:
        vals = {int(field)}
    if ftype == "dow":
        if 0 in vals:
            vals.add(7)
        if 7 in vals:
            vals.add(0)
    return vals


def _cron_matches(parts: list, dt: datetime) -> bool:
    m, h, dom, mon, dow = parts
    if dt.minute not in _cron_allowed(m, "m"):
        return False
    if dt.hour not in _cron_allowed(h, "h"):
        return False
    if dt.month not in _cron_allowed(mon, "mon"):
        return False
    dom_allowed = _cron_allowed(dom, "dom")
    dow_allowed = _cron_allowed(dow, "dow")
    dom_star = dom in ("*", "")
    dow_star = dow in ("*", "")
    cron_dow = dt.isoweekday() % 7  # 周日=0 ... 周六=6
    if dom_star and dow_star:
        return True
    if dom_star:
        return cron_dow in dow_allowed
    if dow_star:
        return dt.day in dom_allowed
    return (dt.day in dom_allowed) or (cron_dow in dow_allowed)


def _next_cron(parts: list, after: datetime) -> datetime:
    cand = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366 * 4)  # 4 年上限，防死循环
    step = timedelta(minutes=1)
    while cand <= limit:
        if _cron_matches(parts, cand):
            return cand
        cand += step
    raise ValueError("4 年内无匹配时间（cron 表达式可能无解，如 2 月 30 日）")


def parse_schedule(spec: str, now: datetime | None = None) -> dict:
    """解析调度规格 → {kind, expr, display, next_run, single_shot, delta_sec?}。"""
    now = now or datetime.now()
    s = (spec or "").strip()
    if not s:
        raise ValueError("调度格式为空")

    # ISO 时间戳
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", s):
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return {"kind": "iso", "expr": s, "display": s,
                "next_run": dt, "single_shot": True}

    # 相对延迟：30m / 2h / 1d
    m = re.match(r"^(\d+)\s*([mhd])$", s, re.I)
    if m:
        delta = _timedelta_from_unit(int(m.group(1)), m.group(2).lower())
        return {"kind": "delay", "expr": s, "display": s,
                "next_run": now + delta, "single_shot": True}

    # 间隔：every 2h / every 30m
    m = re.match(r"^every\s+(\d+)\s*([mhd])$", s, re.I)
    if m:
        delta = _timedelta_from_unit(int(m.group(1)), m.group(2).lower())
        return {"kind": "interval", "expr": s, "display": s,
                "next_run": now + delta, "single_shot": False,
                "delta_sec": delta.total_seconds()}

    # cron 表达式（5 字段）
    parts = s.split()
    if len(parts) == 5:
        try:
            nxt = _next_cron(parts, now)
        except ValueError as e:
            raise ValueError(f"无效 cron 表达式 `{s}`：{e}")
        return {"kind": "cron", "expr": s, "display": s,
                "next_run": nxt, "single_shot": False}

    raise ValueError(
        "无法识别的调度格式，支持：30m/2h/1d（相对延迟）、every 2h（间隔）、"
        "0 9 * * *（cron 表达式）、2025-01-15T09:00:00（ISO 时间戳）"
    )


# ---------------- 调度器 ----------------

class Scheduler:
    def __init__(self):
        self.data_dir = settings.SCHEDULER_DATA_DIR
        self.jobs_path = self.data_dir / "jobs.json"
        self.inbox_path = self.data_dir / "inbox.json"
        self.tick_seconds = settings.SCHEDULER_TICK_SECONDS
        self.enabled = settings.SCHEDULER_ENABLED
        self.agent = None
        self._lock = threading.Lock()       # 进程内锁
        self._flock = None                  # 跨进程文件锁句柄
        self._file_lock_path = self.data_dir / ".scheduler.lock"
        self._thread = None
        self._stop = threading.Event()
        self._dir_ready = False
        self.inbox = self._load_inbox()

    # ---- 目录 / 存储 ----
    def _ensure_dir(self):
        if not self._dir_ready:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._dir_ready = True

    def _load_jobs(self) -> dict:
        self._ensure_dir()
        if not self.jobs_path.exists():
            return {}
        try:
            return json.loads(self.jobs_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("jobs.json 解析失败，返回空")
            return {}

    def _save_jobs(self, jobs: dict):
        self._ensure_dir()
        tmp = self.jobs_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.jobs_path)  # 原子写

    def _load_inbox(self) -> list:
        self._ensure_dir()
        if self.inbox_path.exists():
            try:
                return json.loads(self.inbox_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_inbox(self):
        self._ensure_dir()
        tmp = self.inbox_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.inbox[-200:], ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.inbox_path)

    def set_agent(self, agent):
        self.agent = agent

    # ---- 工具方法 ----
    def _find_id(self, jobs: dict, ref: str) -> str | None:
        if not ref:
            return None
        if ref in jobs:
            return ref
        ref_l = ref.lower()
        for jid, j in jobs.items():
            if jid.startswith(ref_l):
                return jid
            if (j.get("name") or "").lower() == ref_l:
                return jid
        return None

    def _skill_context(self, names: list) -> str:
        if not self.agent:
            return ""
        learned = {s["name"]: s for s in self.agent.skill_evolver.list_learned()}
        blocks = []
        for n in names:
            s = learned.get(n) or learned.get(n.lower())
            if s:
                blocks.append(f"# 技能：{s['name']}\n{s.get('body', '')}")
        return "\n\n".join(blocks)

    def _compute_next(self, job: dict) -> datetime:
        sch = job["schedule"]
        kind = sch["kind"]
        now = datetime.now()
        if kind == "interval":
            return now + timedelta(seconds=job.get("delta_sec") or 3600)
        if kind == "cron":
            return _next_cron(sch["expr"].split(), now)
        return now + timedelta(hours=24)

    # ---- CRUD ----
    def create(self, name, prompt, schedule, skills=None, deliver="ui", repeat=None) -> dict:
        if not name or not prompt or not schedule:
            return {"ok": False, "error": "create 需要 name + prompt + schedule"}
        try:
            parsed = parse_schedule(schedule)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        jobs = self._load_jobs()
        jid = uuid.uuid4().hex[:12]
        now = datetime.now()
        jobs[jid] = {
            "id": jid,
            "name": name,
            "prompt": prompt,
            "schedule": {"kind": parsed["kind"], "expr": parsed["expr"],
                         "display": parsed["display"]},
            "skills": skills or [],
            "deliver": (deliver or "ui").lower(),
            "repeat": {"times": repeat, "completed": 0},
            "state": "scheduled",
            "enabled": True,
            "next_run_at": parsed["next_run"].isoformat(),
            "last_run_at": None,
            "last_status": None,
            "created_at": now.isoformat(),
            "single_shot": parsed["single_shot"],
            "delta_sec": parsed.get("delta_sec"),
            "result": None,
        }
        self._save_jobs(jobs)
        return {"ok": True, "id": jid, "name": name,
                "next_run_at": jobs[jid]["next_run_at"],
                "schedule_display": parsed["display"]}

    def list_jobs(self) -> list:
        jobs = self._load_jobs()
        out = []
        for j in jobs.values():
            out.append({
                "id": j.get("id"),
                "name": j.get("name"),
                "schedule": j.get("schedule"),
                "next_run_at": j.get("next_run_at"),
                "state": j.get("state"),
                "enabled": j.get("enabled"),
                "deliver": j.get("deliver"),
                "repeat": j.get("repeat"),
                "last_status": j.get("last_status"),
                "last_run_at": j.get("last_run_at"),
            })
        return out

    def update(self, job_id_or_name, **fields) -> dict:
        jobs = self._load_jobs()
        jid = self._find_id(jobs, job_id_or_name)
        if not jid:
            return {"ok": False, "error": "未找到任务"}
        j = jobs[jid]
        for k in ("name", "prompt", "skills", "deliver"):
            if fields.get(k) is not None:
                j[k] = fields[k]
        if fields.get("schedule"):
            try:
                parsed = parse_schedule(fields["schedule"])
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            j["schedule"] = {"kind": parsed["kind"], "expr": parsed["expr"],
                             "display": parsed["display"]}
            j["next_run_at"] = parsed["next_run"].isoformat()
            j["single_shot"] = parsed["single_shot"]
            j["delta_sec"] = parsed.get("delta_sec")
        if fields.get("repeat") is not None:
            j["repeat"] = {"times": fields["repeat"],
                           "completed": j["repeat"].get("completed", 0)}
        self._save_jobs(jobs)
        return {"ok": True, "id": jid}

    def _set_state(self, job_id_or_name, enabled, state) -> dict:
        jobs = self._load_jobs()
        jid = self._find_id(jobs, job_id_or_name)
        if not jid:
            return {"ok": False, "error": "未找到任务"}
        jobs[jid]["enabled"] = enabled
        jobs[jid]["state"] = state
        self._save_jobs(jobs)
        return {"ok": True, "id": jid}

    def pause(self, job_id_or_name) -> dict:
        return self._set_state(job_id_or_name, False, "paused")

    def resume(self, job_id_or_name) -> dict:
        return self._set_state(job_id_or_name, True, "scheduled")

    def remove(self, job_id_or_name) -> dict:
        jobs = self._load_jobs()
        jid = self._find_id(jobs, job_id_or_name)
        if not jid:
            return {"ok": False, "error": "未找到任务"}
        del jobs[jid]
        self._save_jobs(jobs)
        return {"ok": True, "id": jid}

    def run_now(self, job_id_or_name) -> dict:
        jobs = self._load_jobs()
        jid = self._find_id(jobs, job_id_or_name)
        if not jid:
            return {"ok": False, "error": "未找到任务"}
        result = self._execute_job(jobs[jid], persist=True)
        return {"ok": True, "id": jid, "result_preview": (result or "")[:500]}

    # ---- 执行 ----
    def _execute_job(self, job: dict, persist: bool = True) -> str:
        jid = job["id"]
        if self.agent is None:
            msg = "（调度器未挂载 agent，无法执行；请确认 Agent 已初始化并完成 set_agent）"
            logger.warning(f"cron 任务 {jid}：{msg}")
            return msg
        if persist:
            jobs = self._load_jobs()
            j = jobs.get(jid)
            if j:
                j["state"] = "running"
                self._save_jobs(jobs)

        try:
            from core.subagents import SubAgent
            prompt = job["prompt"]
            sys_extra = self._skill_context(job.get("skills") or [])
            full = (sys_extra + "\n\n" if sys_extra else "") + prompt
            sa = SubAgent(
                self.agent, role="定时任务执行器",
                focus="基于自包含任务独立完成工作，不依赖对话历史",
                max_iter=settings.SUBAGENT_MAX_ITER, depth=1, disabled_tools=set(),
            )
            result = sa.run(full)
            status = "ok"
        except Exception as e:
            logger.error(f"cron 任务 {jid} 执行异常：{e}")
            result = f"（执行出错：{e}）"
            status = "error"

        self._deliver(job, result)

        if persist:
            jobs = self._load_jobs()
            j = jobs.get(jid)
            if j:
                now = datetime.now()
                j["last_run_at"] = now.isoformat()
                j["last_status"] = status
                j["result"] = (result or "")[:2000]
                rep = j.get("repeat", {"times": None, "completed": 0})
                rep["completed"] = rep.get("completed", 0) + 1
                j["repeat"] = rep
                if j.get("single_shot"):
                    j["state"] = "completed"
                    j["next_run_at"] = None
                else:
                    j["state"] = "scheduled"
                    j["next_run_at"] = self._compute_next(j).isoformat()
                self._save_jobs(jobs)
        return result

    def _deliver(self, job: dict, result: str):
        if not result:
            return
        # [SILENT] 前缀：抑制投递（对齐 Hermes）
        if result.lstrip().startswith("[SILENT]"):
            return
        target = (job.get("deliver") or "ui").lower()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if target == "local":
            out_dir = self.data_dir / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{job['id']}_{ts}.txt").write_text(result, encoding="utf-8")
        # ui / origin / all → 应用收件箱
        self.inbox.append({
            "job_id": job["id"],
            "name": job.get("name"),
            "delivered_at": datetime.now().isoformat(),
            "target": target,
            "content": (result or "")[:4000],
        })
        self._save_inbox()

    # ---- 后台 tick ----
    def _acquire_file_lock(self) -> bool:
        try:
            self._ensure_dir()
            if os.name == "nt":
                import msvcrt
                self._flock = open(self._file_lock_path, "w")
                msvcrt.locking(self._flock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                self._flock = open(self._file_lock_path, "w")
                fcntl.flock(self._flock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except Exception:
            return False

    def _release_file_lock(self):
        try:
            if self._flock:
                self._flock.close()
                self._flock = None
        except Exception:
            pass

    def _tick(self) -> int:
        # 进程内锁 + 跨进程文件锁，防止重叠 tick（对齐 Hermes 锁机制）
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            if not self.enabled or self.agent is None:
                return 0
            if not self._acquire_file_lock():
                return 0
            try:
                jobs = self._load_jobs()
                now = datetime.now()
                due = []
                for j in jobs.values():
                    if not j.get("enabled"):
                        continue
                    if j.get("state") != "scheduled":
                        continue
                    nxt = j.get("next_run_at")
                    if not nxt:
                        continue
                    try:
                        nr = datetime.fromisoformat(nxt)
                    except Exception:
                        continue
                    if nr <= now:
                        due.append(j)
                for j in due:
                    self._execute_job(j, persist=True)
                return len(due)
            finally:
                self._release_file_lock()
        finally:
            self._lock.release()

    def start(self):
        if not self.enabled:
            logger.info("调度器已禁用（SCHEDULER_ENABLED=false）")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="scheduler")
        self._thread.start()
        logger.info(f"调度器已启动（tick={self.tick_seconds}s）")

    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.warning(f"scheduler tick 异常：{e}")
            self._stop.wait(self.tick_seconds)

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "tick_seconds": self.tick_seconds,
            "jobs": self.list_jobs(),
            "inbox_count": len(self.inbox),
        }


# 单例（模块加载时创建；agent 初始化时 set_agent + start）
SCHEDULER = Scheduler()


# ---------------- 暴露给 agent 的 cronjob 工具 ----------------

@registry.register(
    name="cronjob",
    description=(
        "管理定时任务（对齐 Hermes cronjob 工具，动作式）。action 取值：\n"
        "create(需 name+prompt+schedule, 可选 skills/deliver/repeat)、list、\n"
        "update(需 job_id 或 name, 可改任意字段)、pause、resume、run(立即执行)、remove。\n"
        "schedule 支持：30m/2h/1d(相对延迟)、every 2h(间隔)、0 9 * * *(cron)、"
        "2025-01-15T09:00:00(ISO)。deliver 支持：ui(应用收件箱,默认)/local(写文件)。"
    ),
)
def cronjob_tool(action: str, name: str = "", prompt: str = "", schedule: str = "",
                 skills: list = None, deliver: str = "ui", repeat: int = None,
                 job_id: str = "") -> str | dict:
    s = SCHEDULER
    a = (action or "").strip().lower()
    try:
        if a == "create":
            return s.create(name, prompt, schedule, skills=skills,
                            deliver=deliver, repeat=repeat)
        if a == "list":
            return s.list_jobs()
        if a == "update":
            ref = job_id or name
            if not ref:
                return {"ok": False, "error": "update 需要 job_id 或 name"}
            return s.update(ref, name=name or None, prompt=prompt or None,
                            schedule=schedule or None, skills=skills,
                            deliver=deliver or None, repeat=repeat)
        if a == "pause":
            return s.pause(job_id or name)
        if a == "resume":
            return s.resume(job_id or name)
        if a == "run":
            return s.run_now(job_id or name)
        if a == "remove":
            return s.remove(job_id or name)
        return f"未知动作：{action}（create/list/update/pause/resume/run/remove）"
    except Exception as e:
        return f"cronjob 执行异常：{e}"

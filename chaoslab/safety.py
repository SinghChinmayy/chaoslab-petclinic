"""Guardrails: what may be faulted, when to abort, and how to undo everything.

Four independent layers stop a fault:
  1. the watchdog inside the experiment (thresholds checked every second),
  2. the planned end of the experiment,
  3. the kill switch (`python3 -m chaoslab stop`, from any terminal),
  4. the dead-man's switch: a detached process that reverts the fault shortly
     after its planned end, even if the experiment process crashed or was killed.
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .docker import docker, health, targets
from .faults import Fault
from .settings import ROOT, RUNS_DIR, STATE_FILE


class SafetyError(RuntimeError):
    """An experiment was refused before anything was touched."""


@dataclass
class Guardrails:
    max_error_rate: float = 0.25   # abort when more than 25% of user requests fail...
    max_p95_ms: float = 2000       # ...or the slowest 5% take longer than 2 s...
    min_rps_ratio: float = 0.3     # ...or throughput drops below 30% of normal (users stuck)...
    breach_seconds: int = 3        # ...for this many seconds in a row
    max_duration_s: int = 300      # hard cap for any single fault
    expiry_grace_s: int = 30       # dead-man's switch fires this long after the planned end

    def breach(self, sample, baseline_rps: float) -> str | None:
        """Why a sample breaks the limits, or None when it is within them."""
        if sample.semantic_correctness in ("degraded", "data_lost", "unavailable"):
            return f"owner details correctness {sample.semantic_correctness}"
        if sample.stalled:
            return "no requests completing"
        if sample.error_rate is not None and sample.error_rate > self.max_error_rate:
            return f"error rate {sample.error_rate:.0%} > {self.max_error_rate:.0%}"
        if sample.p95_ms is not None and sample.p95_ms > self.max_p95_ms:
            return f"p95 {sample.p95_ms:.0f}ms > {self.max_p95_ms:.0f}ms"
        if sample.rps < self.min_rps_ratio * baseline_rps:
            return f"throughput {sample.rps:.1f} rps < {self.min_rps_ratio:.0%} of normal {baseline_rps:.1f}"
        return None


# Registry of active faults, shared by the experiment, the kill switch and the
# dead-man's switch. A file lock keeps concurrent updates consistent.

@contextmanager
def _state():
    RUNS_DIR.mkdir(exist_ok=True)
    with open(RUNS_DIR / ".state.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        yield state
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)


def active_faults() -> dict[str, dict]:
    with _state() as state:
        return dict(state)


def register(fault: Fault, expires_at: float) -> None:
    with _state() as state:
        state[fault.id] = {**fault.to_dict(), "pid": os.getpid(),
                           "injected_at": time.time(), "expires_at": expires_at}


def unregister(fault_id: str) -> None:
    with _state() as state:
        state.pop(fault_id, None)


def preflight(fault: Fault) -> None:
    """Refuse unsafe experiments before anything is touched."""
    if problems := fault.problems():
        raise SafetyError("; ".join(problems))
    allowed = targets()
    if fault.target not in allowed:
        raise SafetyError(f"{fault.target!r} is not an allowed target. "
                          f"Allowed: {', '.join(sorted(allowed))}")
    state = health(fault.target)
    if state not in ("running", "running/healthy"):
        raise SafetyError(f"{fault.target} is {state}; only healthy targets can be faulted")
    if active := active_faults():
        raise SafetyError(f"another fault is still active ({', '.join(active)}). "
                          "Run the kill switch first: python3 -m chaoslab stop")


def arm_dead_mans_switch(fault: Fault, at: float) -> None:
    """Start a detached process that reverts `fault` at time `at` if it is still active."""
    log = open(RUNS_DIR / "dead-mans-switch.log", "a")
    subprocess.Popen([sys.executable, "-m", "chaoslab", "_expire", fault.id, str(at)],
                     cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True)


def _target_gone(fault: Fault) -> bool:
    """True when the target container no longer exists, so there is nothing left to undo
    (e.g. the stack was removed, or Docker switched image stores, mid-experiment)."""
    return not docker("ps", "--all", "--quiet", "--filter", f"name=^{fault.target}$", check=False)


def expire(fault_id: str, at: float) -> None:
    """Body of the dead-man's switch process."""
    time.sleep(max(0.0, at - time.time()))
    entry = active_faults().get(fault_id)
    if entry is None:
        return  # already reverted normally
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    fault = Fault.from_dict(entry)
    try:
        fault.revert()
    except Exception as err:
        if not _target_gone(fault):  # stays registered, so the kill switch can retry
            print(f"{stamp} dead-man's switch FAILED to revert {fault_id}: {err}", flush=True)
            return
        print(f"{stamp} {fault.target} no longer exists; nothing to undo for {fault_id}", flush=True)
    unregister(fault_id)
    print(f"{stamp} dead-man's switch reverted {fault_id}", flush=True)


def kill_switch(say=print) -> bool:
    """Undo every active fault, then put every target back in its normal state.

    Returns False if something could not be reverted (it stays registered).
    """
    clean = True
    for fault_id, entry in active_faults().items():
        fault = Fault.from_dict(entry)
        try:
            fault.revert()
            unregister(fault_id)
            say(f"reverted: {fault.describe()}")
        except Exception as err:  # keep going: one failure must not block the rest
            if _target_gone(fault):
                unregister(fault_id)
                say(f"cleared {fault_id}: {fault.target} no longer exists, nothing to undo")
            else:
                clean = False
                say(f"FAILED to revert {fault_id}: {err}")
    for helper in docker("ps", "--quiet", "--filter", "label=chaoslab.fault=true").split():
        docker("stop", "--time", "5", helper, check=False)
        say(f"stopped stray fault helper {helper}")
    for name, state in targets().items():
        if state == "paused":
            docker("unpause", name)
            say(f"unpaused {name}")
        elif state != "running":
            docker("start", name)
            say(f"restarted {name} (was {state})")
    say("sandbox restored" if clean else "sandbox restored with errors, see above")
    return clean

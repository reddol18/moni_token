"""Windows Task Scheduler registration for `moni-token check` (issue #1).

The task runs `pythonw -m moni_token.cli check` from the current environment, only while the user is logged on.
Task Scheduler's defaults stop it on battery; moni_token's default is to keep running (spikes happen on battery too),
and `--no-battery` restores the restriction. Registering again replaces the task, so the option can be switched.
"""
import subprocess
import sys
from pathlib import Path

TASK_NAME = "moni_token check"


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def pythonw() -> Path:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def install_script(python: Path, interval_min: int = 2, battery: bool = True) -> str:
    settings = "New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew"
    if battery:
        settings += " -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
    return "; ".join([
        "$ErrorActionPreference = 'Stop'",
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(str(python))} -Argument '-m moni_token.cli check'",
        f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes {int(interval_min)})",
        f"$s = {settings}",
        f"$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive",
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null",
    ])


def uninstall_script() -> str:
    return (f"$t = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue; "
            f"if ($t) {{ Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Confirm:$false; 'removed' }} "
            f"else {{ 'not registered' }}")


def query_script() -> str:
    return (f"$t = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}; "
            "'{0}|{1}|{2}' -f $t.Settings.DisallowStartIfOnBatteries, $t.Settings.StopIfGoingOnBatteries, "
            "$t.Triggers[0].Repetition.Interval")


def run_ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=60)


def main_install(interval_min: int, battery: bool) -> int:
    if sys.platform != "win32":
        print("install-task supports Windows Task Scheduler only. Use cron/launchd to run "
              "`moni-token check` every 2 minutes.")
        return 2
    r = run_ps(install_script(pythonw(), interval_min, battery))
    if r.returncode:
        print(r.stderr.strip() or "registration failed")
        return 1
    q = run_ps(query_script()).stdout.strip().split("|")
    print(f"registered '{TASK_NAME}': every {interval_min} min, "
          f"{'runs on battery' if battery else 'not on battery'} "
          f"(DisallowStartIfOnBatteries={q[0] if q else '?'}, StopIfGoingOnBatteries={q[1] if len(q) > 1 else '?'})")
    return 0


def main_uninstall() -> int:
    if sys.platform != "win32":
        print("uninstall-task supports Windows Task Scheduler only.")
        return 2
    r = run_ps(uninstall_script())
    print(r.stdout.strip() or r.stderr.strip())
    return r.returncode

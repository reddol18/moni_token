"""Windows Task Scheduler registration for `moni-token check` (issue #1).

The task runs `pythonw -m moni_token.cli check` from the current environment, only while the user is logged on.
Task Scheduler's defaults stop it on battery; moni_token's default is to keep running (spikes happen on battery too),
and `--no-battery` restores the restriction. Registering again replaces the task, so the option can be switched.
"""
import subprocess
import sys
from pathlib import Path

TASK_NAME = "moni_token check"
MAP_TASK_NAME = "moni_token map"


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


def map_script(python: Path, port: int = 4990, battery: bool = True) -> str:
    """The agent map server: starts at logon, keeps running (no time limit), restarted if it crashes."""
    settings = ("New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew "
                "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)")
    if battery:
        settings += " -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
    return "; ".join([
        "$ErrorActionPreference = 'Stop'",
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(str(python))} -Argument '-m moni_token.cli map --port {int(port)}'",
        "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME",
        f"$s = {settings}",
        "$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive",
        f"Register-ScheduledTask -TaskName {_ps_quote(MAP_TASK_NAME)} -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null",
        f"Stop-ScheduledTask -TaskName {_ps_quote(MAP_TASK_NAME)} -ErrorAction SilentlyContinue",
        f"Start-ScheduledTask -TaskName {_ps_quote(MAP_TASK_NAME)}",   # run now too, not only at next logon
    ])


def uninstall_script() -> str:
    return "; ".join(
        f"$t = Get-ScheduledTask -TaskName {_ps_quote(n)} -ErrorAction SilentlyContinue; "
        f"if ($t) {{ Stop-ScheduledTask -TaskName {_ps_quote(n)} -ErrorAction SilentlyContinue; "
        f"Unregister-ScheduledTask -TaskName {_ps_quote(n)} -Confirm:$false; 'removed {n}' }} "
        f"else {{ 'not registered: {n}' }}" for n in (TASK_NAME, MAP_TASK_NAME))


def query_script() -> str:
    return (f"$t = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}; "
            "'{0}|{1}|{2}' -f $t.Settings.DisallowStartIfOnBatteries, $t.Settings.StopIfGoingOnBatteries, "
            "$t.Triggers[0].Repetition.Interval")


def run_ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=60)


def main_install(interval_min: int, battery: bool, map_port: int | None = None) -> int:
    if sys.platform != "win32":
        print("install-task supports Windows Task Scheduler only. Use cron/launchd to run "
              "`moni-token check` every 2 minutes.")
        return 2
    r = run_ps(install_script(pythonw(), interval_min, battery))
    if r.returncode:
        print(r.stderr.strip() or "registration failed")
        return 1
    if map_port:
        m = run_ps(map_script(pythonw(), map_port, battery))
        if m.returncode:
            print(m.stderr.strip() or "map registration failed")
            return 1
        print(f"registered '{MAP_TASK_NAME}': starts at logon, http://127.0.0.1:{map_port}/ (started now)")
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

#!/usr/bin/env python3
"""One-command local setup for the EDGAR copy-trader.

    python3 setup_local.py                # macOS/Linux:  venv + dependencies + .env + offline tests
    py setup_local.py                     # Windows:      same
    ... --schedule                        # also install a weekday schedule (cron, or Task Scheduler)

Safe by default: it never turns on trading. The generated .env keeps PAPER=true and DRY_RUN=true,
and you still have to paste in your own keys. Re-running it is harmless.
"""
import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
MARKER = "# edgar-copytrader"
TASK_NAME = "EdgarCopyTrader"
IS_WINDOWS = os.name == "nt"


def say(msg):
    print(f"\n==> {msg}")


def run(cmd, **kw):
    print("   $", " ".join(shlex.quote(str(c)) for c in cmd))
    return subprocess.run([str(c) for c in cmd], cwd=HERE, **kw)


def venv_python():
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def local_run_time(py):
    """Local clock time of 9:45 a.m. New York time, i.e. 15 minutes after the US market opens.

    Computed by the venv's Python so Windows works too (it needs the tzdata package installed there).
    """
    snippet = (
        "from datetime import datetime\n"
        "from zoneinfo import ZoneInfo\n"
        "t = datetime.now(ZoneInfo('America/New_York')).replace(hour=9, minute=45, second=0, microsecond=0)\n"
        "t = t.astimezone()\n"
        "print(t.hour, t.minute)"
    )
    try:
        out = subprocess.run([str(py), "-c", snippet], capture_output=True, text=True, timeout=60)
        hour, minute = out.stdout.split()
        return int(hour), int(minute)
    except Exception:
        print("   Could not look up the Eastern time zone; assuming this computer runs on Eastern time.")
        return 9, 45


def cron_line(py, hour, minute):
    return (f"{minute} {hour} * * 1-5 cd {shlex.quote(str(HERE))} && "
            f"{shlex.quote(str(py))} {shlex.quote(str(HERE / 'agent.py'))} >> cron.log 2>&1 {MARKER}")


def install_cron(line):
    if shutil.which("crontab") is None:
        return False
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = current.stdout if current.returncode == 0 else ""
    kept = [ln for ln in existing.splitlines() if MARKER not in ln]
    new = "\n".join(kept + [line]) + "\n"
    return subprocess.run(["crontab", "-"], input=new, text=True).returncode == 0


def install_windows_task(hour, minute):
    """Create run_agent.bat and a weekday Task Scheduler job that runs it. Re-running replaces the job."""
    bat = HERE / "run_agent.bat"
    bat.write_text(
        "@echo off\r\n"
        'cd /d "%~dp0"\r\n'
        '".venv\\Scripts\\python.exe" agent.py >> cron.log 2>&1\r\n',
        encoding="ascii",
    )
    cmd = ["schtasks", "/Create", "/F", "/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI",
           "/ST", f"{hour:02d}:{minute:02d}", "/TN", TASK_NAME, "/TR", f'"{bat}"']
    return subprocess.run(cmd).returncode == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schedule", action="store_true", help="install a weekday schedule (cron / Task Scheduler)")
    ap.add_argument("--skip-install", action="store_true", help="do not pip install requirements")
    ap.add_argument("--skip-tests", action="store_true", help="do not run the offline tests")
    args = ap.parse_args()

    if sys.version_info < (3, 9):
        raise SystemExit(f"Python 3.9+ is required (this is {platform.python_version()}).")

    say("Creating virtual environment (.venv)")
    if not venv_python().exists():
        if run([sys.executable, "-m", "venv", VENV]).returncode != 0:
            raise SystemExit("Could not create a virtual environment. On Debian/Ubuntu: sudo apt install python3-venv")
    py = venv_python()

    if args.skip_install:
        say("Skipping dependency install (--skip-install)")
    else:
        say("Installing dependencies")
        if run([py, "-m", "pip", "install", "--quiet", "-r", "requirements.txt"]).returncode != 0:
            raise SystemExit("pip install failed. Check your internet connection and try again.")

    say("Preparing .env")
    env, example = HERE / ".env", HERE / ".env.example"
    if env.exists():
        print("   .env already exists; leaving it alone.")
    else:
        shutil.copy(example, env)
        if not IS_WINDOWS:
            env.chmod(0o600)   # keys inside: readable by you only
        print("   Created .env (PAPER=true, DRY_RUN=true). Add your keys before the first run.")

    if args.skip_tests:
        say("Skipping tests (--skip-tests)")
    else:
        say("Running offline tests")
        if run([py, "-m", "unittest"]).returncode != 0:
            raise SystemExit("Tests failed, so nothing was scheduled. Fix that first.")

    hour, minute = local_run_time(py)
    if args.schedule:
        say("Installing the weekday schedule")
        if IS_WINDOWS:
            if install_windows_task(hour, minute):
                print(f"   Installed Task Scheduler job '{TASK_NAME}': weekdays at {hour:02d}:{minute:02d} "
                      "local time (9:45 a.m. Eastern).")
                print(f"   Remove it any time with: schtasks /Delete /TN {TASK_NAME} /F")
                print("   It runs only while you're signed in and the PC is awake.")
            else:
                print("   Could not create the task. Open Task Scheduler and create a weekday task that runs\n"
                      f"   {HERE / 'run_agent.bat'}")
        else:
            line = cron_line(py, hour, minute)
            if install_cron(line):
                print(f"   Installed: weekdays at {hour:02d}:{minute:02d} local time (9:45 a.m. Eastern).")
                print("   Remove it any time with: crontab -e   (delete the line ending in '# edgar-copytrader')")
            else:
                print("   Could not install the cron job. Add this line by hand with 'crontab -e':\n\n   " + line)

    say("Done. Next steps")
    rel_py = r".venv\Scripts\python" if IS_WINDOWS else ".venv/bin/python"
    print(f"""   1. Edit .env: set SEC_USER_AGENT (your real name and email), ALPACA_KEY_ID, ALPACA_SECRET_KEY.
      Use a PAPER Alpaca account's keys first.
   2. Download recent filings (slow the first time, then it only fetches new ones):
        {rel_py} agent.py --collect-only
   3. See what it would do without placing anything:
        {rel_py} agent.py --ignore-market-hours""")
    if not args.schedule:
        print("   4. Optional daily schedule: re-run this script with --schedule.")
    print("   Create a file named STOP in this folder to halt all trading at any time.")


if __name__ == "__main__":
    main()

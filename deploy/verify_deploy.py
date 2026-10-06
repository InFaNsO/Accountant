"""
SSH into the server and verify the deployment is working.
Run: python deploy/verify_deploy.py
"""
import paramiko

HOST = "143.244.128.26"
USER = "root"
PASSWORD = "gr8is@myMom"

def run(client, cmd):
    _, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    return out, err

def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    print(f"Connected to {HOST}\n")

    checks = [
        ("Service status",        "systemctl is-active ledger"),
        ("New routes file",       "test -f /home/ledger/app/app/routes/mobile.py && echo EXISTS || echo MISSING"),
        ("Notification service",  "test -f /home/ledger/app/app/services/notification_service.py && echo EXISTS || echo MISSING"),
        ("Scheduler",             "test -f /home/ledger/app/app/services/scheduler.py && echo EXISTS || echo MISSING"),
        ("firebase-admin pkg",    "source /home/ledger/app/venv/bin/activate && pip show firebase-admin 2>/dev/null | grep Version || echo NOT INSTALLED"),
        ("APScheduler pkg",       "source /home/ledger/app/venv/bin/activate && pip show APScheduler 2>/dev/null | grep Version || echo NOT INSTALLED"),
        ("device_tokens table",   "source /home/ledger/app/venv/bin/activate && python3 -c \"import sqlite3; c=sqlite3.connect('/home/ledger/app/data/ledger.db'); print('EXISTS' if c.execute(\\\"SELECT name FROM sqlite_master WHERE name='device_tokens'\\\").fetchone() else 'MISSING'); c.close()\""),
        ("notification_log table","source /home/ledger/app/venv/bin/activate && python3 -c \"import sqlite3; c=sqlite3.connect('/home/ledger/app/data/ledger.db'); print('EXISTS' if c.execute(\\\"SELECT name FROM sqlite_master WHERE name='notification_log'\\\").fetchone() else 'MISSING'); c.close()\""),
        ("Firebase warning",      "journalctl -u ledger -n 10 --no-pager 2>/dev/null | grep -i firebase | tail -1"),
        ("Scheduler started",     "journalctl -u ledger -n 20 --no-pager 2>/dev/null | grep -i scheduler | tail -1"),
    ]

    all_ok = True
    for label, cmd in checks:
        out, err = run(client, cmd)
        result = out or err or "(empty)"
        status = "OK" if result not in ("NOT INSTALLED", "MISSING", "inactive", "failed") else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"[{status}] {label}: {result}")

    client.close()
    print("\nAll checks passed." if all_ok else "\nSome checks failed — see above.")

if __name__ == "__main__":
    main()

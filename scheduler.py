#!/usr/bin/env python3
"""Small persistent weekday scheduler for the standalone Docker service."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from refresh_new_actors import CST, UTC, atomic_save, parse_date

STATUS = Path('/data/scheduler.json')
CONFIG = Path('/config/config.json')


def due(now, last_date):
    local = now.astimezone(CST)
    return (local.weekday() < 5 and local.hour == 9 and local.minute == 40
            and local.date().isoformat() != last_date)


def next_run(now, last_date):
    local = now.astimezone(CST)
    for days in range(8):
        day = local.date() + dt.timedelta(days=days)
        candidate = dt.datetime.combine(day, dt.time(9, 40), tzinfo=CST)
        if day.weekday() < 5 and day.isoformat() != last_date:
            if candidate >= local or (days == 0 and due(now, last_date)):
                return candidate.isoformat()
    raise ValueError('no_next_run')


def healthy(status, now):
    try:
        if status.get('running_since'):
            return 0 <= (now - parse_date(status['running_since'])).total_seconds() < 600
        return (parse_date(status['next_run']) - now).total_seconds() >= -120
    except (KeyError, ValueError):
        return False


def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields), ensure_ascii=False), flush=True)


def daemon():
    import fcntl
    os.umask(0o077)
    config = json.loads(CONFIG.read_text())
    if not Path(config['state_file']).is_file():
        raise ValueError('worker_state_missing')
    with open('/data/scheduler.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = json.loads(STATUS.read_text()) if STATUS.exists() else {'version': 1}
        if status.get('version') != 1:
            raise ValueError('invalid_scheduler_state')
        # A previous interrupted run must not block the healthcheck forever or
        # be retried immediately after a restart. Its reserved date is retained.
        status.pop('running_since', None)
        now = dt.datetime.now(UTC)
        status['next_run'] = next_run(now, status.get('last_attempt_date'))
        atomic_save(STATUS, status)
        emit('scheduler_started', schedule='周一至周五 09:40 Asia/Shanghai',
             next_run=status['next_run'])
        while True:
            now = dt.datetime.now(UTC)
            if due(now, status.get('last_attempt_date')):
                status['last_attempt_date'] = now.astimezone(CST).date().isoformat()
                status['running_since'] = now.isoformat()
                status['next_run'] = next_run(now, status['last_attempt_date'])
                atomic_save(STATUS, status)  # Reserve date before spawning.
                emit('job_started', at=now.astimezone(CST).isoformat())
                try:
                    result = subprocess.run(
                        [sys.executable, '/app/refresh_new_actors.py', '--config', str(CONFIG)],
                        timeout=540, capture_output=True, text=True)
                    status['last_exit_code'] = result.returncode
                    if result.stdout:
                        emit('job_output', text=result.stdout.strip()[-4000:])
                    if result.stderr:
                        # Worker errors are normally safe JSON on stdout. Do
                        # not publish arbitrary stderr, which may expose data.
                        emit('job_stderr_present')
                except subprocess.TimeoutExpired:
                    status['last_exit_code'] = 124
                    emit('job_timeout')
                except OSError:
                    status['last_exit_code'] = 125
                    emit('worker_start_failed')
                status.pop('running_since', None)
                status['last_finished_at'] = dt.datetime.now(UTC).isoformat()
                status['next_run'] = next_run(dt.datetime.now(UTC), status['last_attempt_date'])
                atomic_save(STATUS, status)
                emit('job_finished', exit_code=status['last_exit_code'], next_run=status['next_run'])
            else:
                target = next_run(now, status.get('last_attempt_date'))
                if status.get('next_run') != target:
                    status['next_run'] = target
                    atomic_save(STATUS, status)
            time.sleep(20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--healthcheck', action='store_true')
    args = parser.parse_args()
    if args.healthcheck:
        try:
            ok = healthy(json.loads(STATUS.read_text()), dt.datetime.now(UTC))
        except Exception:
            ok = False
        return 0 if ok else 1
    daemon()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit('scheduler_failed', reason=type(exc).__name__)
        raise SystemExit(1)

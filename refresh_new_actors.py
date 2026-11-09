#!/usr/bin/env python3
"""Bounded new-Person metadata refresh, independent of Emby plugins. Stdlib only."""
import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc
try:
    CST = ZoneInfo('Asia/Shanghai')
except ZoneInfoNotFoundError:  # Windows test hosts may not ship IANA tzdata.
    CST = dt.timezone(dt.timedelta(hours=8), 'Asia/Shanghai')


class ApiError(Exception):
    """Safe error code only; never include a URL, header, or response body."""


def parse_date(value):
    try:
        value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError()
        return value.astimezone(UTC)
    except (ValueError, AttributeError, TypeError):
        raise ApiError('invalid_date') from None


def stamp(value):
    return value.astimezone(UTC).isoformat()


def within_window(now):
    local = now.astimezone(CST)
    return local.weekday() < 5 and (9 <= local.hour < 12 or 15 <= local.hour < 17)


def new_state(server_id, baseline):
    return dict(version=1, server_id=server_id, baseline=stamp(baseline),
                watermark=stamp(baseline), records={})


def discover(api, since, now, page_size=100, can_continue=None):
    # Grow a single prefix instead of combining offset pages: timestamps and
    # even sort names can tie, and new rows can shift offsets between requests.
    limit = page_size
    while limit <= 10000:
        if can_continue and not can_continue():
            raise ApiError('discovery_budget_exhausted')
        page = api.page(0, limit)
        if can_continue and not can_continue():
            raise ApiError('discovery_budget_exhausted')
        items = page.get('Items')
        if not isinstance(items, list):
            raise ApiError('invalid_page')
        found = {}
        previous = None
        older = False
        for item in items:
            created = parse_date(item.get('DateCreated'))
            if previous is not None and created > previous:
                raise ApiError('unstable_date_order')
            previous = created
            if created < since:
                older = True
            elif created <= now:
                if item.get('Type') != 'Person' or not item.get('Id'):
                    raise ApiError('invalid_person')
                if str(item['Id']) in found:
                    raise ApiError('duplicate_item_in_page')
                found[str(item['Id'])] = item
        if older or not items or len(items) < limit:
            return list(found.values())
        if limit == 10000:
            break
        limit = min(limit * 2, 10000)
    raise ApiError('discovery_limit_exceeded')


def eligible(item, baseline, now):
    if not item or item.get('Type') != 'Person':
        return 'gone_or_wrong_type'
    created = parse_date(item.get('DateCreated'))
    if not baseline <= created <= now:
        return 'outside_baseline'
    if item.get('IsLocked') or item.get('LockData') or item.get('LockedFields'):
        return 'locked'
    tmdb = next((str(v) for k, v in item.get('ProviderIds', {}).items() if k.lower() == 'tmdb'), '')
    if not re.fullmatch(r'[1-9][0-9]*', tmdb):
        return 'missing_id'
    return None


def run(api, state, now, dry_run=False, max_refresh=50, persist=None,
        pause=None, can_continue=None):
    state = copy.deepcopy(state)
    if state.get('version') != 1 or not isinstance(state.get('records'), dict):
        raise ApiError('invalid_state')
    if api.info().get('Id') != state['server_id']:
        raise ApiError('server_identity_changed')
    baseline = parse_date(state['baseline'])
    watermark = parse_date(state['watermark'])
    if watermark > now or baseline > watermark:
        raise ApiError('invalid_checkpoint')
    since = max(baseline, watermark - dt.timedelta(days=2))
    discovered = discover(api, since, now, can_continue=can_continue)
    summary = dict(discovered=len(discovered), eligible=0, submitted=0, missing_id=0,
                   locked=0, failed=0, deferred=0, dry_run=dry_run)
    records = state['records']
    for item in discovered:
        pid = str(item['Id'])
        if pid not in records:
            records[pid] = dict(created=item['DateCreated'], first_seen=stamp(now),
                                status='pending', attempts=0)
    state['watermark'] = stamp(now)  # Discovery snapshot; not end-of-run time.

    def checkpoint():
        if persist and not dry_run:
            persist(state)

    checkpoint()  # Pending queue is durable before any write to Emby.
    attempted = 0
    for pid, record in list(records.items()):
        if record['status'] != 'pending':
            continue
        if record['attempts'] >= 3:
            record['status'] = 'review_failed'
            checkpoint()
            continue
        if attempted >= max_refresh or (can_continue and not can_continue()):
            summary['deferred'] += 1
            continue
        attempted_post = False
        try:
            item = api.person(pid)  # Revalidate immediately before refresh.
            reason = eligible(item, baseline, now)
            if reason == 'missing_id':
                summary['missing_id'] += 1
                record['last_reason'] = reason
                if now - parse_date(record['first_seen']) >= dt.timedelta(days=7):
                    record['status'] = 'review_missing_id'
            elif reason:
                record['status'] = 'skipped_' + reason
                if reason == 'locked':
                    summary['locked'] += 1
            else:
                summary['eligible'] += 1
                if not dry_run:
                    if can_continue and not can_continue():
                        summary['deferred'] += 1
                        continue
                    attempted += 1
                    record['attempts'] += 1
                    checkpoint()  # Reserve attempt durably before submitting it.
                    attempted_post = True
                    api.refresh(pid)
                    record['status'] = 'submitted'
                    record['submitted_at'] = stamp(now)
                    # HTTP acceptance is not proof all providers found metadata.
                    summary['submitted'] += 1
                    checkpoint()
        except ApiError as exc:
            summary['failed'] += 1
            record['last_reason'] = str(exc)
            if record['attempts'] >= 3:
                record['status'] = 'review_failed'
        finally:
            if attempted_post and pause:
                pause()  # Rejected requests must also be rate limited.
        checkpoint()
    summary['pending'] = sum(x['status'] == 'pending' for x in records.values())
    summary['review'] = sum(x['status'].startswith('review_') for x in records.values())
    state['last_run'] = dict(at=stamp(now), **summary)
    checkpoint()
    return state, summary


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the API credential to a redirected origin.


class Api:
    def __init__(self, base_url, token):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.query or parsed.fragment:
            raise ApiError('local_endpoint_required')
        self.base = base_url.rstrip('/')
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, params=None, method='GET', body=None):
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'X-Emby-Token': self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=20) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ApiError('response_too_large')
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise ApiError('http_' + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiError('network_error') from None
        except (ValueError, UnicodeError):
            raise ApiError('invalid_json') from None

    def info(self):
        return self.request('/System/Info')

    def page(self, start, limit):
        return self.request('/Items', dict(IncludeItemTypes='Person', Recursive='true',
                            SortBy='DateCreated,SortName', SortOrder='Descending,Ascending', StartIndex=start,
                            Limit=limit, Fields='DateCreated,ProviderIds,LockedFields'))

    def person(self, pid):
        if not re.fullmatch(r'[0-9]+', pid):
            raise ApiError('invalid_item_id')
        items = self.request('/Items', dict(Ids=pid, IncludeItemTypes='Person', Recursive='true',
                            Fields='DateCreated,ProviderIds,LockedFields')).get('Items', [])
        if len(items) > 1 or (items and str(items[0].get('Id')) != pid):
            raise ApiError('unexpected_item')
        return items[0] if items else None

    def refresh(self, pid):
        if not re.fullmatch(r'[0-9]+', pid):
            raise ApiError('invalid_item_id')
        return self.request('/Items/' + pid + '/Refresh',
                            dict(Recursive='false', MetadataRefreshMode='FullRefresh',
                                 ImageRefreshMode='Default', ReplaceAllMetadata='true',
                                 ReplaceAllImages='false'), method='POST', body={})


def load_token(config):
    # Existing API app token, never a user's login token. SQLite is read-only.
    try:
        with sqlite3.connect(Path(config['auth_db']).as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            row = db.execute("SELECT AccessToken FROM Tokens_2 WHERE IsActive=1 AND AppName=? "
                             "AND (UserId IS NULL OR UserId='') ORDER BY Id DESC LIMIT 1",
                             (config['api_app_name'],)).fetchone()
        if not row or not row[0]:
            raise ApiError('api_credential_unavailable')
        return row[0]
    except sqlite3.Error:
        raise ApiError('credential_database_unavailable') from None


def atomic_save(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp, 0o600)  # NAS inherited ACLs can otherwise add executable bits.
    os.replace(tmp, path)
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--initialize', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    import fcntl  # Host Linux only; pure worker functions remain portable for tests.
    config = json.loads(Path(args.config).read_text())
    state_path = Path(config['state_file'])
    with open(state_path.with_suffix('.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'already_running'})); return
        now = dt.datetime.now(UTC)
        api = Api(config['base_url'], load_token(config))
        if args.initialize:
            if state_path.exists():
                raise ApiError('state_already_exists')
            if api.info().get('Id') != config['server_id']:
                raise ApiError('server_identity_changed')
            atomic_save(state_path, new_state(config['server_id'], parse_date(config['baseline'])))
            print(json.dumps({'status': 'initialized', 'baseline': config['baseline']})); return
        if not args.dry_run and not within_window(now):
            print(json.dumps({'status': 'outside_workday_daytime'})); return
        if not state_path.exists():
            raise ApiError('state_missing_initialize_required')
        state = json.loads(state_path.read_text())
        if state['server_id'] != config['server_id'] or state['baseline'] != stamp(parse_date(config['baseline'])):
            raise ApiError('state_config_mismatch')
        deadline = time.monotonic() + 450
        _, summary = run(api, state, now, dry_run=args.dry_run,
                         max_refresh=config.get('max_refresh', 50),
                         persist=lambda value: atomic_save(state_path, value),
                         pause=lambda: time.sleep(2),
                         can_continue=lambda: time.monotonic() < deadline and
                         (args.dry_run or within_window(dt.datetime.now(UTC))))
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except ApiError as exc:
        print(json.dumps({'status': 'failed', 'reason': str(exc)}))
        raise SystemExit(1)
    except Exception as exc:
        # No traceback: exception text can include auth headers or filesystem data.
        print(json.dumps({'status': 'failed', 'reason': type(exc).__name__}))
        raise SystemExit(1)

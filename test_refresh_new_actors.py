import copy
import datetime as dt
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
import urllib.parse

from refresh_new_actors import Api, ApiError, discover, new_state, run, within_window

UTC = dt.timezone.utc
BASE = dt.datetime(2026, 11, 7, 16, tzinfo=UTC)
NOW = dt.datetime(2026, 11, 9, 1, 40, tzinfo=UTC)


def person(pid, created=None, tmdb='42', **extra):
    return dict(Id=pid, Type='Person', DateCreated=(created or NOW).isoformat(),
                ProviderIds=({'Tmdb': tmdb} if tmdb else {}), **extra)


class FakeApi:
    def __init__(self, people, fail=()):
        self.people = sorted(people, key=lambda x: x['DateCreated'], reverse=True)
        self.refreshed = []
        self.fail = fail
        self.server_id = 'server'

    def info(self):
        return {'Id': self.server_id}

    def page(self, start, limit):
        return {'Items': self.people[start:start + limit], 'TotalRecordCount': len(self.people)}

    def person(self, pid):
        return next((p for p in self.people if p['Id'] == pid), None)

    def refresh(self, pid):
        if pid in self.fail:
            raise ApiError('http_503')
        self.refreshed.append(pid)


class IncrementalTests(unittest.TestCase):
    def state(self):
        return new_state('server', BASE)

    def test_only_new_people_with_verified_ids_are_refreshed(self):
        api = FakeApi([person('old', BASE - dt.timedelta(seconds=1)),
                       person('new'), person('unknown', tmdb=None)])
        state, summary = run(api, self.state(), NOW)
        self.assertEqual(api.refreshed, ['new'])
        self.assertEqual(state['records']['unknown']['status'], 'pending')
        self.assertEqual(summary['missing_id'], 1)

    def test_pagination_includes_ties_at_boundary(self):
        api = FakeApi([person(str(i), BASE) for i in range(5)] +
                      [person('old', BASE - dt.timedelta(seconds=1))])
        self.assertEqual(len(discover(api, BASE, NOW, page_size=2)), 5)

    def test_completed_people_are_not_refreshed_again(self):
        api = FakeApi([person('new')])
        state, _ = run(api, self.state(), NOW)
        run(api, state, NOW + dt.timedelta(days=1))
        self.assertEqual(api.refreshed, ['new'])

    def test_failed_requests_survive_checkpoint_and_retry(self):
        api = FakeApi([person('new')], fail=['new'])
        state, _ = run(api, self.state(), NOW)
        self.assertEqual(state['records']['new']['status'], 'pending')
        api.fail = []
        state, _ = run(api, state, NOW + dt.timedelta(days=3))
        self.assertEqual(api.refreshed, ['new'])
        self.assertEqual(state['records']['new']['status'], 'submitted')

    def test_missing_id_is_rechecked_without_name_matching(self):
        api = FakeApi([person('new', tmdb=None)])
        state, _ = run(api, self.state(), NOW)
        api.people[0]['ProviderIds'] = {'Tmdb': '42'}
        run(api, state, NOW + dt.timedelta(days=1))
        self.assertEqual(api.refreshed, ['new'])

    def test_wrong_server_fails_closed(self):
        api = FakeApi([person('new')]); api.server_id = 'other'
        with self.assertRaises(ApiError):
            run(api, self.state(), NOW)
        self.assertEqual(api.refreshed, [])

    def test_dry_run_does_not_change_state_or_refresh(self):
        api = FakeApi([person('new')]); state = self.state(); original = copy.deepcopy(state)
        _, summary = run(api, state, NOW, dry_run=True)
        self.assertEqual(state, original)
        self.assertEqual(api.refreshed, [])
        self.assertEqual(summary['eligible'], 1)

    def test_refresh_revalidates_type_date_and_locks(self):
        class ChangedApi(FakeApi):
            def person(self, pid):
                return person(pid, BASE - dt.timedelta(days=1))
        api = ChangedApi([person('new')])
        run(api, self.state(), NOW)
        self.assertEqual(api.refreshed, [])
        api = FakeApi([person('locked', IsLocked=True), person('fieldlock', LockedFields=['Name'])])
        run(api, self.state(), NOW)
        self.assertEqual(api.refreshed, [])

    def test_batch_limit_keeps_remainder_pending(self):
        api = FakeApi([person(str(i)) for i in range(5)])
        state, _ = run(api, self.state(), NOW, max_refresh=2)
        self.assertEqual(len(api.refreshed), 2)
        self.assertEqual(sum(x['status'] == 'pending' for x in state['records'].values()), 3)

    def test_quiet_hours_and_weekends(self):
        self.assertTrue(within_window(NOW))
        self.assertFalse(within_window(NOW.replace(hour=16)))  # midnight CST
        self.assertFalse(within_window(NOW.replace(hour=5)))   # 13:40 CST
        self.assertFalse(within_window(NOW - dt.timedelta(days=1)))

    def test_invalid_date_fails_discovery_instead_of_scanning_history(self):
        api = FakeApi([person('new')]); api.people[0]['DateCreated'] = 'invalid'
        with self.assertRaises(ApiError):
            run(api, self.state(), NOW)
        self.assertEqual(api.refreshed, [])

    def test_queue_is_saved_before_refresh_and_after_acceptance(self):
        snapshots = []
        class CheckingApi(FakeApi):
            def refresh(self, pid):
                self_check.assertEqual(snapshots[-1]['records'][pid]['status'], 'pending')
                super().refresh(pid)
        self_check = self
        api = CheckingApi([person('new')])
        run(api, self.state(), NOW, persist=lambda x: snapshots.append(copy.deepcopy(x)))
        self.assertEqual(snapshots[-1]['records']['new']['status'], 'submitted')

    def test_three_rejected_posts_stop_automatic_retry(self):
        api = FakeApi([person('new')], fail=['new']); state = self.state()
        for i in range(4):
            state, _ = run(api, state, NOW + dt.timedelta(days=i))
        self.assertEqual(state['records']['new']['attempts'], 3)
        self.assertEqual(state['records']['new']['status'], 'review_failed')

    def test_restart_after_third_reserved_attempt_does_not_submit_fourth(self):
        api = FakeApi([person('new')]); state = self.state()
        state['records']['new'] = dict(created=NOW.isoformat(), first_seen=NOW.isoformat(),
                                      status='pending', attempts=3)
        state, _ = run(api, state, NOW)
        self.assertEqual(api.refreshed, [])
        self.assertEqual(state['records']['new']['status'], 'review_failed')

    def test_missing_ids_stop_after_seven_days(self):
        api = FakeApi([person('new', tmdb=None)])
        state, _ = run(api, self.state(), NOW)
        state, _ = run(api, state, NOW + dt.timedelta(days=8))
        self.assertEqual(state['records']['new']['status'], 'review_missing_id')

    def test_stopping_mid_batch_keeps_unsubmitted_items_pending(self):
        api = FakeApi([person('new')])
        calls = iter([True, True, False])
        state, summary = run(api, self.state(), NOW, can_continue=lambda: next(calls, False))
        self.assertEqual(api.refreshed, [])
        self.assertEqual(state['records']['new']['status'], 'pending')
        self.assertEqual(summary['deferred'], 1)

    def test_discovery_budget_does_not_advance_checkpoint(self):
        api = FakeApi([person('new')]); state = self.state(); saved = []
        with self.assertRaises(ApiError):
            run(api, state, NOW, can_continue=lambda: False, persist=saved.append)
        self.assertEqual(saved, [])
        self.assertEqual(state['watermark'], self.state()['watermark'])

    def test_quiet_window_is_checked_again_after_person_lookup(self):
        allowed = [True]
        class DelayedApi(FakeApi):
            def person(self, pid):
                allowed[0] = False
                return super().person(pid)
        api = DelayedApi([person('new')])
        state, _ = run(api, self.state(), NOW, can_continue=lambda: allowed[0])
        self.assertEqual(api.refreshed, [])
        self.assertEqual(state['records']['new']['status'], 'pending')

    def test_rejected_posts_are_also_throttled(self):
        api = FakeApi([person('one'), person('two')], fail=['one', 'two']); pauses = []
        run(api, self.state(), NOW, pause=lambda: pauses.append(True))
        self.assertEqual(len(pauses), 2)

    def test_unstable_tied_offset_pages_cannot_hide_people(self):
        class UnstableApi(FakeApi):
            def page(self, start, limit):
                if start > 0:
                    # Tie reordering would duplicate the first person and omit another.
                    return {'Items': self.people[:limit], 'TotalRecordCount': len(self.people)}
                return super().page(start, limit)
        api = UnstableApi([person(str(i), BASE) for i in range(5)])
        result = discover(api, BASE, NOW, page_size=2)
        self.assertEqual({p['Id'] for p in result}, {str(i) for i in range(5)})


class HttpContractTests(unittest.TestCase):
    def test_refresh_is_one_item_nonrecursive_and_credential_stays_in_header(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get('X-Emby-Token'),
                                 json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(204); self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            api = Api('http://127.0.0.1:' + str(server.server_port) + '/emby', 'fake-secret')
            api.refresh('123')
        finally:
            server.shutdown(); server.server_close(); thread.join()
        path, token, body = received[0]
        parsed = urllib.parse.urlsplit(path); query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.path, '/emby/Items/123/Refresh')
        self.assertEqual(query['Recursive'], ['false'])
        self.assertEqual(query['ReplaceAllImages'], ['false'])
        self.assertEqual(query['MetadataRefreshMode'], ['FullRefresh'])
        self.assertNotIn('fake-secret', path)
        self.assertEqual(token, 'fake-secret'); self.assertEqual(body, {})


if __name__ == '__main__':
    unittest.main()

import datetime as dt
import tempfile
from pathlib import Path
import unittest
from initialize import initialize


class SetupTests(unittest.TestCase):
    def test_custom_loopback_port_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg,_=initialize(Path(directory),'app',dt.datetime.now(dt.timezone.utc),
                             lambda c: {'Id':'server'},base_url='http://127.0.0.1:18096/emby')
            self.assertEqual(cfg['base_url'],'http://127.0.0.1:18096/emby')

    def test_new_install_starts_now_and_discovers_server(self):
        with tempfile.TemporaryDirectory() as directory:
            now = dt.datetime(2026, 11, 9, 12, tzinfo=dt.timezone.utc)
            cfg, state = initialize(Path(directory), 'existing-app', now, lambda c: {'Id': 'server-id'})
            self.assertEqual(cfg['server_id'], 'server-id')
            self.assertEqual(state['baseline'], now.isoformat())
            self.assertTrue((Path(directory)/'data/state.json').is_file())
            self.assertTrue((Path(directory)/'config.docker.json').is_file())

    def test_existing_progress_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir();(root/'data/state.json').write_text('keep')
            with self.assertRaises(FileExistsError):
                initialize(root,'app',dt.datetime.now(dt.timezone.utc),lambda c: {'Id':'server'})
            self.assertEqual((root/'data/state.json').read_text(),'keep')

    def test_missing_identity_writes_no_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                initialize(Path(directory),'app',dt.datetime.now(dt.timezone.utc),lambda c: {})
            self.assertFalse((Path(directory)/'config.docker.json').exists())


if __name__ == '__main__':unittest.main()

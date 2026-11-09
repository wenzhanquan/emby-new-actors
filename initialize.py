#!/usr/bin/env python3
"""Initialize a new deployment without including historical people."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
from refresh_new_actors import Api, load_token, new_state, atomic_save


def initialize(root, app_name, now, server_info=None, base_url='http://127.0.0.1:8096/emby'):
    root=Path(root)
    targets=[root/'config.docker.json',root/'data/state.json',root/'data/scheduler.json']
    if any(p.exists() for p in targets):
        raise FileExistsError('existing_config_or_state_restore_instead')
    if not app_name.strip():
        raise ValueError('api_app_name_required')
    cfg=dict(base_url=base_url,auth_db='/emby-auth/authentication.db',
             api_app_name=app_name.strip(),state_file='/data/state.json',max_refresh=50)
    info=server_info(cfg) if server_info else Api(cfg['base_url'],load_token(cfg)).info()
    if not info.get('Id'):
        raise ValueError('server_identity_missing')
    cfg.update(server_id=info['Id'],baseline=now.astimezone(dt.timezone.utc).isoformat())
    state=new_state(cfg['server_id'],now)
    (root/'data').mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_save(root/'data/state.json',state)
    atomic_save(root/'config.docker.json',cfg)
    return cfg,state


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='/bootstrap')
    parser.add_argument('--api-app-name',required=True)
    parser.add_argument('--base-url',default='http://127.0.0.1:8096/emby')
    args=parser.parse_args();os.umask(0o077)
    try:
        cfg,_=initialize(Path(args.root),args.api_app_name,dt.datetime.now(dt.timezone.utc),base_url=args.base_url)
        print(json.dumps({'status':'initialized','baseline':cfg['baseline']},ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status':'failed','reason':type(exc).__name__}))
        raise SystemExit(1)

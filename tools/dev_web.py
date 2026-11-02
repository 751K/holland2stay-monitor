#!/usr/bin/env python3
"""Run the production Web locally with an isolated database and test accounts.

python tools/dev_web.py --seed-prototype
Uses loopback only; never loads the deployment .env or starts a monitor.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path('/private/tmp/flatradar-web-local'))
    parser.add_argument('--port',type=int,default=19568)
    parser.add_argument('--reload',action='store_true',help='Reload the local Web process after source changes')
    parser.add_argument('--seed-prototype',action='store_true',help='Seed an empty database with the public prototype snapshot')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root))
    local = args.data_dir.resolve()
    if local == root/'data':
        parser.error('Use a separate development data directory, not the project data directory.')
    local.mkdir(parents=True,exist_ok=True)
    from cryptography.fernet import Fernet

    def persistent_key(name,generate):
        path = local/name
        if not path.exists():
            path.write_text(generate())
            path.chmod(0o600)
        return path.read_text().strip()

    os.environ.update(PYTHON_DOTENV_DISABLED='1',DB_PATH=str(local/'listings.db'),
        DATA_DIR=str(local),WEB_USERNAME='local-admin',WEB_PASSWORD='local-admin-password',
        WEB_GUEST_MODE='true',SESSION_COOKIE_SECURE='false',TIMEZONE='Europe/Amsterdam',
        TRUSTED_PROXY_HOPS='0',FLASK_SECRET=persistent_key('session.key',lambda:secrets.token_hex(32)),
        DATA_ENCRYPTION_KEY=persistent_key('encryption.key',lambda:Fernet.generate_key().decode()))
    # Reuse only the browser Maps key; all deployment/runtime settings remain isolated.
    from dotenv import dotenv_values
    maps_key = os.environ.get('GOOGLE_MAPS_API_KEY') or dotenv_values(root/'.env').get('GOOGLE_MAPS_API_KEY')
    if maps_key:
        os.environ['GOOGLE_MAPS_API_KEY'] = maps_key
    import config
    config.DATA_DIR = local
    config.ENV_PATH = local/'.env'
    config.ENV_PATH.touch(exist_ok=True)
    from web import app, _ASSET_VERSIONS
    from app.db import storage
    from users import UserConfig, load_users, save_users, set_app_password
    from config import ListingFilter
    from models import Listing

    if not load_users():
        user = UserConfig(name='local-user',app_login_enabled=True,language='zh',
            notifications_enabled=False,listing_filter=ListingFilter(max_rent=1500,min_area=20))
        set_app_password(user,'local-user-password')
        save_users([user])
    if args.seed_prototype:
        snapshot_path = root/'prototypes/web-platform-inbox/data.js'
        if not snapshot_path.exists():
            parser.error('Prototype snapshot is missing; run without --seed-prototype for an empty database.')
        snapshot = json.loads(snapshot_path.read_text().removeprefix('const snapshot = ').rstrip().removesuffix(';'))
        st = storage()
        try:
            if not st.count_all():
                st.diff([Listing(id=f'local-{i}',name=row['name'],status=row['status'],city=row['city'],
                    source=row['source'],price_raw=row['price_raw'],available_from=row['available_from'],
                    url=row['url'],features=row.get('features') or ([f"Area: {row['area_value']} m²"] if row['area_value'] is not None else []))
                    for i,row in enumerate(snapshot['listings'])])
                for i,row in enumerate(snapshot['listings']):
                    st.conn.execute('UPDATE listings SET first_seen=?,status_is_inferred=?,last_seen=? WHERE id=?',
                        (row['first_seen'],row['status_is_inferred'],row.get('last_seen') or snapshot['capturedAt'],f'local-{i}'))
                st.conn.commit()
            # Coordinate snapshots contain only public address-cache data.
            # Existing development read state and listing records stay intact.
            for i,row in enumerate(snapshot['listings']):
                coords=row.get('coordinates')
                entry=st.get_map_listing_by_id(f'local-{i}')
                if coords and entry and entry['name']==row['name'] and entry['source']==row['source']:
                    st.cache_coords(entry['address'],coords['lat'],coords['lng'])
        finally:
            st.close()
    from flask import abort, request
    @app.before_request
    def local_only_guard():
        # Production caches asset versions per process; local edits must become
        # visible without restarting Python for every CSS/JS change.
        _ASSET_VERSIONS.clear()
        if request.path in ('/api/monitor/start','/api/monitor/stop','/api/shutdown'):
            abort(403)
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
    print(f'Local Web: http://127.0.0.1:{args.port}/login?lang=zh',flush=True)
    print('User: local-user / local-user-password; admin: local-admin / local-admin-password',flush=True)
    app.run(host='127.0.0.1',port=args.port,threaded=True,use_reloader=args.reload)


if __name__ == '__main__':
    main()

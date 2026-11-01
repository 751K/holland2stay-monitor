"""Monitor acknowledgement for the settings page, without storing secret values."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from dotenv import dotenv_values

from env_registry import DEPLOY_KEYS, RUNTIME_KEYS, SECRET_KEYS, TUNING_KEYS

META_KEY = 'monitor_config_applied_v1'
_FIXED_KEYS = DEPLOY_KEYS | SECRET_KEYS | TUNING_KEYS
_startup_fixed = None
_UNSET = object()


def saved_fingerprints(values):
    # app_settings is restricted to non-secret runtime keys. Raw values are not
    # published in meta or returned to the settings page.
    return {key: hashlib.sha256(value.encode('utf-8')).hexdigest()
            for key, value in values.items() if key in RUNTIME_KEYS}


def _fixed_dotenv(path):
    raw = dotenv_values(dotenv_path=path, interpolate=False)
    return {k: v for k, v in raw.items() if k in _FIXED_KEYS and v is not None}


def file_stamp(path):
    try:
        st = Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def capture_startup(path, *, expected_stamp=_UNSET):
    global _startup_fixed
    _startup_fixed = None
    before = file_stamp(path)
    if expected_stamp is not _UNSET and before != expected_stamp:
        return
    values = _fixed_dotenv(path)
    if file_stamp(path) == before:
        _startup_fixed = values


def publish(storage, candidate, path, *, pid=None):
    """Acknowledge only the candidate actually committed by this monitor."""
    from settings_store import _external_fixed_keys, _external_runtime
    fixed = _fixed_dotenv(path)
    externally_fixed = _external_fixed_keys | set(_external_runtime or {})
    changed = (sorted(k for k in set(fixed) | set(_startup_fixed or {})
                      if k not in externally_fixed and fixed.get(k) != (_startup_fixed or {}).get(k))
               if _startup_fixed is not None else None)
    record = {
        'pid': pid if pid is not None else os.getpid(),
        'version': candidate.version,
        'saved': dict(candidate.saved_fingerprints) if candidate.saved_fingerprints is not None else None,
        'overridden': sorted(k for k, v in candidate.sources.items() if v == 'env'),
        'restart_required': changed,
        'fixed_overridden': sorted((set(fixed) | set(_startup_fixed or {})) & externally_fixed & _FIXED_KEYS),
        'dotenv_stamp': candidate.dotenv_stamp,
    }
    storage.set_meta(META_KEY, json.dumps(record, sort_keys=True))


def read_status(storage, saved, path, *, monitor_pid):
    try:
        record = json.loads(storage.get_meta(META_KEY, default='') or '{}')
    except (ValueError, TypeError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    running = monitor_pid is not None
    confirmed = running and record.get('pid') == monitor_pid and isinstance(record.get('saved'), dict)
    if not confirmed:
        return {'state': 'unknown' if running else 'stopped', 'pending': [],
                'overridden': [], 'fixed_overridden': [], 'restart_required': [], 'restart_unknown': True}
    current = saved_fingerprints(saved)
    applied = record['saved']
    overridden = set(record.get('overridden', [])) & RUNTIME_KEYS
    pending = sorted(k for k in current.keys() | applied.keys()
                     if k not in overridden and current.get(k) != applied.get(k))
    recorded_stamp = record.get('dotenv_stamp')
    dotenv_changed = (tuple(recorded_stamp) if recorded_stamp is not None else None) != file_stamp(path)
    return {
        'state': 'pending' if pending or dotenv_changed else 'applied',
        'pending': pending,
        'overridden': sorted(overridden),
        'restart_required': [k for k in record.get('restart_required') or [] if k in _FIXED_KEYS],
        'fixed_overridden': [k for k in record.get('fixed_overridden') or [] if k in _FIXED_KEYS],
        'restart_unknown': dotenv_changed or record.get('restart_required') is None,
    }

"""守住 conftest 的 ``_isolate_monitor_control_files``：测试不许碰真实 data/ 下
的 monitor.pid / monitor.reload，且写端和读端仍指向同一个文件。"""
from __future__ import annotations

import config
import monitor
from app import process_ctrl
from app.services import monitor_service


def test_paths_are_not_under_real_data_dir():
    real = config.DATA_DIR.resolve()
    for p in (process_ctrl.RELOAD_REQUEST_FILE, process_ctrl.PID_FILE,
              monitor_service.PID_FILE, monitor._RELOAD_REQUEST_FILE, monitor._PID_FILE):
        assert real not in p.resolve().parents, p


def test_writer_and_reader_share_one_file():
    process_ctrl.write_reload_request()
    assert monitor._consume_reload_request_file() is True
    assert monitor._consume_reload_request_file() is False


def test_pid_paths_agree():
    assert process_ctrl.PID_FILE == monitor_service.PID_FILE == monitor._PID_FILE

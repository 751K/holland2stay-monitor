"""Web requests use one resolved saved configuration without mutating the process."""
from __future__ import annotations

from app.db import storage


def _web_worker(db_path, env_path, pipe):
    import config
    import app.db
    import settings_store
    from app.runtime_config import web_runtime_environment
    from flask import Flask

    config.DB_PATH = app.db.DB_PATH = db_path
    config.ENV_PATH = env_path
    settings_store._external_runtime = {}
    settings_store._active_runtime = None
    app = Flask(__name__)
    while pipe.recv() == "read":
        with app.test_request_context("/"):
            from flask import g
            pipe.send(web_runtime_environment().get("MAP_MAX_AGE_DAYS"))
            st = g.pop("_storage", None)
            if st is not None:
                st._teardown_managed = False
                st.close()


def _save(values):
    st = storage()
    try:
        st.set_app_settings(values)
    finally:
        st.close()


def test_real_map_requests_use_updated_saved_age(admin_client, monkeypatch):
    monkeypatch.setenv("MAP_MAX_AGE_DAYS", "14")
    _save({"MAP_MAX_AGE_DAYS": "14"})
    st = storage()
    try:
        st.conn.execute(
            "INSERT INTO listings (id, name, status, price_raw, available_from, url, city, source, "
            "features, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "datetime('now', '-6 days'), datetime('now', '-5 days'))",
            ("aged", "House", "Occupied", "€900", "", "https://example.com", "Eindhoven", "holland2stay", "[]"),
        )
        st.conn.commit()
        row = st.get_map_listing_by_id("aged")
        st.cache_coords(row["address"], 51.4, 5.4)
    finally:
        st.close()
    assert len(admin_client.get("/api/map").get_json()["listings"]) == 1
    _save({"MAP_MAX_AGE_DAYS": "2"})
    assert admin_client.get("/api/map").get_json()["listings"] == []
    import os
    assert os.environ["MAP_MAX_AGE_DAYS"] == "14"


def test_home_and_user_options_update_without_monitor_or_web_restart(admin_client, test_app):
    from app.routes.users import _city_options

    _save({"SOURCES": "holland2stay", "CITIES": "Eindhoven,29"})
    assert "Plaza" not in admin_client.get("/").get_data(as_text=True).split('class="coverage-grid"')[1].split('</div>')[0]
    with test_app.test_request_context("/users/new"):
        before = _city_options()
    _save({"SOURCES": "holland2stay,plaza", "PLAZA_CITIES": "Groot-Ammers,groot-ammers"})
    html = admin_client.get("/").get_data(as_text=True)
    assert "Configured monitoring platforms and cities" in html
    assert "Groot-Ammers" in html
    with test_app.test_request_context("/users/new"):
        after = _city_options()
    assert "Groot-Ammers" not in before and "Groot-Ammers" in after


def test_request_snapshot_is_stable_and_another_worker_can_see_new_settings(test_app, monkeypatch):
    from app import runtime_config
    _save({"MAP_MAX_AGE_DAYS": "14"})
    with test_app.test_request_context("/first"):
        first = runtime_config.web_runtime_environment()
        _save({"MAP_MAX_AGE_DAYS": "2"})
        assert runtime_config.web_runtime_environment() is first
        assert first["MAP_MAX_AGE_DAYS"] == "14"
    # Model a newly started worker with an empty last-good cache.
    monkeypatch.setattr(runtime_config, "_last_good", None)
    monkeypatch.setattr(runtime_config, "_last_good_key", None)
    with test_app.test_request_context("/second"):
        assert runtime_config.web_runtime_environment()["MAP_MAX_AGE_DAYS"] == "2"


def test_independent_workers_read_saved_changes(test_app, isolated_data_dir):
    import multiprocessing

    _save({"MAP_MAX_AGE_DAYS": "14"})
    ctx = multiprocessing.get_context("spawn")
    workers = []
    try:
        for _ in range(2):
            parent, child = ctx.Pipe()
            proc = ctx.Process(target=_web_worker, args=(isolated_data_dir / "listings.db",
                                                        isolated_data_dir / ".env", child))
            proc.start()
            child.close()
            workers.append((proc, parent))
        for _, pipe in workers:
            pipe.send("read")
        assert all(pipe.poll(15) for _, pipe in workers)
        assert [pipe.recv() for _, pipe in workers] == ["14", "14"]
        _save({"MAP_MAX_AGE_DAYS": "2"})
        for _, pipe in workers:
            pipe.send("read")
        assert all(pipe.poll(15) for _, pipe in workers)
        assert [pipe.recv() for _, pipe in workers] == ["2", "2"]
    finally:
        for proc, pipe in workers:
            if proc.is_alive():
                pipe.send("stop")
            proc.join(timeout=10)
            if proc.is_alive():
                proc.terminate()
                proc.join()
            pipe.close()


def test_override_deletion_and_failed_candidate_fallback(test_app, monkeypatch, isolated_data_dir):
    import settings_store
    from app.runtime_config import web_runtime_environment

    env_file = isolated_data_dir / ".env"
    env_file.write_text("MAP_MAX_AGE_DAYS=8\n", encoding="utf-8")
    monkeypatch.setattr(settings_store, "_external_runtime", {"SOURCES": "holland2stay"})
    _save({"MAP_MAX_AGE_DAYS": "3", "SOURCES": "plaza"})
    with test_app.test_request_context("/first"):
        snapshot = web_runtime_environment()
        assert snapshot["MAP_MAX_AGE_DAYS"] == "8" and snapshot["SOURCES"] == "holland2stay"
    env_file.write_text("", encoding="utf-8")
    with test_app.test_request_context("/second"):
        assert web_runtime_environment()["MAP_MAX_AGE_DAYS"] == "3"
    _save({"CHECK_INTERVAL": "not-a-number", "MAP_MAX_AGE_DAYS": "1"})
    with test_app.test_request_context("/third"):
        assert web_runtime_environment()["MAP_MAX_AGE_DAYS"] == "3"
    _save({"CHECK_INTERVAL": "300"})
    with test_app.test_request_context("/fourth"):
        assert web_runtime_environment()["MAP_MAX_AGE_DAYS"] == "1"

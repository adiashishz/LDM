import sqlite3

from ldm.store import MIGRATIONS, Store


def test_migrates_a_version_1_database(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(MIGRATIONS[0] + "PRAGMA user_version = 1;")
    db.execute("INSERT INTO downloads (url, state, dir, added_at) VALUES ('http://x/a', 'paused', '/tmp', 0)")
    db.commit()
    db.close()

    store = Store(path)
    d = store.get(1)
    assert (d.state, d.position, d.speed_limit) == ("paused", 1, 0)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_settings_defaults_and_overrides(tmp_path):
    store = Store(tmp_path / "s.db")
    assert store.get_settings()["max_parallel"] == 3
    store.set_setting("max_parallel", 5)
    assert Store(tmp_path / "s.db").get_settings()["max_parallel"] == 5


def test_host_rule_matching(tmp_path):
    store = Store(tmp_path / "s.db")
    store.set_host_rule("example.com", max_downloads=1)
    store.set_host_rule("exact.org", max_downloads=2, include_subdomains=False)
    assert store.host_rule_for("cdn.example.com").host == "example.com"
    assert store.host_rule_for("EXAMPLE.com").max_downloads == 1
    assert store.host_rule_for("exact.org").max_downloads == 2
    assert store.host_rule_for("www.exact.org") is None
    assert store.host_rule_for("other.net") is None


def test_queue_order_and_requeue(tmp_path):
    store = Store(tmp_path / "s.db")
    a = store.add("http://h/a", tmp_path)
    b = store.add("http://h/b", tmp_path)
    store.move(b, 0)
    assert [d.id for d in store.queued()] == [b, a]
    store.set_state(a, "running")
    assert store.requeue_interrupted() == [a]
    assert store.get(a).state == "queued"

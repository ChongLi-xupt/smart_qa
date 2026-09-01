"""schema_cache TTL 缓存回归测试（不连真实数据库）。

运行方式：``python tests/test_schema_cache.py``（或 pytest）。
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schema_cache import SchemaCache


class FakeDB:
    """记录调用次数的假数据库，模拟 MySQLDatabase 的 _fetch_* 原始方法。

    公开 get_* 方法一律抛错：SchemaCache 若误调公开分发方法，
    真实环境中会反向重入缓存导致同线程死锁，这里直接暴露回归。
    """

    def __init__(self):
        self.tables_calls = 0
        self.columns_calls = 0
        self.schema_calls: dict[str, int] = {}
        self.relationships_calls = 0

    def _fetch_tables(self, schema=None):
        self.tables_calls += 1
        return [{"name": "user", "comment": "用户表"}, {"name": "order", "comment": None}]

    def _fetch_column_names(self, table_name=None, schema=None):
        self.columns_calls += 1
        all_columns = {"user": ["id", "name"], "order": ["id", "user_id", "amount"]}
        if table_name is None:
            return all_columns
        if table_name not in all_columns:
            raise KeyError(f"no such table: {table_name}")  # 模拟 NoSuchTableError
        return all_columns[table_name]

    def _fetch_schema(self, table_name=None, schema=None):
        if table_name is None:
            return {"user": {"name": "user"}, "order": {"name": "order"}}
        self.schema_calls[table_name] = self.schema_calls.get(table_name, 0) + 1
        if table_name not in ("user", "order"):
            raise KeyError(f"no such table: {table_name}")
        return {"name": table_name, "columns": []}

    def _fetch_relationships(self):
        self.relationships_calls += 1
        return [
            {
                "source_table": "order",
                "constrained_columns": ["user_id"],
                "referred_table": "user",
                "referred_columns": ["id"],
            }
        ]

    def get_tables(self, *args, **kwargs):
        raise AssertionError("SchemaCache 必须调用 _fetch_* 原始方法，不得回调公开分发方法")

    def get_column_names(self, *args, **kwargs):
        raise AssertionError("SchemaCache 必须调用 _fetch_* 原始方法，不得回调公开分发方法")

    def get_schema(self, *args, **kwargs):
        raise AssertionError("SchemaCache 必须调用 _fetch_* 原始方法，不得回调公开分发方法")


def test_tables_cached_within_ttl():
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    for _ in range(5):
        assert cache.get_tables()[0]["name"] == "user"
    assert db.tables_calls == 1  # TTL 内只查库一次


def test_columns_all_and_by_table_hit_cache():
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    assert cache.get_column_names("user") == ["id", "name"]
    assert "order" in cache.get_column_names()
    # 全库列与按表列共用一次刷新
    assert db.tables_calls == 1 and db.columns_calls == 1


def test_columns_lookup_case_insensitive():
    cache = SchemaCache(FakeDB(), ttl=300)
    assert cache.get_column_names("USER") == ["id", "name"]


def test_columns_miss_falls_back_to_db():
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    cache.get_tables()
    try:
        cache.get_column_names("ghost_table")
    except KeyError:
        pass
    else:
        raise AssertionError("不存在的表应回退直查并抛错")


def test_schema_per_table_cached():
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    for _ in range(3):
        assert cache.get_schema("user")["name"] == "user"
    assert db.schema_calls == {"user": 1}


def test_stale_cache_refreshes():
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    cache.get_tables()
    cache._ts -= 400  # 人为把时间戳拨到 TTL 之外
    cache.get_tables()
    assert db.tables_calls == 2


def test_invalidate_forces_refresh():
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    cache.get_tables()
    cache.invalidate()
    cache.get_tables()
    assert db.tables_calls == 2


def test_concurrent_refresh_single_db_call():
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        try:
            barrier.wait()
            cache.get_tables()
            cache.get_column_names("user")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert db.tables_calls == 1 and db.columns_calls == 1  # 并发下只刷新一次


def test_relationships_lazy_loaded_once():
    """外键关系懒加载，首次请求后不再重复查库。"""
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    for _ in range(3):
        relationships = cache.get_relationships()
        assert relationships[0]["referred_table"] == "user"
    assert db.relationships_calls == 1


def test_relationships_expired_with_ttl():
    """TTL 刷新时外键关系一并失效，下次请求重新拉取。"""
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    cache.get_relationships()
    cache._ts -= 400  # 人为把时间戳拨到 TTL 之外
    cache.get_relationships()
    assert db.relationships_calls == 2


def test_relationships_cleared_on_invalidate():
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    cache.get_relationships()
    cache.invalidate()
    cache.get_relationships()
    assert db.relationships_calls == 2


def test_relationships_concurrent_single_fetch():
    """并发首次请求下只有一个线程真正查库。"""
    db = FakeDB()
    cache = SchemaCache(db, ttl=300)
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        try:
            barrier.wait()
            cache.get_relationships()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert db.relationships_calls == 1


if __name__ == "__main__":
    import sys

    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n共 {failures} 个失败" if failures else "\n全部通过")
    sys.exit(1 if failures else 0)

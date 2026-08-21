"""Schema 元数据 TTL 缓存（P2/D2）。

表结构属于低频变化数据：一次提问中，列表工具、schema 工具、
``sql_guard.validate_sql_schema`` 防幻觉核对都会反复查询
``information_schema``，每次提问产生 2~N 次往返。本模块把元数据
缓存几分钟，过期后由双重检查锁保证只有一个线程去刷新。

缓存只覆盖默认 schema（即当前数据库）的常规读取路径；显式指定
``schema`` 参数的少见用法仍直查数据库，保证行为与未缓存时一致。
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅用于类型标注，避免运行期循环导入
    from database import MySQLDatabase


class SchemaCache:
    """为 :class:`MySQLDatabase` 提供线程安全的元数据 TTL 缓存。

    用法::

        cache = SchemaCache(db, ttl=300)
        tables = cache.get_tables()          # TTL 内不再查库
        columns = cache.get_column_names("user")

    ``get_column_names`` / ``get_schema`` 按表名未命中时回退直查
    （由数据库层负责抛 ``NoSuchTableError``），保证缓存期间新建的
    表也不会被误报为不存在。

    重要：本类对数据库层一律调用 ``_fetch_*`` 原始方法，绝不能调用
    ``get_*`` 公开方法——公开方法会把默认 schema 的请求再次分发回
    缓存，造成同线程重入非可重入锁而死锁。
    """

    def __init__(self, db: "MySQLDatabase", ttl: int = 300) -> None:
        self._db = db
        self._ttl = max(1, int(ttl))
        self._lock = threading.Lock()
        self._tables: list[dict[str, Any]] | None = None
        self._columns: dict[str, list[str]] | None = None
        self._schemas: dict[str, dict[str, Any]] = {}
        self._ts = 0.0

    # ------------------------------------------------------------------ #
    # 对外接口（与 MySQLDatabase 同名方法签名一致）
    # ------------------------------------------------------------------ #

    def get_tables(self) -> list[dict[str, Any]]:
        self._refresh_if_stale()
        return list(self._tables or [])

    def get_column_names(self, table_name: str | None = None) -> Any:
        self._refresh_if_stale()
        if table_name is None:
            return {name: list(cols) for name, cols in (self._columns or {}).items()}

        columns = self._lookup_columns(table_name)
        if columns is None:
            # 未命中（大小写不一致 / 缓存后新建的表）：直查，行为与原来一致
            return self._db._fetch_column_names(table_name)
        return list(columns)

    def get_schema(self, table_name: str | None = None) -> Any:
        if table_name is None:
            # 全库 schema 体积大且几乎不会被整库请求，直查即可
            return self._db._fetch_schema()

        self._refresh_if_stale()
        cached = self._schemas.get(table_name)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._schemas.get(table_name)
            if cached is not None:
                return cached
            # 表名不存在时由数据库层抛 NoSuchTableError，与未缓存行为一致
            info = self._db._fetch_schema(table_name)
            self._schemas[table_name] = info
            return info

    def invalidate(self) -> None:
        """清空全部缓存（数据库连接关闭时调用）。"""
        with self._lock:
            self._tables = None
            self._columns = None
            self._schemas = {}
            self._ts = 0.0

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    def _is_fresh(self) -> bool:
        return self._tables is not None and (time.monotonic() - self._ts) < self._ttl

    def _refresh_if_stale(self) -> None:
        """TTL 内直接返回；过期时加锁刷新，并发下只有一个线程查库。"""
        if self._is_fresh():
            return
        with self._lock:
            if self._is_fresh():
                return
            self._tables = self._db._fetch_tables()
            self._columns = self._db._fetch_column_names()
            self._schemas = {}
            self._ts = time.monotonic()

    def _lookup_columns(self, table_name: str) -> list[str] | None:
        columns = (self._columns or {}).get(table_name)
        if columns is not None:
            return columns
        lowered = table_name.lower()
        for name, cols in (self._columns or {}).items():
            if name.lower() == lowered:
                return cols
        return None

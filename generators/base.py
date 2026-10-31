"""Shared generation context and helpers.

The :class:`World` object accumulates every generated row as a plain dict,
keyed by table name, and holds lightweight cross-reference caches used by the
individual generators. ``load_data`` converts the dicts to COPY-ready tuples.
"""
from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from faker import Faker

CENT = Decimal("0.01")


def money(value: Decimal | float | int) -> Decimal:
    """Round to 2 decimal places (banker-safe HALF_UP) for monetary columns."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def rand_dt(rng: random.Random, start: datetime, end: datetime) -> datetime:
    """Uniform random timestamp in [start, end)."""
    if end <= start:
        return start
    seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=rng.randint(0, max(seconds - 1, 0)))


def business_days_between(start: date, end: date) -> int:
    return (end - start).days


class World:
    """Container for all generated rows plus reference caches."""

    def __init__(self, cfg: Any, seed: int) -> None:
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.fake = Faker()
        Faker.seed(seed)
        self.generated_at = datetime.combine(cfg.history_end, time.max, tzinfo=timezone.utc)

        self.rows: dict[str, list[dict]] = defaultdict(list)
        self._ids: dict[str, int] = defaultdict(int)

        # Cross-reference caches (populated by generators)
        self.branches: list[dict] = []
        self.hq: dict | None = None
        self.warehouses: list[dict] = []
        self.central_warehouse: dict | None = None
        self.branch_warehouse: dict[int, dict] = {}
        self.branch_warehouse_services: list[dict] = []
        self.employees: list[dict] = []
        self.categories: list[dict] = []
        self.products: list[dict] = []
        self.product_by_id: dict[int, dict] = {}
        self.suppliers: list[dict] = []
        self.supplier_products: list[dict] = []
        self.sp_by_product: dict[int, list[dict]] = defaultdict(list)
        self.customers: list[dict] = []
        self.sales_orders: list[dict] = []
        self.order_items: list[dict] = []
        self.purchase_orders: list[dict] = []
        self.po_items: list[dict] = []
        self.transfers: list[dict] = []
        self.transfer_items: list[dict] = []

    # -- id + row helpers ---------------------------------------------------
    def next_id(self, table: str) -> int:
        self._ids[table] += 1
        return self._ids[table]

    def add(self, table: str, row: dict) -> dict:
        self.rows[table].append(row)
        return row

    def count(self, table: str) -> int:
        return len(self.rows[table])

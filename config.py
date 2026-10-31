"""Central configuration for the synthetic ERP dataset generator.

All dataset sizes are configurable, either by editing the defaults here or by
setting environment variables (see .env.example). The random seed is global and
makes the whole dataset reproducible.

No database credentials live in this file. Connection settings are read from
environment variables by ``db.py``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # noqa: TRY003
        raise ValueError(f"Environment variable {name}={raw!r} is not a valid integer") from exc


def _env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name)
    return Decimal(raw) if raw else Decimal(default)


@dataclass(frozen=True)
class DatasetConfig:
    """Sizes and global parameters for the generated dataset."""

    seed: int = _env_int("ERP_SEED", 20240101)

    # Organizational scale
    n_branches: int = _env_int("ERP_N_BRANCHES", 5)          # excluding HQ
    n_warehouses: int = _env_int("ERP_N_WAREHOUSES", 3)      # includes 1 central
    n_employees: int = _env_int("ERP_N_EMPLOYEES", 120)

    # Catalog / partners
    n_suppliers: int = _env_int("ERP_N_SUPPLIERS", 20)
    n_products: int = _env_int("ERP_N_PRODUCTS", 500)
    n_customers: int = _env_int("ERP_N_CUSTOMERS", 300)

    # Transactions
    n_sales_orders: int = _env_int("ERP_N_SALES_ORDERS", 10_000)
    max_items_per_order: int = _env_int("ERP_MAX_ITEMS_PER_ORDER", 10)
    n_purchase_orders: int = _env_int("ERP_N_PURCHASE_ORDERS", 500)
    n_stock_transfers: int = _env_int("ERP_N_STOCK_TRANSFERS", 200)

    # Time window for the synthetic history
    history_start: date = field(
        default_factory=lambda: date.fromisoformat(os.getenv("ERP_HISTORY_START", "2023-01-01"))
    )
    history_end: date = field(
        default_factory=lambda: date.fromisoformat(os.getenv("ERP_HISTORY_END", "2023-12-31"))
    )

    # Financial parameters
    default_tax_rate: Decimal = _env_decimal("ERP_TAX_RATE", "0.08")
    currency: str = os.getenv("ERP_CURRENCY", "USD")

    # Behavioural mix
    cancel_rate: float = float(os.getenv("ERP_CANCEL_RATE", "0.04"))
    return_rate: float = float(os.getenv("ERP_RETURN_RATE", "0.06"))
    delayed_shipment_rate: float = float(os.getenv("ERP_DELAYED_SHIPMENT_RATE", "0.08"))
    partial_po_receipt_rate: float = float(os.getenv("ERP_PARTIAL_PO_RATE", "0.15"))
    delayed_po_rate: float = float(os.getenv("ERP_DELAYED_PO_RATE", "0.12"))

    @property
    def n_days(self) -> int:
        return (self.history_end - self.history_start).days + 1

    def validate(self) -> None:
        if self.history_end <= self.history_start:
            raise ValueError("history_end must be after history_start")
        if self.n_warehouses < 2:
            raise ValueError("At least 2 warehouses are required (central + branch)")
        if self.n_branches < 1:
            raise ValueError("At least 1 branch (besides HQ) is required")
        if self.max_items_per_order < 1:
            raise ValueError("max_items_per_order must be >= 1")


CONFIG = DatasetConfig()

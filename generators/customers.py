"""Customer generators."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .base import World, money

TIERS = ["none", "bronze", "silver", "gold", "platinum"]
TIER_WEIGHTS = [35, 30, 20, 10, 5]


def generate(world: World) -> None:
    cfg = world.cfg
    rng = world.rng
    now = world.generated_at

    for cid in range(1, cfg.n_customers + 1):
        is_business = rng.random() < 0.35
        city_row = rng.choice(world.branches)
        tier = rng.choices(TIERS, weights=TIER_WEIGHTS, k=1)[0]
        reg = world.fake.date_between(
            start_date=cfg.history_start - timedelta(days=6 * 365),
            end_date=cfg.history_start,
        )
        first = world.fake.first_name()
        last = world.fake.last_name()
        company = f"{world.fake.last_name()} {rng.choice(['Retail','Stores','Enterprises','Group','Supplies','Markets'])}"
        customer = {
            "customer_id": world.next_id("customers"),
            "customer_code": f"CUST{cid:05d}",
            "customer_type": "business" if is_business else "individual",
            "first_name": None if is_business else first,
            "last_name": None if is_business else last,
            "company_name": company if is_business else None,
            "email": f"customer{cid}@example.com",
            "phone": world.fake.phone_number()[:32],
            "address_line": world.fake.street_address(),
            "city": city_row["city"],
            "region": city_row["region"],
            "country": city_row["country"],
            "loyalty_tier": tier,
            "credit_limit": money(Decimal(rng.choice([0, 500, 1000, 2500, 5000, 10000, 25000])))
            if is_business else Decimal("0"),
            "registration_date": reg,
            "is_active": True,
            "created_at": datetime.combine(reg, datetime.min.time(), tzinfo=timezone.utc),
            "updated_at": datetime.combine(reg, datetime.min.time(), tzinfo=timezone.utc),
        }
        world.add("customers", customer)
        world.customers.append(customer)

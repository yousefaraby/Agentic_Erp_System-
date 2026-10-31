"""Supplier generators: suppliers and supplier_products (pricing + lead times)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from .base import World, money

COUNTRIES = ["United States", "Canada", "Mexico", "Germany", "China", "Vietnam",
             "India", "Turkey", "Spain", "Brazil"]

COMPANY_SUFFIX = ["Trading Co.", "Supply Group", "Industries", "Wholesale", "Distribution",
                  "Global Sourcing", "Import & Export", "Manufacturing", "Partners", "Logistics"]

PAYMENT_TERMS = ["NET15", "NET30", "NET45", "NET60", "PREPAID", "COD"]


def generate(world: World) -> None:
    cfg = world.cfg
    rng = world.rng
    now = world.generated_at
    valid_from = cfg.history_start

    # ---- suppliers -------------------------------------------------------
    world.supplier_perf = {}
    for sid in range(1, cfg.n_suppliers + 1):
        city = world.fake.city()
        country = rng.choice(COUNTRIES)
        base_lead = rng.choice([3, 5, 7, 10, 14, 21, 30])
        rating = round(rng.uniform(2.5, 5.0), 2)
        # on-time performance correlates loosely with rating
        on_time = min(0.99, max(0.45, rating / 5.0 + rng.uniform(-0.15, 0.10)))
        name = f"{world.fake.last_name()} {rng.choice(COMPANY_SUFFIX)}"
        supplier = {
            "supplier_id": world.next_id("suppliers"),
            "supplier_code": f"SUP{sid:04d}",
            "name": name,
            "contact_name": world.fake.name(),
            "email": f"sales{sid}@supplier{sid}.example",
            "phone": world.fake.phone_number()[:32],
            "address_line": world.fake.street_address(),
            "city": city,
            "country": country,
            "payment_terms": rng.choice(PAYMENT_TERMS),
            "default_lead_time_days": base_lead,
            "rating": Decimal(str(rating)),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        world.add("suppliers", supplier)
        world.suppliers.append(supplier)
        world.supplier_perf[sid] = {
            "on_time_rate": on_time,
            "default_lead_time_days": base_lead,
            "rating": rating,
        }

    # ---- supplier_products ----------------------------------------------
    for product in world.products:
        # 15% single-sourced to create single-supplier dependency scenarios
        single = rng.random() < 0.15
        n_suppliers = 1 if single else rng.choice([2, 2, 3, 3, 4])
        chosen = rng.sample(world.suppliers, k=min(n_suppliers, len(world.suppliers)))
        preferred_idx = rng.randrange(len(chosen))
        for i, supplier in enumerate(chosen):
            base_cost = product["cost_price"]
            # supplier-specific cost around the product's reference cost
            factor = Decimal(str(round(rng.uniform(0.90, 1.12), 3)))
            unit_cost = money(base_cost * factor)
            lead = max(1, supplier["default_lead_time_days"] + rng.choice([-3, -1, 0, 0, 2, 5]))
            sp = {
                "supplier_product_id": world.next_id("supplier_products"),
                "supplier_id": supplier["supplier_id"],
                "product_id": product["product_id"],
                "supplier_sku": f"{supplier['supplier_code']}-{product['product_id']:05d}",
                "unit_cost": unit_cost,
                "lead_time_days": lead,
                "min_order_qty": rng.choice([1, 2, 5, 10, 12, 20]),
                "is_preferred": i == preferred_idx,
                "valid_from": valid_from,
                "valid_to": None,
                "created_at": now,
            }
            world.add("supplier_products", sp)
            world.supplier_products.append(sp)
            world.sp_by_product[product["product_id"]].append(sp)

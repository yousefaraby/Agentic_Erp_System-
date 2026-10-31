"""Product catalog generators: categories and products.

Products also receive non-persisted demand attributes used downstream by the
sales and inventory generators (stored on ``world.product_demand``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .base import World, money

# top-level categories with (markup_low, markup_high, cost_low, cost_high)
CATEGORY_DEFS = {
    "Electronics": (1.25, 1.70, 40, 900),
    "Apparel": (1.80, 2.60, 6, 90),
    "Grocery": (1.15, 1.45, 1, 25),
    "Home & Garden": (1.40, 2.10, 8, 300),
    "Sports & Outdoors": (1.45, 2.20, 12, 400),
    "Toys & Games": (1.60, 2.40, 5, 120),
    "Health & Beauty": (1.70, 2.80, 3, 80),
    "Office Supplies": (1.30, 1.90, 2, 150),
}

SUBCATEGORIES = {
    "Electronics": ["Audio", "Computing", "Mobile Accessories", "Smart Home"],
    "Apparel": ["Menswear", "Womenswear", "Footwear", "Accessories"],
    "Grocery": ["Beverages", "Snacks", "Pantry", "Frozen"],
    "Home & Garden": ["Furniture", "Kitchenware", "Garden Tools", "Decor"],
    "Sports & Outdoors": ["Fitness", "Camping", "Cycling", "Team Sports"],
    "Toys & Games": ["Board Games", "Building Sets", "Outdoor Toys", "Plush"],
    "Health & Beauty": ["Skincare", "Supplements", "Haircare", "Personal Care"],
    "Office Supplies": ["Stationery", "Paper", "Printer Supplies", "Organisation"],
}

ADJ = ["Premium", "Classic", "Compact", "Deluxe", "Essential", "Professional", "Everyday",
       "Ultra", "Eco", "Smart", "Portable", "Heavy-Duty", "Mini", "Advanced", "Signature"]
NOUN = ["Series", "Edition", "Pack", "Set", "Kit", "Unit", "Model", "Bundle", "Collection",
        "System", "Line", "Range"]

SEASONALITY = ["none", "none", "none", "summer", "winter", "holiday", "back_to_school"]


def generate(world: World) -> None:
    cfg = world.cfg
    rng = world.rng
    now = world.generated_at

    # ---- categories ------------------------------------------------------
    for name in CATEGORY_DEFS:
        cat = {
            "category_id": world.next_id("product_categories"),
            "category_code": name.upper().replace(" & ", "_").replace(" ", "_"),
            "name": name,
            "parent_category_id": None,
            "description": f"{name} products",
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        world.add("product_categories", cat)
        world.categories.append(cat)

    root_by_name = {c["name"]: c["category_id"] for c in world.categories}
    leaf_categories: list[dict] = []
    for parent, subs in SUBCATEGORIES.items():
        for sub in subs:
            code = f"{parent[:3]}{sub[:3]}".upper().replace(" ", "")
            cat = {
                "category_id": world.next_id("product_categories"),
                "category_code": f"{code}{len(leaf_categories):02d}",
                "name": sub,
                "parent_category_id": root_by_name[parent],
                "description": f"{sub} ({parent})",
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
            world.add("product_categories", cat)
            world.categories.append(cat)
            leaf_categories.append({**cat, "root_name": parent})

    world.product_demand = {}

    # ---- products --------------------------------------------------------
    # intensity classes to create traceable "high sellers", normal and slow movers
    for pid in range(1, cfg.n_products + 1):
        leaf = leaf_categories[(pid - 1) % len(leaf_categories)]
        root_name = leaf["root_name"]
        m_low, m_high, c_low, c_high = CATEGORY_DEFS[root_name]

        cost = money(Decimal(str(round(rng.uniform(c_low, c_high), 2))))
        markup = Decimal(str(round(rng.uniform(m_low, m_high), 3)))
        list_price = money(cost * markup)

        # demand class: 15% high, 15% low, 70% normal
        r = rng.random()
        demand_class = "high" if r < 0.15 else ("low" if r < 0.30 else "normal")
        base_daily = {"high": rng.uniform(4.0, 12.0),
                      "normal": rng.uniform(0.6, 3.0),
                      "low": rng.uniform(0.05, 0.5)}[demand_class]

        seasonality = rng.choice(SEASONALITY) if demand_class != "high" else \
            rng.choice(["summer", "winter", "holiday", "none"])

        name = (f"{rng.choice(ADJ)} {leaf['name'].rstrip('s')} "
                f"{rng.choice(NOUN)} {pid:04d}")

        reorder_point = max(5, int(base_daily * rng.uniform(10, 25)))
        reorder_qty = max(20, int(base_daily * rng.uniform(30, 60)))
        safety_stock = max(2, int(base_daily * rng.uniform(5, 10)))

        product = {
            "product_id": world.next_id("products"),
            "sku": f"{leaf['category_code'][:6]}-{pid:05d}",
            "name": name,
            "category_id": leaf["category_id"],
            "description": f"{name} - {root_name} / {leaf['name']}",
            "unit_of_measure": "EA",
            "cost_price": cost,
            "list_price": list_price,
            "reorder_point": reorder_point,
            "reorder_quantity": reorder_qty,
            "safety_stock": safety_stock,
            "weight_kg": money(Decimal(str(round(rng.uniform(0.05, 25.0), 3)))),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        world.add("products", product)
        world.products.append(product)
        world.product_by_id[product["product_id"]] = product

        world.product_demand[product["product_id"]] = {
            "base_daily": base_daily,
            "demand_class": demand_class,
            "seasonality": seasonality,
            "root_category": root_name,
            "unit_cost": cost,
        }

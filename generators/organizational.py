"""Organizational generators: branches, warehouses, employees, roles, users."""
from __future__ import annotations

from datetime import datetime, timezone
from datetime import timedelta
from decimal import Decimal

from .base import World

COMPANY_NAME = "Meridian Retail & Distribution Group (synthetic)"

# (city, region, country) - plausible but entirely fictional usage.
HQ_LOCATION = ("Springfield", "Central Region", "United States")
BRANCH_LOCATIONS = [
    ("Riverton", "North Region", "United States"),
    ("Lakeside", "East Region", "United States"),
    ("Fairview", "West Region", "United States"),
    ("Oakdale", "South Region", "United States"),
    ("Brookfield", "Coastal Region", "United States"),
    ("Hillcrest", "Mountain Region", "United States"),
    ("Cedar Park", "Valley Region", "United States"),
]

ROLE_DEFS = [
    ("admin", "Full administrative access"),
    ("branch_manager", "Manages a branch and its staff"),
    ("sales_agent", "Processes sales orders and customers"),
    ("warehouse_manager", "Oversees warehouse inventory and transfers"),
    ("procurement_officer", "Manages suppliers and purchase orders"),
    ("finance_officer", "Handles payments, refunds and reconciliation"),
    ("ai_agent_service", "Service account used by automated agents (read-only)"),
]

PERMISSIONS = [
    "branch.read", "branch.write",
    "warehouse.read", "warehouse.write",
    "product.read", "product.write",
    "supplier.read", "supplier.write",
    "customer.read", "customer.write",
    "sales.read", "sales.write",
    "inventory.read", "inventory.write",
    "transfer.read", "transfer.approve",
    "procurement.read", "procurement.approve",
    "report.read",
]

ROLE_PERMISSIONS = {
    "admin": PERMISSIONS,
    "branch_manager": ["branch.read", "customer.read", "sales.read", "sales.write",
                       "inventory.read", "report.read"],
    "sales_agent": ["customer.read", "customer.write", "sales.read", "sales.write",
                    "product.read", "inventory.read"],
    "warehouse_manager": ["warehouse.read", "warehouse.write", "inventory.read",
                          "inventory.write", "transfer.read", "transfer.approve",
                          "product.read"],
    "procurement_officer": ["supplier.read", "supplier.write", "procurement.read",
                            "procurement.approve", "product.read", "inventory.read"],
    "finance_officer": ["sales.read", "procurement.read", "report.read"],
    "ai_agent_service": ["branch.read", "warehouse.read", "product.read", "supplier.read",
                         "customer.read", "sales.read", "inventory.read", "transfer.read",
                         "procurement.read", "report.read"],
}


def _ts(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def generate(world: World) -> None:
    cfg = world.cfg
    rng = world.rng
    now = world.generated_at

    # ---- roles -----------------------------------------------------------
    for rid, (name, desc) in enumerate(ROLE_DEFS, start=1):
        world.add("roles", {"role_id": rid, "role_name": name,
                            "description": desc, "created_at": now})
    role_id_by_name = {name: i for i, (name, _) in enumerate(ROLE_DEFS, start=1)}
    world.role_ids = role_id_by_name

    # ---- permissions -----------------------------------------------------
    for pid, key in enumerate(PERMISSIONS, start=1):
        world.add("permissions", {"permission_id": pid, "permission_key": key,
                                  "description": f"Allows {key.replace('.', ' ')}"})
    perm_id = {key: i for i, key in enumerate(PERMISSIONS, start=1)}
    for role_name, perms in ROLE_PERMISSIONS.items():
        for p in perms:
            world.add("role_permissions",
                      {"role_id": role_id_by_name[role_name], "permission_id": perm_id[p]})

    # ---- branches (HQ + N) ----------------------------------------------
    locations = [HQ_LOCATION, *BRANCH_LOCATIONS[: cfg.n_branches]]
    for idx, (city, region, country) in enumerate(locations):
        is_hq = idx == 0
        opened = world.fake.date_between(
            start_date=cfg.history_start - timedelta(days=18 * 365),
            end_date=cfg.history_start - timedelta(days=2 * 365),
        )
        branch = {
            "branch_id": world.next_id("branches"),
            "branch_code": "HQ" if is_hq else f"BR{idx:03d}",
            "name": f"{COMPANY_NAME.split(' (')[0]} - {'Headquarters' if is_hq else city + ' Branch'}",
            "branch_type": "headquarters" if is_hq else "branch",
            "address_line": world.fake.street_address(),
            "city": city,
            "region": region,
            "country": country,
            "phone": world.fake.phone_number()[:32],
            "email": f"{'hq' if is_hq else 'br' + str(idx)}@{COMPANY_NAME.split()[0].lower()}.example",
            "opened_date": opened,
            "is_active": True,
            "created_at": _ts(datetime.combine(opened, datetime.min.time())),
            "updated_at": _ts(datetime.combine(opened, datetime.min.time())),
        }
        world.add("branches", branch)
        world.branches.append(branch)
        if is_hq:
            world.hq = branch

    # ---- warehouses (1 central + rest regional) --------------------------
    central_branch = world.hq
    central = {
        "warehouse_id": world.next_id("warehouses"),
        "warehouse_code": "WH-CENTRAL",
        "name": "Central Distribution Center",
        "warehouse_type": "central",
        "branch_id": central_branch["branch_id"],
        "address_line": world.fake.street_address(),
        "city": central_branch["city"],
        "region": central_branch["region"],
        "country": central_branch["country"],
        "capacity_units": rng.randint(120_000, 200_000),
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    world.add("warehouses", central)
    world.warehouses.append(central)
    world.central_warehouse = central

    n_regional = max(cfg.n_warehouses - 1, 1)
    for i in range(n_regional):
        city, region, country = BRANCH_LOCATIONS[i % len(BRANCH_LOCATIONS)]
        wh = {
            "warehouse_id": world.next_id("warehouses"),
            "warehouse_code": f"WH-R{i + 1:02d}",
            "name": f"{city} Regional Warehouse",
            "warehouse_type": "regional",
            "branch_id": None,
            "address_line": world.fake.street_address(),
            "city": city,
            "region": region,
            "country": country,
            "capacity_units": rng.randint(30_000, 70_000),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        world.add("warehouses", wh)
        world.warehouses.append(wh)

    # assign each branch a primary warehouse (spread across all warehouses)
    for i, branch in enumerate(world.branches):
        primary = world.warehouses[i % len(world.warehouses)]
        world.branch_warehouse[branch["branch_id"]] = primary
        world.branch_warehouse_services.append({
            "branch_id": branch["branch_id"],
            "warehouse_id": primary["warehouse_id"],
            "is_primary": True,
            "can_fulfill": True,
            "can_transfer": True,
            "valid_from": cfg.history_start,
            "valid_to": None,
            "created_at": now,
        })
        if primary["warehouse_id"] != world.central_warehouse["warehouse_id"]:
            world.branch_warehouse_services.append({
                "branch_id": branch["branch_id"],
                "warehouse_id": world.central_warehouse["warehouse_id"],
                "is_primary": False,
                "can_fulfill": True,
                "can_transfer": True,
                "valid_from": cfg.history_start,
                "valid_to": None,
                "created_at": now,
            })
    for service in world.branch_warehouse_services:
        world.add("branch_warehouse_services", service)

    # ---- employees -------------------------------------------------------
    dept_titles = {
        "Management": ["Branch Manager", "Operations Manager"],
        "Sales": ["Sales Associate", "Senior Sales Associate", "Sales Lead"],
        "Warehouse": ["Warehouse Associate", "Forklift Operator", "Inventory Clerk"],
        "Procurement": ["Procurement Officer", "Buyer"],
        "Finance": ["Accountant", "Finance Officer"],
        "IT": ["System Administrator", "Data Analyst"],
    }
    departments = list(dept_titles.keys())
    dept_id_by_name = {name: idx for idx, name in enumerate(departments, start=1)}
    for dept_id, dept_name in enumerate(departments, start=1):
        world.add("departments", {
            "department_id": dept_id,
            "department_code": dept_name.upper().replace(" ", "_"),
            "name": dept_name,
            "created_at": now,
        })
    weights = [6, 30, 30, 8, 8, 8]

    for eid in range(1, cfg.n_employees + 1):
        dept = rng.choices(departments, weights=weights, k=1)[0]
        title = rng.choice(dept_titles[dept])
        branch = rng.choice(world.branches)
        wh = world.branch_warehouse[branch["branch_id"]] if dept == "Warehouse" else None
        first = world.fake.first_name()
        last = world.fake.last_name()
        hire = world.fake.date_between(
            start_date=cfg.history_start - timedelta(days=10 * 365),
            end_date=cfg.history_start - timedelta(days=30),
        )
        emp = {
            "employee_id": world.next_id("employees"),
            "employee_code": f"EMP{eid:05d}",
            "branch_id": branch["branch_id"],
            "warehouse_id": wh["warehouse_id"] if wh else None,
            "first_name": first,
            "last_name": last,
            "email": f"{first.lower()}.{last.lower()}{eid}@meridian.example",
            "phone": world.fake.phone_number()[:32],
            "job_title": title,
            "department": dept,
            "department_id": dept_id_by_name[dept],
            "hire_date": hire,
            "termination_date": None,
            "manager_id": None,
            "is_active": True,
            "created_at": _ts(datetime.combine(hire, datetime.min.time())),
            "updated_at": _ts(datetime.combine(hire, datetime.min.time())),
        }
        world.add("employees", emp)
        world.employees.append(emp)

    # managers: first two management employees lead everyone in their branch
    managers = [e for e in world.employees if e["department"] == "Management"]
    if managers:
        for e in world.employees:
            if e["department"] != "Management":
                cands = [m for m in managers if m["branch_id"] == e["branch_id"]]
                if cands:
                    e["manager_id"] = rng.choice(cands)["employee_id"]

    # ---- users -----------------------------------------------------------
    dept_role = {
        "Management": "branch_manager",
        "Sales": "sales_agent",
        "Warehouse": "warehouse_manager",
        "Procurement": "procurement_officer",
        "Finance": "finance_officer",
        "IT": "admin",
    }
    # pick a subset of employees to receive user accounts
    account_holders = managers[:]
    for dept in ("Sales", "Warehouse", "Procurement", "Finance", "IT"):
        staff = [e for e in world.employees if e["department"] == dept]
        account_holders.extend(rng.sample(staff, k=max(1, len(staff) // 4)))
    seen = set()
    for emp in account_holders:
        if emp["employee_id"] in seen:
            continue
        seen.add(emp["employee_id"])
        role = dept_role.get(emp["department"], "sales_agent")
        uname = f"{emp['first_name'].lower()}.{emp['last_name'].lower()}{emp['employee_id']}"
        world.add("users", {
            "user_id": world.next_id("users"),
            "username": uname[:60],
            "email": emp["email"],
            # synthetic, non-functional placeholder hash - never a real credential
            "password_hash": "$2b$12$SYNTHETIC.NOT.A.REAL.HASH." + f"{emp['employee_id']:06d}",
            "employee_id": emp["employee_id"],
            "role_id": world.role_ids[role],
            "is_active": True,
            "last_login_at": now,
            "created_at": emp["created_at"],
            "updated_at": now,
        })

    # dedicated service account for the AI agents
    admin_emp = world.employees[0]
    world.add("users", {
        "user_id": world.next_id("users"),
        "username": "ai_agent_service",
        "email": "ai_agent_service@meridian.example",
        "password_hash": "$2b$12$SYNTHETIC.NOT.A.REAL.HASH.SERVICE",
        "employee_id": admin_emp["employee_id"],
        "role_id": world.role_ids["ai_agent_service"],
        "is_active": True,
        "last_login_at": now,
        "created_at": now,
        "updated_at": now,
    })

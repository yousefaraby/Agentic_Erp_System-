"""Work schedules and gross payroll based on explicit demo attendance and rates."""

from datetime import date, timedelta

from storage import Assessment, Blocked, Draft, source


def weekdays(period_start):
    start = date.fromisoformat(period_start)
    return [(start + timedelta(days=offset)).isoformat() for offset in range(7)
            if (start + timedelta(days=offset)).weekday() < 5]


def active_employees(db):
    return [row for row in source(db, "employees") if row["is_active"]]


def seed_demo_attendance(db, period_start):
    for person in active_employees(db):
        employee_id = person["employee_id"]
        # Fictional rates and clock records for this demo.
        db.execute("INSERT OR IGNORE INTO demo_pay_rates VALUES (?, ?)",
                   (employee_id, 2200 + employee_id % 5 * 100))
        for number, day in enumerate(weekdays(period_start)):
            minutes = 360 if number == 4 and employee_id % 5 == 0 else 480
            db.execute("INSERT OR IGNORE INTO demo_attendance VALUES (?, ?, ?)", (employee_id, day, minutes))


def roster_agent(db, event):
    period = event["roster_start"]
    days = weekdays(period)
    existing = set((row[0], row[1]) for row in db.execute(
        "SELECT employee_id, work_date FROM work_roster WHERE work_date BETWEEN ? AND ?", (days[0], days[-1]),
    ))
    rows = [
        {"employee_id": r["employee_id"], "employee_code": r["employee_code"], "work_date": day, "minutes": 480}
        for r in active_employees(db) for day in days if (r["employee_id"], day) not in existing
    ]
    if not rows:
        return Assessment(f"The roster beginning {period} has no unpublished shifts.")
    payload = {"roster_start": period, "shifts": rows, "shift_count": len(rows)}
    return Assessment(f"Prepared {len(rows)} shifts for the week beginning {period}.", (
        Draft("publish_roster", period, f"Publish {len(rows)} demo work shifts for the week of {period}.", payload),
    ))


def payroll_agent(db, event):
    period = event["payroll_start"]
    if db.execute("SELECT 1 FROM payroll_ledger WHERE period_start = ?", (period,)).fetchone():
        return Assessment(f"Payroll for {period} is already posted.")
    days = weekdays(period)
    rates = dict(db.execute("SELECT employee_id, hourly_minor FROM demo_pay_rates").fetchall())
    attendance = {(row[0], row[1]): row[2] for row in db.execute(
        "SELECT employee_id, work_date, minutes FROM demo_attendance WHERE work_date BETWEEN ? AND ?",
        (days[0], days[-1]),
    )}
    rows = []
    for person in active_employees(db):
        employee_id = person["employee_id"]
        if employee_id not in rates or any((employee_id, day) not in attendance for day in days):
            return Assessment(f"Payroll for {period} needs complete time and rate inputs.")
        minutes = sum(attendance[employee_id, day] for day in days)
        rows.append({
            "employee_id": employee_id, "employee_code": person["employee_code"], "minutes": minutes,
            "hourly_minor": rates[employee_id], "gross_minor": (minutes * rates[employee_id] + 30) // 60,
        })
    if not rows:
        return Assessment(f"Payroll for {period} has no active employees.")
    total = sum(r["gross_minor"] for r in rows)
    payload = {"payroll_start": period, "employees": rows, "gross_minor": total, "currency": "USD"}
    return Assessment(f"Prepared a gross-pay draft for {len(rows)} employees.", (
        Draft("post_payroll", period,
              f"Post demo gross payroll of USD {total / 100:,.2f} for {len(rows)} employees.", payload),
    ))


def publish_roster(db, approval, payload):
    drafts = roster_agent(db, payload).drafts
    if not drafts or drafts[0].payload != payload:
        raise Blocked("The staff or work roster changed since this proposal.")
    db.executemany("INSERT INTO work_roster VALUES (?, ?, ?, ?)", (
        (r["employee_id"], r["work_date"], r["minutes"], approval["id"]) for r in payload["shifts"]
    ))
    return {"shifts_published": payload["shift_count"], "roster_start": payload["roster_start"]}


def post_payroll(db, approval, payload):
    drafts = payroll_agent(db, payload).drafts
    if not drafts or drafts[0].payload != payload:
        raise Blocked("The demo attendance, rates or ledger changed since this proposal.")
    db.executemany("INSERT INTO payroll_ledger VALUES (?, ?, ?, ?, ?)", (
        (payload["payroll_start"], r["employee_id"], r["minutes"], r["gross_minor"], approval["id"])
        for r in payload["employees"]
    ))
    return {"employees_posted": len(payload["employees"]), "gross_minor": payload["gross_minor"],
            "currency": "USD", "payout": "simulated"}

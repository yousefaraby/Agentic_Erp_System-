"""Inventory analytics and the manager's current Excel report."""

from collections import defaultdict
from contextlib import closing
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from math import ceil

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from storage import Assessment, encode


STOCK_SQL = """
    SELECT w.code AS warehouse, p.sku, p.unit, i.on_hand, i.reserved, i.on_hand - i.reserved AS available,
           i.warehouse_id, i.product_id,
           CAST(json_extract(s.payload, '$.reorder_point') AS INTEGER) AS reorder_point
    FROM inventory AS i
    JOIN warehouses AS w ON w.id = i.warehouse_id JOIN products AS p ON p.id = i.product_id
    LEFT JOIN source_data AS s ON s.table_name = 'inventory'
        AND json_extract(s.payload, '$.warehouse_id') = i.warehouse_id
        AND json_extract(s.payload, '$.product_id') = i.product_id
    ORDER BY w.code, p.sku
"""


def inventory_metrics(db):
    rows = [(r["warehouse"], r["available"], r["reorder_point"] or 0) for r in db.execute(STOCK_SQL)]
    counts = defaultdict(lambda: {"skus": 0, "at_reorder": 0, "out_of_stock": 0})
    for warehouse, available, reorder in rows:
        counts[warehouse]["skus"] += 1
        counts[warehouse]["at_reorder"] += available <= reorder
        counts[warehouse]["out_of_stock"] += available == 0
    return [{"warehouse": key, **value} for key, value in sorted(counts.items())]


def analytics_agent(db, event):
    metrics = inventory_metrics(db)
    counts = ", ".join(f"{r['warehouse']} {r['at_reorder']}/{r['skus']} at reorder, "
                       f"{r['out_of_stock']} out" for r in metrics)
    summary = f"Inventory scan. {counts}."
    return Assessment(summary)


def low_stock_rows(db):
    return [
        dict(row) for row in db.execute(STOCK_SQL)
        if row["reorder_point"] is not None and row["available"] <= row["reorder_point"]
    ]


DECISIONS_SHEET = "Manager Approves"
DECISIONS_HEADER = ("ID", "Agent", "Proposed action", "Manager decision", "Action key")
DECISIONS = {"Approve": "approve", "Cancel": "reject"}


def action_key(workbook_id, row):
    return sha256(encode([workbook_id, row["id"], row["event_key"], row["kind"], row["payload"]]).encode()).hexdigest()


def workbook_decisions(content):
    if content is None:
        return []
    with closing(load_workbook(BytesIO(content), read_only=True)) as book:
        if DECISIONS_SHEET not in book.sheetnames:
            return []
        rows = book[DECISIONS_SHEET].iter_rows(min_row=5, max_col=5, values_only=True)
        if next(rows, None) != DECISIONS_HEADER:
            raise ValueError("The Manager Approves headers changed.")
        decisions = {}
        for action_id, _, _, choice, key in rows:
            if type(action_id) is not int or choice not in DECISIONS or not isinstance(key, str):
                continue
            decision = DECISIONS[choice]
            if action_id in decisions and decisions[action_id] != (decision, key):
                raise ValueError(f"Conflicting decisions for action {action_id}.")
            decisions[action_id] = (decision, key)
        return [(action_id, decision, key) for action_id, (decision, key) in decisions.items()]


def report_data(db):
    """Build the manager's decision queue, approval history, and email orders."""
    actions = db.execute("SELECT * FROM approvals ORDER BY id").fetchall()
    workbook = db.execute("SELECT value FROM company_meta WHERE key = 'manager_workbook'").fetchone()
    workbook_id = workbook[0] if workbook else ""
    pending = [
        [r["id"], r["agent"], r["summary"], None, action_key(workbook_id, r)]
        for r in actions if r["status"] == "pending" and r["decision"] is None
    ]
    states = {"pending": "Pending", "executed": "Executed", "rejected": "Cancelled", "blocked": "Blocked"}
    approvals = [
        [r["id"], r["agent"], r["target"], r["summary"],
         "Approved" if r["status"] == "pending" and r["decision"] == "approve" else states[r["status"]],
         {"approve": "Approve", "reject": "Cancel"}.get(r["decision"]), r["decided_at"]]
        for r in actions
    ]
    orders = [list(r) for r in db.execute("""
        SELECT so.order_number, b.code, c.name, so.status, so.total_minor / 100.0, so.currency, so.created_at
        FROM sales_orders AS so JOIN branches AS b ON b.id = so.branch_id
        JOIN customers AS c ON c.id = so.customer_id
        WHERE so.order_number LIKE 'SO-EMAIL-%' ORDER BY so.id
    """)]
    return {
        DECISIONS_SHEET: {
            "title": "Manager Approves", "note": "Choose Approve or Cancel, then save and close Excel to refresh the report.",
            "headers": list(DECISIONS_HEADER), "widths": [10, 20, 78, 23, 20], "rows": pending,
        },
        "Approvals": {
            "title": "Approvals", "note": "Decision and execution history. Times shown in UTC.",
            "headers": ["ID", "Agent", "Reference", "Proposed action", "Status", "Decision", "Decided at"],
            "widths": [10, 20, 25, 74, 17, 15, 23], "dates": {6: "yyyy-mm-dd hh:mm"}, "rows": approvals,
        },
        "Email Orders": {
            "title": "Email Orders", "note": "Orders received by email and their catalog totals. Times shown in UTC.",
            "headers": ["Order", "Branch", "Customer", "Status", "Total", "Currency", "Created at"],
            "widths": [22, 12, 31, 21, 15, 13, 23], "dates": {6: "yyyy-mm-dd hh:mm"},
            "numbers": {4: "#,##0.00"}, "rows": orders,
        },
    }


def export_excel(db, *, blank=False):
    """Build the manager workbook from a consistent database snapshot."""
    db.execute("BEGIN")
    try:
        data = report_data(db)
    finally:
        db.rollback()
    if blank:
        for table in data.values():
            table["rows"] = []
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, table in data.items():
        sheet = workbook.create_sheet(name)
        sheet.sheet_view.showGridLines = False
        sheet.sheet_properties.tabColor = "1D5D55" if name == DECISIONS_SHEET else "798F8B"
        sheet["A2"] = table["title"]
        sheet["A2"].font = Font(name="Arial", size=16, bold=True, color="153E3B")
        sheet.row_dimensions[2].height = 26
        sheet["A3"] = table["note"]
        sheet["A3"].font = Font(name="Arial", size=10, color="566962", italic=True)
        sheet.row_dimensions[3].height = 16
        for column, label in enumerate(table["headers"], 1):
            sheet.column_dimensions[get_column_letter(column)].width = table["widths"][column - 1]
            cell = sheet.cell(5, column, label)
            cell.font = Font(name="Arial", size=10, color="FFFFFF", bold=True)
            cell.fill = PatternFill("solid", fgColor="20554F")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.row_dimensions[5].height = 24
        sheet.freeze_panes = "A6"
        for index, row in enumerate(table["rows"], 6):
            height = 17
            for column, value in enumerate(row, 1):
                if value is None:
                    continue
                date_format = table.get("dates", {}).get(column - 1)
                cell = sheet.cell(index, column, datetime.fromisoformat(value) if date_format else value)
                if isinstance(value, str) and not date_format:
                    cell.data_type = "s"
                cell.font = Font(name="Arial", size=10, color="203631")
                width = table["widths"][column - 1]
                wrap = (isinstance(value, str) and not date_format
                        and table["headers"][column - 1] != "Action key" and len(value) > width + 8)
                cell.alignment = Alignment(vertical="center", wrap_text=wrap)
                if wrap:
                    height = max(height, 17 * ceil(len(value) / width))
                if index % 2 == 0:
                    cell.fill = PatternFill("solid", fgColor="F0F6F4")
                if fmt := date_format or table.get("numbers", {}).get(column - 1):
                    cell.number_format = fmt
                if value == "Pending" and table["headers"][column - 1] == "Status":
                    cell.fill = PatternFill("solid", fgColor="FCEBC9")
            sheet.row_dimensions[index].height = min(height, 130)
        sheet.auto_filter.ref = f"A5:{get_column_letter(len(table['headers']))}{sheet.max_row}"
        if name == DECISIONS_SHEET:
            sheet.column_dimensions["E"].hidden = True
            validation = DataValidation(type="list", formula1='"Approve,Cancel"', allow_blank=True,
                                        showErrorMessage=True, errorTitle="Choose a decision",
                                        error="Select Approve or Cancel from the list.")
            sheet.add_data_validation(validation)
            validation.add(f"D6:D{max(6, sheet.max_row)}")
            if table["rows"]:
                for cell, in sheet.iter_rows(min_row=6, min_col=4, max_col=4):
                    cell.fill = PatternFill("solid", fgColor="FCEBC9")
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()

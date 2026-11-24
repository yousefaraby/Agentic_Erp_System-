"""Sales agent: receive email, validate the attached order, and book it."""

from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from html import unescape
import imaplib
from io import BytesIO
import json
import os
import re
import ssl
from xml.parsers import expat
from zipfile import ZipFile

from openpyxl import load_workbook

from storage import Assessment, Draft, normalize
from .inventory import check_stock, reserve_stock


MAX_ATTACHMENT = 5 * 1024 * 1024
MAX_MESSAGE = 8 * 1024 * 1024
MAX_ROWS = 500

FIELDS = {
    "sku": {"sku", "item code", "product code", "كود", "كود الصنف", "كود المنتج"},
    "quantity": {"qty", "quantity", "الكمية", "كمية"},
    "unit": {"unit", "unit of measure", "الوحدة", "وحدة"},
    "branch": {"branch", "branch code", "الفرع", "كود الفرع"},
    "name": {
        "customer", "customer name", "company", "company name", "name",
        "اسم العميل", "اسم الشركة", "الاسم",
    },
}


class NeedsReview(ValueError):
    """The message needs a human to resolve an input problem."""


@dataclass(frozen=True)
class RequestedLine:
    sku: str
    quantity: int
    unit: str


@dataclass(frozen=True)
class OrderIntent:
    customer_name: str
    branch_code: str
    lines: tuple[RequestedLine, ...]


def field(value: object) -> str:
    label = normalize(value).replace("_", " ")
    return next((key for key, aliases in FIELDS.items() if label in aliases), "")


def quantity(value: object, row: int) -> int:
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise NeedsReview(f"Row {row}: quantity must be a positive whole number.") from None
    if not amount.is_finite() or not 1 <= amount <= 1_000_000 or amount != int(amount):
        raise NeedsReview(f"Row {row}: quantity must be a whole number from 1 to 1000000.")
    return int(amount)


def validate_workbook(payload: bytes) -> None:
    # Check XML before openpyxl parses the untrusted attachment.
    def reject_doctype(*_):
        raise NeedsReview("The attachment contains an unsupported XML declaration.")

    with ZipFile(BytesIO(payload)) as archive:
        members = archive.infolist()
        if len(members) > 200 or sum(part.file_size for part in members) > 20 * 1024 * 1024:
            raise NeedsReview("The Excel workbook is too large.")
        for part in members:
            if part.is_dir():
                continue
            parser = expat.ParserCreate()
            parser.StartDoctypeDeclHandler = reject_doctype
            try:
                with archive.open(part) as source:
                    while chunk := source.read(65536):
                        parser.Parse(chunk)
                parser.Parse(b"", True)
            except expat.ExpatError:
                if part.filename.lower().endswith((".xml", ".rels")):
                    raise


def read_order_sheet(sheet):
    sheet.reset_dimensions()
    metadata = []
    rows = sheet.iter_rows()
    for header_row, row in enumerate(rows, start=1):
        if header_row > 20:
            return None
        headers = [field(cell.value) for cell in row]
        if {"sku", "quantity"} <= set(headers):
            break
        if len(row) >= 2 and headers[0] in {"name", "branch"}:
            metadata.append((headers[0], row[1].value))
        elif header_row == 1 and row and isinstance(row[0].value, str) and not headers[0]:
            if all(cell.value is None for cell in row[1:]):
                metadata.append(("name", row[0].value))
    else:
        return None

    for key in ("sku", "quantity", "unit"):
        if headers.count(key) > 1:
            raise NeedsReview(f"The order sheet has more than one {key} column.")
    columns = {key: headers.index(key) for key in ("sku", "quantity", "unit") if key in headers}
    lines = Counter()
    for row_number, row in enumerate(rows, start=header_row + 1):
        if row_number > header_row + MAX_ROWS:
            raise NeedsReview(f"The order sheet exceeds {MAX_ROWS} rows below its header.")
        if all(cell.value is None for cell in row):
            continue
        cells = {key: row[index] for key, index in columns.items() if index < len(row)}
        if any(getattr(cell, "data_type", None) in {"f", "e"} for cell in cells.values()):
            raise NeedsReview(f"Row {row_number}: enter SKU, quantity and unit as values.")
        values = {key: cell.value for key, cell in cells.items()}
        sku = normalize(values.get("sku")).upper()
        if not sku or len(sku) > 100:
            raise NeedsReview(f"Row {row_number}: a valid SKU is required.")
        unit = normalize(values.get("unit")).upper()
        lines[sku, unit] += quantity(values.get("quantity"), row_number)
    if not lines:
        raise NeedsReview("The order sheet has no items.")
    return metadata, tuple(RequestedLine(sku, count, unit) for (sku, unit), count in lines.items())


def read_attachment(payload: bytes):
    if len(payload) > MAX_ATTACHMENT:
        raise NeedsReview("The Excel attachment exceeds 5 MiB.")
    try:
        validate_workbook(payload)
        with BytesIO(payload) as source:
            workbook = load_workbook(source, read_only=True, keep_links=False)
            try:
                sheets = []
                for sheet in workbook.worksheets:
                    if result := read_order_sheet(sheet):
                        sheets.append(result)
                if len(sheets) != 1:
                    raise NeedsReview("Include one sheet with SKU and Quantity columns.")
                return sheets[0]
            finally:
                workbook.close()
    except NeedsReview:
        raise
    except Exception as exc:
        raise NeedsReview("The Excel attachment could not be read.") from exc


def parse_message(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


def extract_order(message, default_branch: str) -> OrderIntent:
    parts = [
        part for part in message.iter_attachments()
        if (part.get_filename() or "").lower().endswith(".xlsx")
    ]
    if len(parts) != 1:
        raise NeedsReview("Attach one .xlsx order workbook.")
    metadata, lines = read_attachment(parts[0].get_payload(decode=True) or b"")
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        try:
            content = body.get_content()
        except (LookupError, UnicodeError) as exc:
            raise NeedsReview("The message text could not be decoded.") from exc
        if body.get_content_subtype() == "html":
            # Extract metadata from visible line breaks in simple HTML emails.
            content = unescape(re.sub(r"<[^>]*>", "", re.sub(
                r"<(?:br\b[^>]*|/(?:div|p|tr))>", "\n", content, flags=re.IGNORECASE
            )))
        for line in content.splitlines()[:20]:
            label, separator, value = line.partition(":")
            if separator and field(label) in {"name", "branch"}:
                metadata.append((field(label), value))
    names = {normalize(value): str(value).strip() for key, value in metadata
             if key == "name" and normalize(value)}
    branches = {normalize(value).upper() for key, value in metadata
                if key == "branch" and normalize(value)}
    if len(names) > 1 or len(branches) > 1:
        raise NeedsReview("The message and workbook contain conflicting names or branches.")
    branch = next(iter(branches), default_branch.strip().upper())
    if not branch:
        raise NeedsReview("Supply a branch code in the message or workbook.")
    return OrderIntent(next(iter(names.values()), ""), branch, lines)


def quote_order(db, sender, intent):
    customer = db.execute(
        "SELECT id, name FROM customers WHERE email = ? AND active = 1", (sender,),
    ).fetchone()
    if customer is None:
        raise NeedsReview("The sender does not match an active customer email.")
    if intent.customer_name and normalize(intent.customer_name) != normalize(customer["name"]):
        raise NeedsReview("The supplied name does not match the customer registered for the sender.")
    branch = db.execute(
        "SELECT id FROM branches WHERE code = ? AND active = 1", (intent.branch_code,),
    ).fetchone()
    if branch is None:
        raise NeedsReview(f"Unknown or inactive branch {intent.branch_code}.")

    lines = {}
    for line in intent.lines:
        product = db.execute("SELECT * FROM products WHERE sku = ? AND active = 1", (line.sku,)).fetchone()
        if product is None:
            raise NeedsReview(f"Unknown or inactive SKU {line.sku}.")
        if line.unit and line.unit != product["unit"]:
            raise NeedsReview(f"{line.sku} uses {product['unit']}, received {line.unit}.")
        _, previous = lines.get(line.sku, (product, 0))
        lines[line.sku] = (product, previous + line.quantity)
    return customer["id"], branch["id"], list(lines.values())


def create_order(db, sender, intent):
    customer_id, branch_id, lines = quote_order(db, sender, intent)
    allocations, shortages = check_stock(db, branch_id, lines)
    status = "awaiting_stock" if shortages else "reserved"
    total = sum(product["unit_price_minor"] * quantity for product, quantity in lines)
    order_id = db.execute("""
        INSERT INTO sales_orders (customer_id, branch_id, status, channel, total_minor, currency)
        VALUES (?, ?, ?, 'email', ?, 'USD')
    """, (customer_id, branch_id, status, total)).lastrowid
    number = f"SO-EMAIL-{order_id:06d}"
    db.execute("UPDATE sales_orders SET order_number = ? WHERE id = ?", (number, order_id))
    items = {}
    for product, quantity in lines:
        items[product["id"]] = db.execute("""
            INSERT INTO sales_order_items (order_id, product_id, quantity, unit_price_minor)
            VALUES (?, ?, ?, ?)
        """, (order_id, product["id"], quantity, product["unit_price_minor"])).lastrowid
    reserved = [] if shortages else reserve_stock(db, order_id, lines, items, allocations)
    return {
        "status": status, "order_number": number, "total_minor": total, "currency": "USD",
        "shortages": shortages, "reserved": reserved,
    }


def process_message(db, raw, source, default_branch):
    sender, intent = "", None
    if len(raw) > MAX_MESSAGE:
        message_key = "oversize:" + source
        error = "The message exceeds 8 MiB."
    else:
        message = parse_message(raw)
        from_header = message["From"]
        addresses = getattr(from_header, "addresses", ())
        if len(addresses) == 1:
            sender = normalize(addresses[0].addr_spec)
        message_id = str(message.get("Message-ID", "")).strip()
        message_key = "mail:" + sender + ":" + message_id if message_id else sha256(raw).hexdigest()
        error = ""
        try:
            if not sender or len(message.get_all("From", [])) != 1:
                raise NeedsReview("The message needs a single sender email.")
            intent = extract_order(message, default_branch)
        except NeedsReview as exc:
            error = str(exc)

    with db:
        # Serialize the availability check with other inventory writes.
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT result FROM email_intake WHERE message_key = ?", (message_key,)).fetchone()
        if previous:
            return {**json.loads(previous["result"]), "duplicate": True}
        if not error:
            try:
                result = create_order(db, sender, intent)
            except NeedsReview as exc:
                error = str(exc)
        if error:
            result = {"status": "needs_review", "reason": error}
        db.execute("""
            INSERT INTO email_intake (message_key, source, sender, intent, result)
            VALUES (?, ?, ?, ?, ?)
        """, (message_key, source, sender, json.dumps(asdict(intent)) if intent else None, json.dumps(result)))
        return result


def report(result):
    print(json.dumps(result), flush=True)


def fetch_new_messages(db, mail, mailbox, folder, default_branch):
    status, _ = mail.select(folder, readonly=True)
    if status != "OK":
        raise imaplib.IMAP4.error("Could not select the order folder.")
    _, values = mail.response("UIDVALIDITY")
    if not values or not values[-1]:
        raise imaplib.IMAP4.error("The folder did not provide UIDVALIDITY.")
    uidvalidity = int(values[-1])
    cursor = db.execute(
        "SELECT uid FROM inbox_cursor WHERE mailbox = ? AND uidvalidity = ?", (mailbox, uidvalidity),
    ).fetchone()
    last_uid = cursor["uid"] if cursor else 0
    status, values = mail.uid("SEARCH", None, "UID", f"{last_uid + 1}:*")
    if status != "OK":
        raise imaplib.IMAP4.error("Could not list incoming messages.")
    uids = sorted(int(uid) for uid in (values[0] or b"").split())
    for uid in [uid for uid in uids if uid > last_uid][:100]:
        status, parts = mail.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{MAX_MESSAGE + 1}>)")
        raw = next((part[1] for part in (parts or ()) if isinstance(part, tuple)), None)
        if status != "OK" or raw is None:
            raise imaplib.IMAP4.error(f"Could not read message UID {uid}.")
        source = f"{mailbox}:{uidvalidity}:{uid}"
        result = process_message(db, raw, source, default_branch)
        with db:
            db.execute("""
                INSERT INTO inbox_cursor VALUES (?, ?, ?)
                ON CONFLICT (mailbox, uidvalidity) DO UPDATE SET uid = max(uid, excluded.uid)
            """, (mailbox, uidvalidity, uid))
        report({"source": source, **result})


def poll_imap(db, default_branch):
    variables = ("ORDER_IMAP_HOST", "ORDER_IMAP_USER", "ORDER_IMAP_PASSWORD", "ORDER_IMAP_FOLDER")
    missing = [name for name in variables if not os.environ.get(name)]
    if missing:
        raise ValueError("Set " + ", ".join(missing) + " before using IMAP.")
    host, user, password, folder = (os.environ[name] for name in variables)
    port = int(os.getenv("ORDER_IMAP_PORT", "993"))
    mailbox = f"{host}:{port}/{user}/{folder}"
    # Quote account and folder strings per IMAP's quoted-string grammar.
    def quoted(value):
        if "\r" in value or "\n" in value or "\0" in value:
            raise ValueError("Invalid control character in IMAP configuration.")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    username, selected_folder = quoted(user), quoted(folder)
    with imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30) as mail:
        mail.login(username, password)
        fetch_new_messages(db, mail, mailbox, selected_folder, default_branch)


def sales_agent(db, event):
    result = event["intake"]
    if result["status"] == "needs_review":
        sender = event["sender"]
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", sender):
            draft = Draft("request_order_details", sender, "Ask the sender for corrected order details.", {
                "recipient": sender, "channel": "email_demo", "reason": result["reason"],
                "message": "Please resend the order from your registered email with your company or "
                           "customer name, branch code and an Excel list of SKU and quantity.",
            })
        else:
            draft = Draft("review_intake_exception", "inbox", "Review an order with no usable reply address.", {
                "reason": result["reason"],
            })
        return Assessment(result["reason"], (draft,))
    return Assessment(f"{result['order_number']} recorded as {result['status']}.")


def process_folder(db, folder, default_branch, seen):
    for path in sorted(folder.glob("*.eml")):
        try:
            before = path.stat()
            version = before.st_mtime_ns, before.st_size
            if seen.get(path) == version:
                continue
            with path.open("rb") as stream:
                raw = stream.read(MAX_MESSAGE + 1)
            after = path.stat()
        except (FileNotFoundError, PermissionError):
            continue
        if (after.st_mtime_ns, after.st_size) != version:
            continue
        process_message(db, raw, f"inbox/{path.name}", default_branch)
        seen[path] = version

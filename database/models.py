"""SQLAlchemy ORM mappings resolved from the migrated database.
Call prepare_models(engine) once at startup. Reflection preserves every SQL
constraint and type without maintaining a second, divergent schema definition.
"""
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.declarative import DeferredReflection
class Base(DeclarativeBase):
    pass
class Reflected(DeferredReflection, Base):
    __abstract__ = True

class Branches(Reflected):
    __tablename__ = "branches"
    __table_args__ = {"schema": "erp"}

class Warehouses(Reflected):
    __tablename__ = "warehouses"
    __table_args__ = {"schema": "erp"}

class BranchWarehouseServices(Reflected):
    __tablename__ = "branch_warehouse_services"
    __table_args__ = {"schema": "erp"}

class Departments(Reflected):
    __tablename__ = "departments"
    __table_args__ = {"schema": "erp"}

class Employees(Reflected):
    __tablename__ = "employees"
    __table_args__ = {"schema": "erp"}

class Roles(Reflected):
    __tablename__ = "roles"
    __table_args__ = {"schema": "erp"}

class Permissions(Reflected):
    __tablename__ = "permissions"
    __table_args__ = {"schema": "erp"}

class RolePermissions(Reflected):
    __tablename__ = "role_permissions"
    __table_args__ = {"schema": "erp"}

class Users(Reflected):
    __tablename__ = "users"
    __table_args__ = {"schema": "erp"}

class ProductCategories(Reflected):
    __tablename__ = "product_categories"
    __table_args__ = {"schema": "erp"}

class Products(Reflected):
    __tablename__ = "products"
    __table_args__ = {"schema": "erp"}

class Suppliers(Reflected):
    __tablename__ = "suppliers"
    __table_args__ = {"schema": "erp"}

class SupplierProducts(Reflected):
    __tablename__ = "supplier_products"
    __table_args__ = {"schema": "erp"}

class Customers(Reflected):
    __tablename__ = "customers"
    __table_args__ = {"schema": "erp"}

class SalesOrders(Reflected):
    __tablename__ = "sales_orders"
    __table_args__ = {"schema": "erp"}

class SalesOrderItems(Reflected):
    __tablename__ = "sales_order_items"
    __table_args__ = {"schema": "erp"}

class Payments(Reflected):
    __tablename__ = "payments"
    __table_args__ = {"schema": "erp"}

class Shipments(Reflected):
    __tablename__ = "shipments"
    __table_args__ = {"schema": "erp"}

class Returns(Reflected):
    __tablename__ = "returns"
    __table_args__ = {"schema": "erp"}

class ReturnItems(Reflected):
    __tablename__ = "return_items"
    __table_args__ = {"schema": "erp"}

class PaymentRefunds(Reflected):
    __tablename__ = "payment_refunds"
    __table_args__ = {"schema": "erp"}

class Inventory(Reflected):
    __tablename__ = "inventory"
    __table_args__ = {"schema": "erp"}

class InventoryReservations(Reflected):
    __tablename__ = "inventory_reservations"
    __table_args__ = {"schema": "erp"}

class InventoryReservationItems(Reflected):
    __tablename__ = "inventory_reservation_items"
    __table_args__ = {"schema": "erp"}

class StockMovements(Reflected):
    __tablename__ = "stock_movements"
    __table_args__ = {"schema": "erp"}

class StockTransfers(Reflected):
    __tablename__ = "stock_transfers"
    __table_args__ = {"schema": "erp"}

class StockTransferItems(Reflected):
    __tablename__ = "stock_transfer_items"
    __table_args__ = {"schema": "erp"}

class PurchaseOrders(Reflected):
    __tablename__ = "purchase_orders"
    __table_args__ = {"schema": "erp"}

class PurchaseOrderItems(Reflected):
    __tablename__ = "purchase_order_items"
    __table_args__ = {"schema": "erp"}

class GoodsReceipts(Reflected):
    __tablename__ = "goods_receipts"
    __table_args__ = {"schema": "erp"}

class GoodsReceiptItems(Reflected):
    __tablename__ = "goods_receipt_items"
    __table_args__ = {"schema": "erp"}

def prepare_models(engine):
    Reflected.prepare(engine)

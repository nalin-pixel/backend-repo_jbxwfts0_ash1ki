"""
Database Schemas for the Field Stock App

Each Pydantic model represents a collection in MongoDB. The collection name is the lowercase of the class name.
"""
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, EmailStr

# Users of the system: technicians and office staff
class User(BaseModel):
    name: str = Field(..., description="Full name")
    email: EmailStr = Field(..., description="Unique email address")
    role: Literal["technician", "office"] = Field("technician", description="User role")
    password_hash: str = Field(..., description="BCrypt hash of the password")
    phone: Optional[str] = Field(None, description="Phone number")
    is_active: bool = Field(True, description="Active status")

# Master catalog of items coming from supplier (mijninstallatiepartner)
class InventoryItem(BaseModel):
    sku: str = Field(..., description="Supplier SKU / article number")
    name: str = Field(..., description="Item name")
    description: Optional[str] = Field(None, description="Item description")
    unit: Optional[str] = Field(None, description="Unit of measure, e.g. pcs")
    supplier: str = Field("mijninstallatiepartner", description="Supplier identifier")
    price: Optional[float] = Field(None, ge=0, description="Unit price (optional)")
    active: bool = Field(True, description="Whether the item is active")

# Per-technician stock records
class TechnicianStock(BaseModel):
    user_id: str = Field(..., description="Reference to user (technician)")
    sku: str = Field(..., description="SKU of the item")
    quantity: int = Field(0, ge=0, description="Quantity assigned to the technician")

# Work orders (bonnen) minimal model to link to OptimoRoute
class WorkOrder(BaseModel):
    order_id: str = Field(..., description="Local work order id/reference")
    technician_id: str = Field(..., description="Assigned technician user id")
    status: Literal["open", "completed"] = Field("open")
    external_ref: Optional[str] = Field(None, description="External/OptimoRoute reference if applicable")

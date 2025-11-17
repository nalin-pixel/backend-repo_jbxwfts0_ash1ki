import os
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

import requests
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from bson import ObjectId
from bs4 import BeautifulSoup

from database import db, create_document, get_documents
from schemas import User as UserSchema, InventoryItem as InventoryItemSchema, TechnicianStock as TechnicianStockSchema, WorkOrder as WorkOrderSchema

# Environment
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

# Auth setup
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Helpers
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class UserPublic(BaseModel):
    id: str
    name: str
    email: str
    role: str


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def user_collection():
    return db["user"]


def to_public_user(doc: Dict[str, Any]) -> UserPublic:
    return UserPublic(
        id=str(doc["_id"]),
        name=doc.get("name"),
        email=doc.get("email"),
        role=doc.get("role", "technician"),
    )


def authenticate_user(email: str, password: str):
    user = user_collection().find_one({"email": email})
    if not user:
        return None
    if not verify_password(password, user.get("password_hash", "")):
        return None
    return user


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt


async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(status_code=401, detail="Could not validate credentials")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except Exception:
        raise credentials_exception
    user = user_collection().find_one({"_id": ObjectId(user_id)})
    if user is None:
        raise credentials_exception
    return user


# Auth routes
@app.post("/auth/register", response_model=UserPublic)
def register(user: UserSchema):
    # email unique check
    if user_collection().find_one({"email": user.email}):
        raise HTTPException(status_code=400, detail="Email already registered")
    data = user.model_dump()
    data["password_hash"] = get_password_hash(data.pop("password_hash"))
    user_id = create_document("user", data)
    created = user_collection().find_one({"_id": ObjectId(user_id)})
    return to_public_user(created)


@app.post("/auth/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    token = create_access_token({"sub": str(user["_id"])})
    return Token(access_token=token)


@app.get("/auth/me", response_model=UserPublic)
def me(current_user=Depends(get_current_user)):
    return to_public_user(current_user)


# Inventory sync from mijninstallatiepartner (experimental scraper)
class SupplierScrapeParams(BaseModel):
    url: str
    max_items: int = 200


def parse_products_from_html(html: str):
    soup = BeautifulSoup(html, 'lxml')
    items = []
    # Generic selectors; may need adjustment with real site structure
    for card in soup.select('[class*="product" i]'):  # tries to catch typical product cards
        name = card.get_text(strip=True)[:200]
        sku = None
        # Heuristic: look for data attributes or text pieces with 'SKU' or 'Artikel'
        for attr in ["data-sku", "data-article", "data-code"]:
            if card.has_attr(attr):
                sku = card[attr]
                break
        if not sku:
            txt = card.get_text(" ", strip=True)
            for key in ["SKU", "Artikel", "Art.", "Code"]:
                if key.lower() in txt.lower():
                    # naive split
                    parts = txt.split(key)
                    if len(parts) > 1:
                        sku = parts[1].split()[0][:64]
                        break
        if name and sku:
            items.append({"sku": sku, "name": name})
    return items


@app.post("/inventory/scrape")
def inventory_scrape(params: SupplierScrapeParams, current_user=Depends(get_current_user)):
    if current_user.get("role") != "office":
        raise HTTPException(status_code=403, detail="Only office can sync inventory")
    try:
        r = requests.get(params.url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail="Cannot fetch supplier page")
        products = parse_products_from_html(r.text)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to parse supplier page")

    upserted = 0
    for item in products[: params.max_items]:
        db["inventoryitem"].update_one({"sku": item["sku"]}, {"$set": {
            "sku": item["sku"],
            "name": item["name"],
            "supplier": "mijninstallatiepartner",
            "active": True,
            "updated_at": datetime.now(timezone.utc)
        }}, upsert=True)
        upserted += 1
    return {"status": "ok", "upserted": upserted}


# Technician stock endpoints
class StockUpdate(BaseModel):
    sku: str
    quantity: int


@app.get("/stock/mine")
def my_stock(current_user=Depends(get_current_user)):
    if current_user.get("role") != "technician":
        raise HTTPException(status_code=403, detail="Only technicians have personal stock")
    items = list(db["technicianstock"].find({"user_id": str(current_user["_id"]) }))
    for it in items:
        it["id"] = str(it.pop("_id"))
    return items


@app.post("/stock/update")
def update_stock(update: StockUpdate, current_user=Depends(get_current_user)):
    if current_user.get("role") != "technician":
        raise HTTPException(status_code=403, detail="Only technicians can update their stock")
    db["technicianstock"].update_one(
        {"user_id": str(current_user["_id"]), "sku": update.sku},
        {"$set": {"quantity": max(0, int(update.quantity)), "updated_at": datetime.now(timezone.utc)}},
        upsert=True
    )
    return {"status": "ok"}


@app.get("/stock/overview")
def stock_overview(current_user=Depends(get_current_user)):
    if current_user.get("role") != "office":
        raise HTTPException(status_code=403, detail="Only office can view all technicians' stock")
    rows = list(db["technicianstock"].find())
    result = []
    for r in rows:
        result.append({
            "id": str(r.get("_id")),
            "user_id": r.get("user_id"),
            "sku": r.get("sku"),
            "quantity": r.get("quantity", 0)
        })
    return result


# Work order (bonnen) completion via OptimoRoute webhook
class RouteCompletion(BaseModel):
    order_id: str
    technician_email: Optional[str] = None
    status: str
    completed_at: Optional[str] = None


@app.post("/optimoroute/webhook")
def optimoroute_webhook(payload: RouteCompletion):
    db["workorder"].update_one(
        {"order_id": payload.order_id},
        {"$set": {"status": "completed", "completed_at": payload.completed_at or datetime.now(timezone.utc).isoformat()}},
        upsert=True
    )
    return {"status": "ok"}


@app.get("/")
def read_root():
    return {"message": "Field Stock API running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
                response["connection_status"] = "Connected"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

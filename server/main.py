import os
import sys
import shutil
import sqlite3
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime, Date, ForeignKey, Text, Boolean, and_
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from pydantic import BaseModel, Field
from dateutil.parser import parse as dateparse

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "kitchen.db"
BACKUP_DIR = BASE_DIR / "backups"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite:///{DB_PATH}"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ========== SQLAlchemy 模型 ==========

class Ingredient(Base):
    __tablename__ = "ingredients"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    category = Column(String(50), default="其他")
    unit = Column(String(20), default="kg")
    safety_stock_days = Column(Integer, default=3)
    shelf_life_days = Column(Integer, default=30)
    unit_cost = Column(Float, default=0.0)
    barcode = Column(String(50), unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class Stock(Base):
    __tablename__ = "stock"
    id = Column(Integer, primary_key=True, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), unique=True)
    current_qty = Column(Float, default=0.0)
    last_inbound_at = Column(DateTime, nullable=True)
    last_outbound_at = Column(DateTime, nullable=True)
    last_unit_price = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    ingredient = relationship("Ingredient")


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(String(20), nullable=False)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    order_qty = Column(Float, nullable=False)
    received_qty = Column(Float, default=0.0)
    order_date = Column(Date, default=date.today)
    status = Column(String(20), default="pending")
    unit_price = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.now)
    confirmed_at = Column(DateTime, nullable=True)
    ingredient = relationship("Ingredient")


class Waste(Base):
    __tablename__ = "waste"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(String(20), default="central")
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    waste_qty = Column(Float, nullable=False)
    waste_reason = Column(String(20), default="其他")
    unit_price = Column(Float, default=0.0)
    waste_amount = Column(Float, default=0.0)
    waste_date = Column(Date, default=date.today)
    created_at = Column(DateTime, default=datetime.now)
    note = Column(Text, default="")
    ingredient = relationship("Ingredient")


class Outbound(Base):
    __tablename__ = "outbound"
    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(String(20), default="central")
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    qty = Column(Float, nullable=False)
    outbound_date = Column(Date, default=date.today)
    created_at = Column(DateTime, default=datetime.now)
    operator = Column(String(50), default="system")
    note = Column(Text, default="")
    ingredient = relationship("Ingredient")


class ForecastLog(Base):
    __tablename__ = "forecast_log"
    id = Column(Integer, primary_key=True, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    forecast_date = Column(Date, nullable=False)
    forecast_qty = Column(Float, nullable=False)
    actual_qty = Column(Float, nullable=True)
    error_rate = Column(Float, nullable=True)
    is_flagged = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    ingredient = relationship("Ingredient")


class Promotion(Base):
    __tablename__ = "promotions"
    id = Column(Integer, primary_key=True, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    promo_date = Column(Date, nullable=False)
    multiplier = Column(Float, default=1.5)
    note = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)
    ingredient = relationship("Ingredient")


class Stocktake(Base):
    __tablename__ = "stocktake"
    id = Column(Integer, primary_key=True, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    store_id = Column(String(20), default="central")
    system_qty = Column(Float, nullable=False)
    actual_qty = Column(Float, nullable=False)
    diff_qty = Column(Float, default=0.0)
    diff_rate = Column(Float, default=0.0)
    stocktake_date = Column(Date, default=date.today)
    created_at = Column(DateTime, default=datetime.now)
    operator = Column(String(50), default="")
    note = Column(Text, default="")
    ingredient = relationship("Ingredient")


Base.metadata.create_all(bind=engine)


# ========== Pydantic 模型 ==========

class IngredientCreate(BaseModel):
    name: str
    category: str = "其他"
    unit: str = "kg"
    safety_stock_days: int = 3
    shelf_life_days: int = 30
    unit_cost: float = 0.0
    barcode: Optional[str] = None


class OrderCreate(BaseModel):
    store_id: str
    ingredient_id: int
    order_qty: float
    order_date: Optional[str] = None


class InboundConfirm(BaseModel):
    order_id: int
    received_qty: float
    unit_price: float


class OutboundCreate(BaseModel):
    ingredient_id: int
    qty: float
    store_id: str = "central"
    operator: str = "system"
    outbound_date: Optional[str] = None
    note: str = ""


class WasteCreate(BaseModel):
    ingredient_id: int
    waste_qty: float
    waste_reason: str = "其他"
    store_id: str = "central"
    waste_date: Optional[str] = None
    note: str = ""


class PromotionCreate(BaseModel):
    ingredient_id: int
    promo_date: str
    multiplier: float = 1.5
    note: str = ""


class StocktakeItem(BaseModel):
    ingredient_id: int
    actual_qty: float


class StocktakeCreate(BaseModel):
    items: List[StocktakeItem]
    store_id: str = "central"
    operator: str = ""
    quick_mode: bool = False


# ========== FastAPI 应用 ==========

app = FastAPI(
    title="餐饮后厨原料库存与智能补货预警系统",
    description="中央厨房库存管理 + 智能补货 + 损耗分析",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def backup_database():
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = BACKUP_DIR / f"kitchen_backup_{ts}.db"
        shutil.copy2(DB_PATH, backup_file)
        old_backups = sorted(BACKUP_DIR.glob("kitchen_backup_*.db"), key=lambda p: p.stat().st_mtime)
        for old in old_backups[:-30]:
            old.unlink()
        return str(backup_file)
    except Exception as e:
        return f"备份失败: {str(e)}"


# ========== 原料管理 ==========

@app.post("/api/ingredients", tags=["原料管理"])
def create_ingredient(data: IngredientCreate, db: Session = Depends(get_db)):
    ing = Ingredient(**data.model_dump())
    db.add(ing)
    db.commit()
    db.refresh(ing)
    stk = Stock(ingredient_id=ing.id, current_qty=0.0)
    db.add(stk)
    db.commit()
    return {"code": 0, "data": {"id": ing.id, "name": ing.name}}


@app.get("/api/ingredients", tags=["原料管理"])
def list_ingredients(
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    db: Session = Depends(get_db)
):
    q = db.query(Ingredient)
    if category:
        q = q.filter(Ingredient.category == category)
    if keyword:
        q = q.filter(Ingredient.name.like(f"%{keyword}%"))
    items = q.order_by(Ingredient.id.desc()).all()
    return {"code": 0, "data": [
        {
            "id": i.id, "name": i.name, "category": i.category,
            "unit": i.unit, "safety_stock_days": i.safety_stock_days,
            "shelf_life_days": i.shelf_life_days, "unit_cost": i.unit_cost,
            "barcode": i.barcode
        } for i in items
    ]}


@app.get("/api/ingredients/{ing_id}", tags=["原料管理"])
def get_ingredient(ing_id: int, db: Session = Depends(get_db)):
    i = db.query(Ingredient).filter(Ingredient.id == ing_id).first()
    if not i:
        raise HTTPException(404, "原料不存在")
    return {"code": 0, "data": {
        "id": i.id, "name": i.name, "category": i.category, "unit": i.unit,
        "safety_stock_days": i.safety_stock_days, "shelf_life_days": i.shelf_life_days,
        "unit_cost": i.unit_cost, "barcode": i.barcode
    }}


@app.get("/api/ingredients/by-barcode/{barcode}", tags=["原料管理"])
def get_ingredient_by_barcode(barcode: str, db: Session = Depends(get_db)):
    i = db.query(Ingredient).filter(Ingredient.barcode == barcode).first()
    if not i:
        raise HTTPException(404, "条码不存在")
    return {"code": 0, "data": {
        "id": i.id, "name": i.name, "category": i.category, "unit": i.unit,
        "unit_cost": i.unit_cost
    }}


# ========== 库存查询 ==========

@app.get("/api/stock", tags=["库存管理"])
def list_stock(
    low_only: bool = False,
    category: Optional[str] = None,
    db: Session = Depends(get_db)
):
    q = db.query(Stock).join(Ingredient)
    if category:
        q = q.filter(Ingredient.category == category)
    results = []
    for s in q.all():
        data = {
            "ingredient_id": s.ingredient_id,
            "name": s.ingredient.name,
            "category": s.ingredient.category,
            "unit": s.ingredient.unit,
            "current_qty": s.current_qty,
            "safety_stock_days": s.ingredient.safety_stock_days,
            "last_unit_price": s.last_unit_price,
            "last_inbound_at": s.last_inbound_at.isoformat() if s.last_inbound_at else None,
            "last_outbound_at": s.last_outbound_at.isoformat() if s.last_outbound_at else None,
        }
        if low_only:
            seven_days_ago = date.today() - timedelta(days=7)
            consumed = db.query(Outbound).filter(
                Outbound.ingredient_id == s.ingredient_id,
                Outbound.outbound_date >= seven_days_ago
            ).all()
            total = sum(c.qty for c in consumed)
            daily_avg = total / 7.0 if total > 0 else 0
            safety_qty = daily_avg * s.ingredient.safety_stock_days
            if s.current_qty < safety_qty:
                results.append({**data, "daily_avg": round(daily_avg, 3), "safety_qty": round(safety_qty, 3)})
        else:
            results.append(data)
    return {"code": 0, "data": results}


# ========== 订货管理 ==========

@app.post("/api/orders", tags=["订货管理"])
def create_order(data: OrderCreate, db: Session = Depends(get_db)):
    ing = db.query(Ingredient).filter(Ingredient.id == data.ingredient_id).first()
    if not ing:
        raise HTTPException(400, "原料不存在")
    od = date.today()
    if data.order_date:
        od = dateparse(data.order_date).date()
    order = Order(
        store_id=data.store_id,
        ingredient_id=data.ingredient_id,
        order_qty=data.order_qty,
        order_date=od
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return {"code": 0, "data": {"order_id": order.id, "status": order.status}}


@app.get("/api/orders", tags=["订货管理"])
def list_orders(
    store_id: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db)
):
    q = db.query(Order)
    if store_id:
        q = q.filter(Order.store_id == store_id)
    if status:
        q = q.filter(Order.status == status)
    if date_from:
        q = q.filter(Order.order_date >= dateparse(date_from).date())
    if date_to:
        q = q.filter(Order.order_date <= dateparse(date_to).date())
    items = q.order_by(Order.id.desc()).all()
    return {"code": 0, "data": [
        {
            "id": o.id, "store_id": o.store_id,
            "ingredient_id": o.ingredient_id,
            "ingredient_name": o.ingredient.name if o.ingredient else "",
            "order_qty": o.order_qty, "received_qty": o.received_qty,
            "pending_qty": round(o.order_qty - o.received_qty, 3),
            "order_date": o.order_date.isoformat(),
            "status": o.status, "unit_price": o.unit_price
        } for o in items
    ]}


@app.post("/api/orders/confirm", tags=["订货管理"])
def confirm_order(data: InboundConfirm, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == data.order_id).first()
    if not order:
        raise HTTPException(404, "订单不存在")
    if order.status == "completed":
        raise HTTPException(400, "订单已完成，不可重复收货")
    if data.received_qty <= 0:
        raise HTTPException(400, "到货量必须大于0")

    pending_qty = round(order.order_qty - order.received_qty, 3)
    if data.received_qty > pending_qty:
        raise HTTPException(
            400,
            f"本次到货量({data.received_qty})超过剩余待收货量({pending_qty})，"
            f"原订货量={order.order_qty}，已到货={order.received_qty}，"
            f"本次最多可收={pending_qty}"
        )

    order.received_qty = round(order.received_qty + data.received_qty, 3)
    order.unit_price = data.unit_price
    order.confirmed_at = datetime.now()
    if abs(order.received_qty - order.order_qty) < 0.001:
        order.status = "completed"
    else:
        order.status = "partial"

    stock = db.query(Stock).filter(Stock.ingredient_id == order.ingredient_id).first()
    if not stock:
        stock = Stock(ingredient_id=order.ingredient_id, current_qty=0.0)
        db.add(stock)
    stock.current_qty = round(stock.current_qty + data.received_qty, 3)
    stock.last_inbound_at = datetime.now()
    stock.last_unit_price = data.unit_price

    db.commit()
    return {"code": 0, "data": {
        "order_id": order.id,
        "status": order.status,
        "order_qty": order.order_qty,
        "total_received": order.received_qty,
        "remaining_pending": round(order.order_qty - order.received_qty, 3),
        "current_stock": stock.current_qty
    }}


# ========== 出库管理 ==========

@app.post("/api/outbound", tags=["出库管理"])
def create_outbound(data: OutboundCreate, db: Session = Depends(get_db)):
    ing = db.query(Ingredient).filter(Ingredient.id == data.ingredient_id).first()
    if not ing:
        raise HTTPException(400, "原料不存在")
    stock = db.query(Stock).filter(Stock.ingredient_id == data.ingredient_id).first()
    if not stock or stock.current_qty < data.qty:
        raise HTTPException(400, f"库存不足，当前库存: {stock.current_qty if stock else 0} {ing.unit}")

    od = date.today()
    if data.outbound_date:
        od = dateparse(data.outbound_date).date()

    ob = Outbound(
        store_id=data.store_id,
        ingredient_id=data.ingredient_id,
        qty=data.qty,
        outbound_date=od,
        operator=data.operator,
        note=data.note
    )
    db.add(ob)

    stock.current_qty -= data.qty
    stock.last_outbound_at = datetime.now()

    today = date.today()
    fl = db.query(ForecastLog).filter(
        ForecastLog.ingredient_id == data.ingredient_id,
        ForecastLog.forecast_date == od
    ).first()
    if fl:
        actual_so_far = db.query(Outbound).filter(
            Outbound.ingredient_id == data.ingredient_id,
            Outbound.outbound_date == od
        ).all()
        total_actual = sum(a.qty for a in actual_so_far)
        fl.actual_qty = total_actual
        if fl.forecast_qty > 0:
            fl.error_rate = abs(total_actual - fl.forecast_qty) / fl.forecast_qty
            if fl.error_rate > 0.30:
                fl.is_flagged = True

    db.commit()
    return {"code": 0, "data": {
        "outbound_id": ob.id,
        "current_stock": stock.current_qty
    }}


@app.get("/api/outbound", tags=["出库管理"])
def list_outbound(
    ingredient_id: Optional[int] = None,
    store_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db)
):
    q = db.query(Outbound)
    if ingredient_id:
        q = q.filter(Outbound.ingredient_id == ingredient_id)
    if store_id:
        q = q.filter(Outbound.store_id == store_id)
    if date_from:
        q = q.filter(Outbound.outbound_date >= dateparse(date_from).date())
    if date_to:
        q = q.filter(Outbound.outbound_date <= dateparse(date_to).date())
    items = q.order_by(Outbound.id.desc()).all()
    return {"code": 0, "data": [
        {
            "id": o.id, "ingredient_id": o.ingredient_id,
            "ingredient_name": o.ingredient.name if o.ingredient else "",
            "qty": o.qty, "unit": o.ingredient.unit if o.ingredient else "",
            "store_id": o.store_id, "outbound_date": o.outbound_date.isoformat(),
            "operator": o.operator, "note": o.note
        } for o in items
    ]}


# ========== 损耗管理 ==========

@app.post("/api/waste", tags=["损耗管理"])
def create_waste(data: WasteCreate, db: Session = Depends(get_db)):
    ing = db.query(Ingredient).filter(Ingredient.id == data.ingredient_id).first()
    if not ing:
        raise HTTPException(400, "原料不存在")
    stock = db.query(Stock).filter(Stock.ingredient_id == data.ingredient_id).first()
    if not stock or stock.current_qty < data.waste_qty:
        raise HTTPException(400, f"库存不足，无法登记损耗，当前库存: {stock.current_qty if stock else 0}")

    wd = date.today()
    if data.waste_date:
        wd = dateparse(data.waste_date).date()

    unit_price = stock.last_unit_price if stock.last_unit_price > 0 else ing.unit_cost
    waste_amount = round(unit_price * data.waste_qty, 2)

    w = Waste(
        store_id=data.store_id,
        ingredient_id=data.ingredient_id,
        waste_qty=data.waste_qty,
        waste_reason=data.waste_reason,
        unit_price=unit_price,
        waste_amount=waste_amount,
        waste_date=wd,
        note=data.note
    )
    db.add(w)
    stock.current_qty -= data.waste_qty
    db.commit()
    db.refresh(w)
    return {"code": 0, "data": {
        "waste_id": w.id,
        "waste_amount": waste_amount,
        "current_stock": stock.current_qty
    }}


@app.get("/api/waste", tags=["损耗管理"])
def list_waste(
    ingredient_id: Optional[int] = None,
    store_id: Optional[str] = None,
    reason: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db)
):
    q = db.query(Waste)
    if ingredient_id:
        q = q.filter(Waste.ingredient_id == ingredient_id)
    if store_id:
        q = q.filter(Waste.store_id == store_id)
    if reason:
        q = q.filter(Waste.waste_reason == reason)
    if date_from:
        q = q.filter(Waste.waste_date >= dateparse(date_from).date())
    if date_to:
        q = q.filter(Waste.waste_date <= dateparse(date_to).date())
    items = q.order_by(Waste.id.desc()).all()
    return {"code": 0, "data": [
        {
            "id": w.id, "ingredient_id": w.ingredient_id,
            "ingredient_name": w.ingredient.name if w.ingredient else "",
            "waste_qty": w.waste_qty,
            "unit": w.ingredient.unit if w.ingredient else "",
            "waste_reason": w.waste_reason,
            "unit_price": w.unit_price,
            "waste_amount": w.waste_amount,
            "store_id": w.store_id,
            "waste_date": w.waste_date.isoformat(),
            "note": w.note
        } for w in items
    ]}


# ========== 补货预警 ==========

@app.get("/api/restock-alert", tags=["补货预警"])
def restock_alert(db: Session = Depends(get_db)):
    backup_database()
    today = date.today()
    seven_days_ago = today - timedelta(days=7)
    tomorrow = today + timedelta(days=1)

    ingredients = db.query(Ingredient).all()
    alerts = []

    for ing in ingredients:
        consumed = db.query(Outbound).filter(
            Outbound.ingredient_id == ing.id,
            Outbound.outbound_date >= seven_days_ago,
            Outbound.outbound_date < today
        ).all()
        total_consumed = sum(c.qty for c in consumed)
        daily_avg = total_consumed / 7.0 if total_consumed > 0 else 0

        stock = db.query(Stock).filter(Stock.ingredient_id == ing.id).first()
        current_qty = stock.current_qty if stock else 0.0
        last_unit_price = stock.last_unit_price if stock and stock.last_unit_price > 0 else ing.unit_cost

        fl = db.query(ForecastLog).filter(
            ForecastLog.ingredient_id == ing.id,
            ForecastLog.forecast_date == tomorrow
        ).first()
        forecast_qty = fl.forecast_qty if fl else None

        pending_orders = db.query(Order).filter(
            Order.ingredient_id == ing.id,
            Order.status != "completed"
        ).all()
        in_transit_qty = round(sum(o.order_qty - o.received_qty for o in pending_orders), 3)

        effective_qty = round(current_qty + in_transit_qty, 3)

        has_consumption_data = daily_avg > 0 or (forecast_qty and forecast_qty > 0)
        if not has_consumption_data:
            continue

        safety_from_avg = daily_avg * ing.safety_stock_days
        safety_from_forecast = (forecast_qty if forecast_qty and forecast_qty > 0 else 0) * 1
        safety_qty = round(max(safety_from_avg, safety_from_forecast), 3)
        shortfall = round(safety_qty - effective_qty, 3)

        effective_daily = forecast_qty if forecast_qty and forecast_qty > 0 else daily_avg
        if effective_daily > 0:
            days_supported = round(effective_qty / effective_daily, 1)
        else:
            days_supported = 999

        if shortfall > 0 or (forecast_qty and forecast_qty > 0 and effective_qty < forecast_qty):
            if forecast_qty and forecast_qty > 0 and daily_avg <= 0:
                suggest_qty = round(forecast_qty * ing.safety_stock_days * 1.2, 2)
            elif forecast_qty and forecast_qty > 0:
                suggest_qty = round(max(shortfall, forecast_qty * ing.safety_stock_days * 1.2), 2)
            else:
                suggest_qty = round(max(shortfall, daily_avg * ing.safety_stock_days * 1.5), 2)

            if ing.shelf_life_days > 0 and effective_daily > 0:
                max_reasonable = round(effective_daily * ing.shelf_life_days, 2)
                if suggest_qty > max_reasonable:
                    suggest_qty = max_reasonable

            if days_supported <= 1:
                risk_level = "urgent"
            elif days_supported <= ing.safety_stock_days:
                risk_level = "high"
            elif days_supported <= ing.safety_stock_days * 2:
                risk_level = "medium"
            else:
                risk_level = "low"

            alerts.append({
                "ingredient_id": ing.id,
                "ingredient_name": ing.name,
                "category": ing.category,
                "unit": ing.unit,
                "current_stock": round(current_qty, 3),
                "in_transit_qty": in_transit_qty,
                "effective_qty": effective_qty,
                "daily_avg_7d": round(daily_avg, 3),
                "forecast_qty_tomorrow": round(forecast_qty, 3) if forecast_qty else None,
                "safety_stock_days": ing.safety_stock_days,
                "shelf_life_days": ing.shelf_life_days,
                "safety_qty": safety_qty,
                "shortfall": shortfall,
                "suggested_purchase_qty": suggest_qty,
                "days_supported": days_supported,
                "risk_level": risk_level,
                "last_unit_price": last_unit_price,
                "estimated_cost": round(suggest_qty * last_unit_price, 2)
            })

    risk_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    alerts.sort(key=lambda x: (risk_order.get(x["risk_level"], 9), -x["shortfall"]))
    return {"code": 0, "data": {
        "generated_at": datetime.now().isoformat(),
        "total_alerts": len(alerts),
        "urgent_count": sum(1 for a in alerts if a["risk_level"] == "urgent"),
        "high_count": sum(1 for a in alerts if a["risk_level"] == "high"),
        "total_estimated_cost": round(sum(a["estimated_cost"] for a in alerts), 2),
        "items": alerts
    }}


# ========== 促销管理 ==========

@app.post("/api/promotions", tags=["促销管理"])
def create_promotion(data: PromotionCreate, db: Session = Depends(get_db)):
    ing = db.query(Ingredient).filter(Ingredient.id == data.ingredient_id).first()
    if not ing:
        raise HTTPException(400, "原料不存在")
    pd = dateparse(data.promo_date).date()
    existing = db.query(Promotion).filter(
        Promotion.ingredient_id == data.ingredient_id,
        Promotion.promo_date == pd
    ).first()
    if existing:
        existing.multiplier = data.multiplier
        existing.note = data.note
        prom = existing
    else:
        prom = Promotion(
            ingredient_id=data.ingredient_id,
            promo_date=pd,
            multiplier=data.multiplier,
            note=data.note
        )
        db.add(prom)
    db.commit()
    db.refresh(prom)
    return {"code": 0, "data": {"id": prom.id, "ingredient_name": ing.name, "promo_date": pd.isoformat()}}


@app.get("/api/promotions", tags=["促销管理"])
def list_promotions(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db)
):
    q = db.query(Promotion)
    if date_from:
        q = q.filter(Promotion.promo_date >= dateparse(date_from).date())
    if date_to:
        q = q.filter(Promotion.promo_date <= dateparse(date_to).date())
    items = q.order_by(Promotion.promo_date.desc()).all()
    return {"code": 0, "data": [
        {
            "id": p.id, "ingredient_id": p.ingredient_id,
            "ingredient_name": p.ingredient.name if p.ingredient else "",
            "promo_date": p.promo_date.isoformat(),
            "multiplier": p.multiplier, "note": p.note
        } for p in items
    ]}


# ========== 预测日志 ==========

@app.get("/api/forecast-logs", tags=["预测管理"])
def list_forecast_logs(
    flagged_only: bool = False,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db)
):
    q = db.query(ForecastLog)
    if flagged_only:
        q = q.filter(ForecastLog.is_flagged == True)
    if date_from:
        q = q.filter(ForecastLog.forecast_date >= dateparse(date_from).date())
    if date_to:
        q = q.filter(ForecastLog.forecast_date <= dateparse(date_to).date())
    items = q.order_by(ForecastLog.id.desc()).limit(200).all()
    return {"code": 0, "data": [
        {
            "id": f.id, "ingredient_id": f.ingredient_id,
            "ingredient_name": f.ingredient.name if f.ingredient else "",
            "forecast_date": f.forecast_date.isoformat(),
            "forecast_qty": f.forecast_qty,
            "actual_qty": f.actual_qty,
            "error_rate": round(f.error_rate, 4) if f.error_rate else None,
            "is_flagged": f.is_flagged
        } for f in items
    ]}


# ========== 盘存管理 ==========

@app.post("/api/stocktake", tags=["盘存管理"])
def create_stocktake(data: StocktakeCreate, db: Session = Depends(get_db)):
    today = date.today()
    anomalies = []
    results = []

    ingredient_ids = [item.ingredient_id for item in data.items]
    query = db.query(Ingredient)
    if data.quick_mode:
        query = query.filter(Ingredient.unit_cost > 50)
        high_value_ids = [i.id for i in query.all()]
        ingredient_ids = [i for i in ingredient_ids if i in high_value_ids]

    for item in data.items:
        if data.quick_mode and item.ingredient_id not in ingredient_ids:
            continue
        ing = db.query(Ingredient).filter(Ingredient.id == item.ingredient_id).first()
        if not ing:
            continue
        stock = db.query(Stock).filter(Stock.ingredient_id == item.ingredient_id).first()
        system_qty = stock.current_qty if stock else 0.0
        diff_qty = round(item.actual_qty - system_qty, 3)
        diff_rate = round(abs(diff_qty) / system_qty, 4) if system_qty > 0 else 0.0

        st = Stocktake(
            ingredient_id=item.ingredient_id,
            store_id=data.store_id,
            system_qty=system_qty,
            actual_qty=item.actual_qty,
            diff_qty=diff_qty,
            diff_rate=diff_rate,
            stocktake_date=today,
            operator=data.operator
        )
        db.add(st)

        item_result = {
            "ingredient_id": ing.id,
            "ingredient_name": ing.name,
            "system_qty": system_qty,
            "actual_qty": item.actual_qty,
            "diff_qty": diff_qty,
            "diff_rate": diff_rate,
            "is_anomaly": diff_rate > 0.10
        }
        results.append(item_result)
        if diff_rate > 0.10:
            anomalies.append(item_result)

    db.commit()
    return {"code": 0, "data": {
        "stocktake_date": today.isoformat(),
        "total_items": len(results),
        "anomaly_count": len(anomalies),
        "anomaly_threshold": "10%",
        "results": results,
        "anomalies": anomalies
    }}


@app.get("/api/stocktake", tags=["盘存管理"])
def list_stocktake(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    store_id: Optional[str] = None,
    anomaly_only: bool = False,
    db: Session = Depends(get_db)
):
    q = db.query(Stocktake)
    if store_id:
        q = q.filter(Stocktake.store_id == store_id)
    if date_from:
        q = q.filter(Stocktake.stocktake_date >= dateparse(date_from).date())
    if date_to:
        q = q.filter(Stocktake.stocktake_date <= dateparse(date_to).date())
    if anomaly_only:
        q = q.filter(Stocktake.diff_rate > 0.10)
    items = q.order_by(Stocktake.id.desc()).all()
    return {"code": 0, "data": [
        {
            "id": s.id, "ingredient_id": s.ingredient_id,
            "ingredient_name": s.ingredient.name if s.ingredient else "",
            "store_id": s.store_id,
            "system_qty": s.system_qty, "actual_qty": s.actual_qty,
            "diff_qty": s.diff_qty, "diff_rate": s.diff_rate,
            "stocktake_date": s.stocktake_date.isoformat(),
            "operator": s.operator
        } for s in items
    ]}


# ========== 每日运营摘要 ==========

@app.get("/api/daily-digest", tags=["每日运营摘要"])
def daily_digest(
    report_date: Optional[str] = Query(None, description="报告日期 YYYY-MM-DD，默认今天"),
    markdown_only: bool = Query(False, description="是否仅返回Markdown文本"),
    db: Session = Depends(get_db)
):
    import pandas as pd
    rd = dateparse(report_date).date() if report_date else date.today()
    seven_days_ago = rd - timedelta(days=7)
    tomorrow = rd + timedelta(days=1)

    restock_alerts = []
    ingredients = db.query(Ingredient).all()
    for ing in ingredients:
        consumed = db.query(Outbound).filter(
            Outbound.ingredient_id == ing.id,
            Outbound.outbound_date >= seven_days_ago,
            Outbound.outbound_date < rd
        ).all()
        total_consumed = sum(c.qty for c in consumed)
        daily_avg = total_consumed / 7.0 if total_consumed > 0 else 0
        stock = db.query(Stock).filter(Stock.ingredient_id == ing.id).first()
        current_qty = stock.current_qty if stock else 0.0
        last_unit_price = stock.last_unit_price if stock and stock.last_unit_price > 0 else ing.unit_cost
        fl = db.query(ForecastLog).filter(ForecastLog.ingredient_id == ing.id, ForecastLog.forecast_date == tomorrow).first()
        forecast_qty = fl.forecast_qty if fl else None
        pending_orders = db.query(Order).filter(
            Order.ingredient_id == ing.id,
            Order.order_date <= rd,
            Order.status != "completed"
        ).all()
        in_transit_qty = round(sum(o.order_qty - o.received_qty for o in pending_orders), 3)
        effective_qty = round(current_qty + in_transit_qty, 3)
        has_data = daily_avg > 0 or (forecast_qty and forecast_qty > 0)
        if not has_data:
            continue
        safety_from_avg = daily_avg * ing.safety_stock_days
        safety_from_forecast = (forecast_qty if forecast_qty and forecast_qty > 0 else 0)
        safety_qty = round(max(safety_from_avg, safety_from_forecast), 3)
        shortfall = round(safety_qty - effective_qty, 3)
        effective_daily = forecast_qty if forecast_qty and forecast_qty > 0 else daily_avg
        if effective_daily > 0:
            days_supported = round(effective_qty / effective_daily, 1)
        else:
            days_supported = 999
        need_alert = shortfall > 0 or (forecast_qty and forecast_qty > 0 and effective_qty < forecast_qty)
        if not need_alert:
            continue
        if forecast_qty and forecast_qty > 0 and daily_avg <= 0:
            suggest_qty = round(forecast_qty * ing.safety_stock_days * 1.2, 2)
        elif forecast_qty and forecast_qty > 0:
            suggest_qty = round(max(shortfall, forecast_qty * ing.safety_stock_days * 1.2), 2)
        else:
            suggest_qty = round(max(shortfall, daily_avg * ing.safety_stock_days * 1.5), 2)
        if ing.shelf_life_days > 0 and effective_daily > 0:
            max_reasonable = round(effective_daily * ing.shelf_life_days, 2)
            if suggest_qty > max_reasonable:
                suggest_qty = max_reasonable
        if days_supported <= 1:
            risk = "urgent"
        elif days_supported <= ing.safety_stock_days:
            risk = "high"
        elif days_supported <= ing.safety_stock_days * 2:
            risk = "medium"
        else:
            risk = "low"
        restock_alerts.append({
            "ingredient_id": ing.id, "ingredient_name": ing.name, "category": ing.category,
            "unit": ing.unit, "current_stock": round(current_qty, 3),
            "in_transit_qty": in_transit_qty, "effective_qty": effective_qty,
            "daily_avg_7d": round(daily_avg, 3),
            "forecast_qty_tomorrow": round(forecast_qty, 3) if forecast_qty else None,
            "safety_stock_days": ing.safety_stock_days, "shelf_life_days": ing.shelf_life_days,
            "shortfall": shortfall, "suggested_purchase_qty": suggest_qty,
            "days_supported": days_supported, "risk_level": risk,
            "last_unit_price": last_unit_price, "estimated_cost": round(suggest_qty * last_unit_price, 2)
        })
    risk_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    restock_alerts.sort(key=lambda x: (risk_order.get(x["risk_level"], 9), -x["shortfall"]))
    restock = {
        "total": len(restock_alerts),
        "urgent_count": sum(1 for a in restock_alerts if a["risk_level"] == "urgent"),
        "high_count": sum(1 for a in restock_alerts if a["risk_level"] == "high"),
        "total_estimated_cost": round(sum(a["estimated_cost"] for a in restock_alerts), 2),
        "items": restock_alerts
    }

    fc_logs = db.query(ForecastLog).filter(
        and_(ForecastLog.is_flagged == True, ForecastLog.forecast_date >= seven_days_ago, ForecastLog.forecast_date <= rd)
    ).order_by(ForecastLog.forecast_date.desc()).all()
    fc_counter: Dict[int, Dict] = {}
    for fl in fc_logs:
        if fl.ingredient_id not in fc_counter:
            fc_counter[fl.ingredient_id] = {
                "ingredient_id": fl.ingredient_id,
                "ingredient_name": fl.ingredient.name if fl.ingredient else "",
                "category": fl.ingredient.category if fl.ingredient else "",
                "flagged_count": 0, "avg_error_rate": 0.0,
                "last_error_rate": 0.0, "last_date": None,
                "last_forecast": 0, "last_actual": 0,
            }
        c = fc_counter[fl.ingredient_id]
        c["flagged_count"] += 1
        c["last_error_rate"] = fl.error_rate or 0
        c["avg_error_rate"] += (fl.error_rate or 0)
        c["last_date"] = fl.forecast_date.isoformat()
        c["last_forecast"] = fl.forecast_qty
        c["last_actual"] = fl.actual_qty if fl.actual_qty is not None else 0
    fc_items = []
    for c in fc_counter.values():
        c["avg_error_rate"] = round(c["avg_error_rate"] / c["flagged_count"] * 100, 2)
        c["last_error_rate"] = round(c["last_error_rate"] * 100, 2)
        fc_items.append(c)
    fc_items.sort(key=lambda x: x["flagged_count"], reverse=True)
    fcerrors = {"total": len(fc_items), "items": fc_items, "period": f"{seven_days_ago.isoformat()} ~ {rd.isoformat()}"}

    period_days = 7
    end = rd
    start = end - timedelta(days=period_days - 1)
    prev_start = start - timedelta(days=period_days)
    prev_end = start - timedelta(days=1)
    wastes = db.query(Waste).filter(Waste.waste_date >= prev_start, Waste.waste_date <= end).all()
    outbounds = db.query(Outbound).filter(Outbound.outbound_date >= prev_start, Outbound.outbound_date <= end).all()
    w_data = []
    for w in wastes:
        w_data.append({"store_id": w.store_id, "waste_amount": w.waste_amount, "waste_date": w.waste_date})
    df_w = pd.DataFrame(w_data)
    ob_data = []
    for o in outbounds:
        ing = o.ingredient
        cost = ing.unit_cost if ing else 0
        ob_data.append({"store_id": o.store_id, "outbound_date": o.outbound_date, "value": o.qty * cost})
    df_ob = pd.DataFrame(ob_data)
    if not df_w.empty and not df_ob.empty:
        if not df_w.empty:
            df_w_curr = df_w[(df_w["waste_date"] >= start) & (df_w["waste_date"] <= end)]
            df_w_prev = df_w[(df_w["waste_date"] >= prev_start) & (df_w["waste_date"] <= prev_end)]
        else:
            df_w_curr = df_w
            df_w_prev = df_w
        if not df_ob.empty:
            df_ob_curr = df_ob[(df_ob["outbound_date"] >= start) & (df_ob["outbound_date"] <= end)]
            df_ob_prev = df_ob[(df_ob["outbound_date"] >= prev_start) & (df_ob["outbound_date"] <= prev_end)]
        else:
            df_ob_curr = df_ob
            df_ob_prev = df_ob
        all_stores = list(set(
            (df_w_curr["store_id"].tolist() if not df_w_curr.empty else []) +
            (df_ob_curr["store_id"].tolist() if not df_ob_curr.empty else []) +
            (df_w_prev["store_id"].tolist() if not df_w_prev.empty else []) +
            (df_ob_prev["store_id"].tolist() if not df_ob_prev.empty else [])
        ))
        items = []
        flagged = []
        for sid in sorted(all_stores):
            curr_w = df_w_curr[df_w_curr["store_id"] == sid]["waste_amount"].sum() if not df_w_curr.empty else 0
            curr_ob = df_ob_curr[df_ob_curr["store_id"] == sid]["value"].sum() if not df_ob_curr.empty else 0
            prev_w = df_w_prev[df_w_prev["store_id"] == sid]["waste_amount"].sum() if not df_w_prev.empty else 0
            prev_ob = df_ob_prev[df_ob_prev["store_id"] == sid]["value"].sum() if not df_ob_prev.empty else 0
            curr_rate = round(curr_w / curr_ob * 100, 2) if curr_ob > 0 else 0
            prev_rate = round(prev_w / prev_ob * 100, 2) if prev_ob > 0 else 0
            is_consecutive = curr_rate > 5 and prev_rate > 5
            entry = {
                "store_id": sid,
                "current_waste_amount": round(curr_w, 2),
                "current_outbound_value": round(curr_ob, 2),
                "current_waste_rate": curr_rate,
                "prev_waste_amount": round(prev_w, 2),
                "prev_outbound_value": round(prev_ob, 2),
                "prev_waste_rate": prev_rate,
                "is_consecutive_high": is_consecutive
            }
            items.append(entry)
            if is_consecutive:
                flagged.append(entry)
        items.sort(key=lambda x: x["current_waste_rate"], reverse=True)
        flagged.sort(key=lambda x: x["current_waste_rate"], reverse=True)
        for e in items:
            e["is_consecutive_high"] = bool(e["is_consecutive_high"])
        for e in flagged:
            e["is_consecutive_high"] = bool(e["is_consecutive_high"])
    else:
        items, flagged = [], []
    stores = {
        "current_period": f"{start.isoformat()} ~ {end.isoformat()}",
        "prev_period": f"{prev_start.isoformat()} ~ {prev_end.isoformat()}",
        "total_stores": len(items),
        "flagged_count": len(flagged),
        "items": items,
        "flagged": flagged,
        "has_flagged": bool(len(flagged) > 0)
    }

    L = []
    L.append(f"# 📋 餐饮中央厨房每日运营摘要")
    L.append(f"> 报告日期：**{rd.isoformat()}**  生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    L.append("## 一、关键指标速览\n")
    L.append("| 模块 | 指标 | 数值 |")
    L.append("|------|------|------|")
    L.append(f"| 📦 补货预警 | 需补货原料数 | **{restock['total']}** 种 |")
    L.append(f"| 📦 补货预警 | 紧急/高风险 | {restock['urgent_count']} / {restock['high_count']} 种 |")
    L.append(f"| 📦 补货预警 | 预估采购额 | ¥ {restock['total_estimated_cost']:,.2f} |")
    L.append(f"| 📈 预测误差 | 近7天预测不准原料 | **{fcerrors['total']}** 种 |")
    L.append(f"| 🗑️  高损耗门店 | 连续两周>5% | **{stores['flagged_count']}** 家 |")
    L.append(f"| 🗑️  高损耗门店 | 当期周期 | {stores['current_period']} |")
    L.append(f"| 🗑️  高损耗门店 | 对比周期 | {stores['prev_period']} |")
    L.append("\n## 二、补货建议清单\n")
    if restock["items"]:
        L.append("| 优先级 | 原料名称 | 分类 | 现库存 | 在途 | 有效库存 | 明日预测 | 缺口 | 建议采购 | 可支撑天数 | 风险 |")
        L.append("|--------|----------|------|--------|------|----------|----------|------|----------|------------|------|")
        risk_icon = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
        for a in restock["items"]:
            fc = f"{a['forecast_qty_tomorrow']:.1f}" if a['forecast_qty_tomorrow'] else "-"
            L.append(
                f"| {risk_icon.get(a['risk_level'], '')} {a['risk_level']} | "
                f"{a['ingredient_name']} | {a['category']} | "
                f"{a['current_stock']:.1f}{a['unit']} | {a['in_transit_qty']:.1f}{a['unit']} | {a['effective_qty']:.1f}{a['unit']} | "
                f"{fc}{a['unit']} | {a['shortfall']:+.1f}{a['unit']} | "
                f"{a['suggested_purchase_qty']:.1f}{a['unit']} | {a['days_supported']:.1f}天 | "
                f"¥{a['estimated_cost']:,.2f} |"
            )
    else:
        L.append("✅ 所有原料库存充足，暂无补货需求")
    L.append("\n## 三、近7天预测不准清单（误差>30%）\n")
    if fcerrors["items"]:
        L.append(f"> 统计周期：{fcerrors['period']}\n")
        L.append("| 原料 | 分类 | 超标次数 | 平均误差率 | 最近误差率 | 最近日期 | 最近预测 vs 实际 |")
        L.append("|------|------|----------|------------|------------|----------|------------------|")
        for f in fcerrors["items"]:
            L.append(
                f"| {f['ingredient_name']} | {f['category']} | {f['flagged_count']} 次 | "
                f"{f['avg_error_rate']:.1f}% | {f['last_error_rate']:.1f}% | "
                f"{f['last_date']} | {f['last_forecast']:.1f} vs {f['last_actual']:.1f} |"
            )
        L.append("\n> 💡 以上原料建议采购负责人结合实际经营情况复盘预测模型，或调整促销录入口径")
    else:
        L.append("✅ 近7天所有原料预测误差均在30%以内，预测质量良好")
    L.append("\n## 四、门店损耗率监控（连续两周>5%标红）\n")
    if stores["flagged"]:
        L.append(f"> 当期周期 {stores['current_period']}  |  上期周期 {stores['prev_period']}\n")
        L.append("| 门店 | 当期损耗率 | 当期损耗额 | 上期损耗率 | 上期损耗额 | 状态 |")
        L.append("|------|------------|------------|------------|------------|------|")
        for s in stores["flagged"]:
            L.append(
                f"| 🔴 **{s['store_id']}** | {s['current_waste_rate']:.2f}% | ¥{s['current_waste_amount']:,.2f} | "
                f"{s['prev_waste_rate']:.2f}% | ¥{s['prev_waste_amount']:,.2f} | **连续超标** |"
            )
        L.append("\n> 🚨 以上门店连续两周损耗率均超过5%警戒线，**请管理组现场核查后厨管理与存储流程**")
    else:
        L.append(f"> 当期周期 {stores['current_period']}  |  上期周期 {stores['prev_period']}\n")
        L.append("✅ **无连续两周高损耗门店**，所有门店损耗率均在正常范围（或单周超标但未连续两周）")
    L.append("\n## 五、所有门店损耗率一览\n")
    if stores["items"]:
        L.append("| 门店 | 当期损耗率 | 当期损耗额 | 上期损耗率 | 上期损耗额 | 状态 |")
        L.append("|------|------------|------------|------------|------------|------|")
        for s in stores["items"]:
            if s["is_consecutive_high"]:
                flag = "🔴 连续两周超标"
            elif s["current_waste_rate"] > 5:
                flag = "🟡 当期超标"
            else:
                flag = "🟢 正常"
            L.append(
                f"| {s['store_id']} | {s['current_waste_rate']:.2f}% | ¥{s['current_waste_amount']:,.2f} | "
                f"{s['prev_waste_rate']:.2f}% | ¥{s['prev_waste_amount']:,.2f} | {flag} |"
            )
    else:
        L.append("无门店数据")
    md_text = "\n".join(L)

    if markdown_only:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(md_text, media_type="text/markdown; charset=utf-8")

    return {"code": 0, "data": {
        "report_date": rd.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "markdown": md_text,
        "restock": restock,
        "forecast_errors": fcerrors,
        "stores": stores,
    }}


@app.get("/api/daily-digest/excel", tags=["每日运营摘要"])
def daily_digest_excel(
    report_date: Optional[str] = Query(None, description="报告日期 YYYY-MM-DD，默认今天"),
    db: Session = Depends(get_db)
):
    import pandas as pd
    import tempfile
    from fastapi.responses import FileResponse

    rd = dateparse(report_date).date() if report_date else date.today()
    seven_days_ago = rd - timedelta(days=7)
    tomorrow = rd + timedelta(days=1)

    restock_alerts = []
    ingredients = db.query(Ingredient).all()
    for ing in ingredients:
        consumed = db.query(Outbound).filter(
            Outbound.ingredient_id == ing.id,
            Outbound.outbound_date >= seven_days_ago,
            Outbound.outbound_date < rd
        ).all()
        total_consumed = sum(c.qty for c in consumed)
        daily_avg = total_consumed / 7.0 if total_consumed > 0 else 0
        stock = db.query(Stock).filter(Stock.ingredient_id == ing.id).first()
        current_qty = stock.current_qty if stock else 0.0
        last_unit_price = stock.last_unit_price if stock and stock.last_unit_price > 0 else ing.unit_cost
        fl = db.query(ForecastLog).filter(ForecastLog.ingredient_id == ing.id, ForecastLog.forecast_date == tomorrow).first()
        forecast_qty = fl.forecast_qty if fl else None
        pending_orders = db.query(Order).filter(
            Order.ingredient_id == ing.id,
            Order.order_date <= rd,
            Order.status != "completed"
        ).all()
        in_transit_qty = round(sum(o.order_qty - o.received_qty for o in pending_orders), 3)
        effective_qty = round(current_qty + in_transit_qty, 3)
        has_data = daily_avg > 0 or (forecast_qty and forecast_qty > 0)
        if not has_data:
            continue
        safety_from_avg = daily_avg * ing.safety_stock_days
        safety_from_forecast = (forecast_qty if forecast_qty and forecast_qty > 0 else 0)
        safety_qty = round(max(safety_from_avg, safety_from_forecast), 3)
        shortfall = round(safety_qty - effective_qty, 3)
        effective_daily = forecast_qty if forecast_qty and forecast_qty > 0 else daily_avg
        if effective_daily > 0:
            days_supported = round(effective_qty / effective_daily, 1)
        else:
            days_supported = 999
        need_alert = shortfall > 0 or (forecast_qty and forecast_qty > 0 and effective_qty < forecast_qty)
        if not need_alert:
            continue
        if forecast_qty and forecast_qty > 0 and daily_avg <= 0:
            suggest_qty = round(forecast_qty * ing.safety_stock_days * 1.2, 2)
        elif forecast_qty and forecast_qty > 0:
            suggest_qty = round(max(shortfall, forecast_qty * ing.safety_stock_days * 1.2), 2)
        else:
            suggest_qty = round(max(shortfall, daily_avg * ing.safety_stock_days * 1.5), 2)
        if ing.shelf_life_days > 0 and effective_daily > 0:
            max_reasonable = round(effective_daily * ing.shelf_life_days, 2)
            if suggest_qty > max_reasonable:
                suggest_qty = max_reasonable
        if days_supported <= 1:
            risk = "urgent"
        elif days_supported <= ing.safety_stock_days:
            risk = "high"
        elif days_supported <= ing.safety_stock_days * 2:
            risk = "medium"
        else:
            risk = "low"
        restock_alerts.append({
            "ingredient_id": ing.id, "ingredient_name": ing.name, "category": ing.category,
            "unit": ing.unit, "current_stock": round(current_qty, 3),
            "in_transit_qty": in_transit_qty, "effective_qty": effective_qty,
            "daily_avg_7d": round(daily_avg, 3),
            "forecast_qty_tomorrow": round(forecast_qty, 3) if forecast_qty else None,
            "safety_stock_days": ing.safety_stock_days, "shelf_life_days": ing.shelf_life_days,
            "shortfall": shortfall, "suggested_purchase_qty": suggest_qty,
            "days_supported": days_supported, "risk_level": risk,
            "last_unit_price": last_unit_price, "estimated_cost": round(suggest_qty * last_unit_price, 2)
        })
    risk_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    restock_alerts.sort(key=lambda x: (risk_order.get(x["risk_level"], 9), -x["shortfall"]))
    restock = {
        "total": len(restock_alerts),
        "urgent_count": sum(1 for a in restock_alerts if a["risk_level"] == "urgent"),
        "high_count": sum(1 for a in restock_alerts if a["risk_level"] == "high"),
        "total_estimated_cost": round(sum(a["estimated_cost"] for a in restock_alerts), 2),
        "items": restock_alerts
    }

    fc_logs = db.query(ForecastLog).filter(
        and_(ForecastLog.is_flagged == True, ForecastLog.forecast_date >= seven_days_ago, ForecastLog.forecast_date <= rd)
    ).order_by(ForecastLog.forecast_date.desc()).all()
    fc_counter: Dict[int, Dict] = {}
    for fl in fc_logs:
        if fl.ingredient_id not in fc_counter:
            fc_counter[fl.ingredient_id] = {
                "ingredient_id": fl.ingredient_id,
                "ingredient_name": fl.ingredient.name if fl.ingredient else "",
                "category": fl.ingredient.category if fl.ingredient else "",
                "flagged_count": 0, "avg_error_rate": 0.0,
                "last_error_rate": 0.0, "last_date": None,
                "last_forecast": 0, "last_actual": 0,
            }
        c = fc_counter[fl.ingredient_id]
        c["flagged_count"] += 1
        c["last_error_rate"] = fl.error_rate or 0
        c["avg_error_rate"] += (fl.error_rate or 0)
        c["last_date"] = fl.forecast_date.isoformat()
        c["last_forecast"] = fl.forecast_qty
        c["last_actual"] = fl.actual_qty if fl.actual_qty is not None else 0
    fc_items = []
    for c in fc_counter.values():
        c["avg_error_rate"] = round(c["avg_error_rate"] / c["flagged_count"] * 100, 2)
        c["last_error_rate"] = round(c["last_error_rate"] * 100, 2)
        fc_items.append(c)
    fc_items.sort(key=lambda x: x["flagged_count"], reverse=True)
    fcerrors = {"total": len(fc_items), "items": fc_items, "period": f"{seven_days_ago.isoformat()} ~ {rd.isoformat()}"}

    period_days = 7
    end = rd
    start = end - timedelta(days=period_days - 1)
    prev_start = start - timedelta(days=period_days)
    prev_end = start - timedelta(days=1)
    wastes = db.query(Waste).filter(Waste.waste_date >= prev_start, Waste.waste_date <= end).all()
    outbounds = db.query(Outbound).filter(Outbound.outbound_date >= prev_start, Outbound.outbound_date <= end).all()
    w_data = []
    for w in wastes:
        w_data.append({"store_id": w.store_id, "waste_amount": w.waste_amount, "waste_date": w.waste_date})
    df_w = pd.DataFrame(w_data)
    ob_data = []
    for o in outbounds:
        ing2 = o.ingredient
        cost = ing2.unit_cost if ing2 else 0
        ob_data.append({"store_id": o.store_id, "outbound_date": o.outbound_date, "value": o.qty * cost})
    df_ob = pd.DataFrame(ob_data)
    items_stores, flagged_stores = [], []
    if not df_w.empty or not df_ob.empty:
        if not df_w.empty:
            df_w_curr = df_w[(df_w["waste_date"] >= start) & (df_w["waste_date"] <= end)]
            df_w_prev = df_w[(df_w["waste_date"] >= prev_start) & (df_w["waste_date"] <= prev_end)]
        else:
            df_w_curr, df_w_prev = df_w, df_w
        if not df_ob.empty:
            df_ob_curr = df_ob[(df_ob["outbound_date"] >= start) & (df_ob["outbound_date"] <= end)]
            df_ob_prev = df_ob[(df_ob["outbound_date"] >= prev_start) & (df_ob["outbound_date"] <= prev_end)]
        else:
            df_ob_curr, df_ob_prev = df_ob, df_ob
        all_stores = list(set(
            (df_w_curr["store_id"].tolist() if not df_w_curr.empty else []) +
            (df_ob_curr["store_id"].tolist() if not df_ob_curr.empty else []) +
            (df_w_prev["store_id"].tolist() if not df_w_prev.empty else []) +
            (df_ob_prev["store_id"].tolist() if not df_ob_prev.empty else [])
        ))
        for sid in sorted(all_stores):
            curr_w = df_w_curr[df_w_curr["store_id"] == sid]["waste_amount"].sum() if not df_w_curr.empty else 0
            curr_ob = df_ob_curr[df_ob_curr["store_id"] == sid]["value"].sum() if not df_ob_curr.empty else 0
            prev_w = df_w_prev[df_w_prev["store_id"] == sid]["waste_amount"].sum() if not df_w_prev.empty else 0
            prev_ob = df_ob_prev[df_ob_prev["store_id"] == sid]["value"].sum() if not df_ob_prev.empty else 0
            curr_rate = round(curr_w / curr_ob * 100, 2) if curr_ob > 0 else 0
            prev_rate = round(prev_w / prev_ob * 100, 2) if prev_ob > 0 else 0
            is_consecutive = curr_rate > 5 and prev_rate > 5
            entry = {
                "store_id": sid,
                "current_waste_amount": round(curr_w, 2),
                "current_outbound_value": round(curr_ob, 2),
                "current_waste_rate": curr_rate,
                "prev_waste_amount": round(prev_w, 2),
                "prev_outbound_value": round(prev_ob, 2),
                "prev_waste_rate": prev_rate,
                "is_consecutive_high": bool(is_consecutive)
            }
            items_stores.append(entry)
            if is_consecutive:
                flagged_stores.append(entry)
    items_stores.sort(key=lambda x: x["current_waste_rate"], reverse=True)
    flagged_stores.sort(key=lambda x: x["current_waste_rate"], reverse=True)
    stores = {
        "current_period": f"{start.isoformat()} ~ {end.isoformat()}",
        "prev_period": f"{prev_start.isoformat()} ~ {prev_end.isoformat()}",
        "total_stores": len(items_stores),
        "flagged_count": len(flagged_stores),
        "items": items_stores,
        "flagged": flagged_stores,
        "has_flagged": bool(len(flagged_stores) > 0)
    }

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False, prefix=f"digest_{rd.isoformat()}_")
    tmp_path = tmp.name
    tmp.close()
    with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
        summary = [{
            "报告日期": rd.isoformat(),
            "需补货原料数": restock["total"],
            "紧急补货": restock["urgent_count"],
            "高风险补货": restock["high_count"],
            "预估采购额(元)": restock["total_estimated_cost"],
            "近7天预测不准原料数": fcerrors["total"],
            "连续两周高损耗门店数": stores["flagged_count"],
            "门店损耗对比周期_当期": stores["current_period"],
            "门店损耗对比周期_上期": stores["prev_period"],
        }]
        pd.DataFrame(summary).T.to_excel(writer, sheet_name="汇总", header=False)
        if restock["items"]:
            pd.DataFrame(restock["items"]).to_excel(writer, sheet_name="补货建议", index=False)
        if fcerrors["items"]:
            pd.DataFrame(fcerrors["items"]).to_excel(writer, sheet_name="预测不准清单", index=False)
        if stores["items"]:
            pd.DataFrame(stores["items"]).to_excel(writer, sheet_name="门店损耗对比", index=False)

    filename = f"daily_digest_{rd.isoformat().replace('-', '')}.xlsx"
    return FileResponse(
        path=tmp_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ========== 系统工具 ==========

@app.get("/api/health", tags=["系统"])
def health_check():
    return {"code": 0, "data": {"status": "ok", "time": datetime.now().isoformat(), "db": str(DB_PATH)}}


@app.post("/api/backup", tags=["系统"])
def trigger_backup():
    path = backup_database()
    return {"code": 0, "data": {"backup_path": path}}


@app.get("/api/stats", tags=["系统"])
def system_stats(db: Session = Depends(get_db)):
    today = date.today()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    return {"code": 0, "data": {
        "total_ingredients": db.query(Ingredient).count(),
        "total_stock_value": round(
            sum((s.current_qty * s.last_unit_price) for s in db.query(Stock).all()), 2
        ),
        "pending_orders": db.query(Order).filter(Order.status != "completed").count(),
        "week_outbound_qty": round(
            sum(o.qty for o in db.query(Outbound).filter(Outbound.outbound_date >= week_ago).all()), 2
        ),
        "month_waste_amount": round(
            sum(w.waste_amount for w in db.query(Waste).filter(Waste.waste_date >= month_ago).all()), 2
        ),
        "active_promotions": db.query(Promotion).filter(Promotion.promo_date >= today).count(),
        "flagged_forecasts": db.query(ForecastLog).filter(ForecastLog.is_flagged == True).count(),
    }}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

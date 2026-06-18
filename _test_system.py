import sys
from pathlib import Path
from datetime import datetime, timedelta, date

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from sqlalchemy.orm import Session
from server.main import (
    engine, SessionLocal,
    Ingredient, Stock, Order, Waste, Outbound,
    ForecastLog, Promotion, Stocktake
)
from server.forecast import run_forecast, verify_forecast_errors, get_flagged_list

def seed_test_data(db: Session):
    print("\n=== 1. 初始化测试数据 ===")

    ings = [
        Ingredient(name="鸡胸肉", category="肉类", unit="kg", safety_stock_days=3, shelf_life_days=5, unit_cost=28.5, barcode="6900001"),
        Ingredient(name="猪五花", category="肉类", unit="kg", safety_stock_days=3, shelf_life_days=7, unit_cost=42.0, barcode="6900002"),
        Ingredient(name="东北大米", category="主食", unit="kg", safety_stock_days=7, shelf_life_days=180, unit_cost=6.8, barcode="6900003"),
        Ingredient(name="生菜", category="蔬菜", unit="kg", safety_stock_days=2, shelf_life_days=3, unit_cost=5.5, barcode="6900004"),
        Ingredient(name="番茄", category="蔬菜", unit="kg", safety_stock_days=2, shelf_life_days=5, unit_cost=7.2, barcode="6900005"),
        Ingredient(name="鸡蛋", category="蛋品", unit="kg", safety_stock_days=5, shelf_life_days=30, unit_cost=11.0, barcode="6900006"),
        Ingredient(name="大豆油", category="调料", unit="L", safety_stock_days=10, shelf_life_days=365, unit_cost=15.0, barcode="6900007"),
        Ingredient(name="和牛A5", category="肉类", unit="kg", safety_stock_days=2, shelf_life_days=3, unit_cost=580.0, barcode="6900008"),
    ]
    for ing in ings:
        db.add(ing)
    db.flush()
    print(f"  添加 {len(ings)} 种原料")

    for ing in ings:
        db.add(Stock(ingredient_id=ing.id, current_qty=0.0))
    db.flush()

    today = date.today()
    for i in range(1, 31):
        d = today - timedelta(days=i)
        is_weekend = d.weekday() >= 5
        w_factor = 0.75 if is_weekend else 1.0

        for idx, ing in enumerate(ings[:6]):
            base = [55, 32, 120, 40, 28, 75][idx]
            qty = round(base * w_factor * (0.9 + 0.2 * ((i * (idx + 1)) % 10) / 10), 2)
            ob = Outbound(
                store_id="S001" if i % 3 else "S002",
                ingredient_id=ing.id,
                qty=qty,
                outbound_date=d,
                operator="test_data"
            )
            db.add(ob)
            if i <= 15:
                ob2 = Outbound(
                    store_id="S003",
                    ingredient_id=ing.id,
                    qty=round(qty * 0.4, 2),
                    outbound_date=d,
                    operator="test_data"
                )
                db.add(ob2)
    db.flush()
    print("  生成30天出库历史")

    stock_init = {1: 120.0, 2: 80.0, 3: 500.0, 4: 60.0, 5: 50.0, 6: 200.0, 7: 50.0, 8: 15.0}
    for iid, qty in stock_init.items():
        s = db.query(Stock).filter(Stock.ingredient_id == iid).first()
        if s:
            s.current_qty = qty
            s.last_unit_price = [28.5, 42.0, 6.8, 5.5, 7.2, 11.0, 15.0, 580.0][iid - 1]
            s.last_inbound_at = datetime.now()
    print("  初始化库存数据")

    waste_data = [
        (1, 8.5, "过期", "S001", 5),
        (1, 3.2, "制作失误", "S002", 6),
        (2, 5.1, "存储不当", "S001", 4),
        (4, 12.3, "过期", "S002", 3),
        (4, 6.7, "过期", "S001", 7),
        (5, 4.5, "制作失误", "S003", 2),
        (5, 3.8, "其他", "S001", 9),
        (6, 2.5, "存储不当", "S002", 5),
        (8, 1.2, "过期", "S001", 8),
        (2, 2.8, "过期", "S003", 10),
        (3, 15.0, "其他", "S001", 1),
        (7, 2.5, "制作失误", "S002", 2),
    ]
    for iid, qty, reason, sid, days_ago in waste_data:
        ing = db.query(Ingredient).filter(Ingredient.id == iid).first()
        wd = today - timedelta(days=days_ago)
        unit_price = ing.unit_cost
        w = Waste(
            store_id=sid,
            ingredient_id=iid,
            waste_qty=qty,
            waste_reason=reason,
            unit_price=unit_price,
            waste_amount=round(unit_price * qty, 2),
            waste_date=wd,
            note="测试数据"
        )
        db.add(w)
    print(f"  生成 {len(waste_data)} 条损耗记录")

    orders = [
        ("S001", 1, 100, 100, today - timedelta(days=2), 28.0),
        ("S002", 1, 50, 50, today - timedelta(days=2), 28.0),
        ("S001", 2, 80, 0, today - timedelta(days=1), 42.0),
        ("S003", 3, 200, 180, today - timedelta(days=1), 6.5),
        ("S001", 4, 100, 0, today, 5.5),
    ]
    for sid, iid, oq, rq, od, up in orders:
        order = Order(
            store_id=sid, ingredient_id=iid, order_qty=oq, received_qty=rq,
            order_date=od, status="completed" if rq >= oq else "partial",
            unit_price=up if rq > 0 else 0
        )
        db.add(order)
    print(f"  生成 {len(orders)} 条订货记录")

    db.commit()
    print("  ✅ 测试数据初始化完成")


def run_api_tests():
    print("\n=== 2. FastAPI 接口测试 ===")
    from fastapi.testclient import TestClient
    from server.main import app

    client = TestClient(app)

    r = client.get("/api/health")
    print(f"  [GET /api/health] status={r.status_code} -> {r.json()['data']['status']}")

    r = client.get("/api/stats")
    s = r.json()["data"]
    print(f"  [GET /api/stats] 原料:{s['total_ingredients']} 库存价值:¥{s['total_stock_value']:,.2f} 待处理订单:{s['pending_orders']}")

    payload = {"store_id": "S001", "ingredient_id": 1, "order_qty": 200, "order_date": date.today().isoformat()}
    r = client.post("/api/orders", json=payload)
    oid = r.json()["data"]["order_id"]
    print(f"  [POST /api/orders] 订货成功 -> order_id={oid}")

    r = client.post("/api/orders/confirm", json={"order_id": oid, "received_qty": 180, "unit_price": 27.8})
    print(f"  [POST /api/orders/confirm] 入库确认 -> {r.json()['data']}")

    r = client.post("/api/outbound", json={"ingredient_id": 1, "qty": 50, "store_id": "S001", "operator": "张师傅"})
    print(f"  [POST /api/outbound] 出库 -> 当前库存: {r.json()['data']['current_stock']} kg")

    r = client.post("/api/waste", json={"ingredient_id": 4, "waste_qty": 3.5, "waste_reason": "过期", "store_id": "S001"})
    wr = r.json()["data"]
    print(f"  [POST /api/waste] 损耗登记 -> 损耗金额:¥{wr['waste_amount']:,.2f} 剩余库存:{wr['current_stock']}")

    r = client.get("/api/restock-alert")
    ra = r.json()["data"]
    print(f"  [GET /api/restock-alert] 补货预警: {ra['total_alerts']} 项, 预估采购额 ¥{ra['total_estimated_cost']:,.2f}")
    for a in ra["items"][:3]:
        print(f"      - {a['ingredient_name']}: 现库存{a['current_stock']}, 缺口{a['shortfall']}{a['unit']}, 建议采购{a['suggested_purchase_qty']}{a['unit']}(¥{a['estimated_cost']})")

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = client.post("/api/promotions", json={"ingredient_id": 5, "promo_date": tomorrow, "multiplier": 1.8, "note": "番茄炒蛋5折促销"})
    print(f"  [POST /api/promotions] 登记促销 -> {r.json()['data']}")

    r = client.get("/api/ingredients", params={"keyword": "肉"})
    names = [i["name"] for i in r.json()["data"]]
    print(f"  [GET /api/ingredients?keyword=肉] 搜索结果: {names}")

    print("  ✅ API 接口测试完成")


def run_forecast_tests():
    print("\n=== 3. 消耗预测模块测试 ===")

    result = run_forecast(verbose=True)
    print(f"\n  预测完成: {result['total_ingredients']} 项, 日期: {result['target_date']}")
    restock = [x for x in result["items"] if x["need_restock"] and x["forecast_qty"] > 0]
    print(f"  需补货: {len(restock)} 项")

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    promo_res = None
    for x in result["items"]:
        if x["details"]["promo_applied"]:
            promo_res = x
            break
    if promo_res:
        print(f"  含促销调整: {promo_res['ingredient_name']} x{promo_res['details']['promo_multiplier']} -> {promo_res['forecast_qty']}{promo_res['unit']}")

    db = SessionLocal()
    try:
        target_date = date.today() - timedelta(days=1)
        from sqlalchemy import and_
        ings = db.query(Ingredient).limit(3).all()
        for ing in ings:
            total = 0
            for i in range(1, 8):
                qty = 50 + i * 5
                total += qty
                db.add(Outbound(
                    store_id="S001", ingredient_id=ing.id, qty=qty,
                    outbound_date=target_date, operator="forecast_test"
                ))
        db.commit()
    finally:
        db.close()

    verify_res = verify_forecast_errors(check_date=target_date.isoformat(), threshold=0.30)
    print(f"\n  误差核对: {verify_res['total_checked']} 条, 超标 {verify_res['flagged_count']} 条")

    flagged = get_flagged_list(days=7)
    print(f"  近7天预测不准清单: {flagged['total_flagged_ingredients']} 种")

    print("  ✅ 预测模块测试完成")


def final_report():
    print("\n=== 4. 系统结构总结 ===")
    print("""
  📁 项目结构:
  ├── requirements.txt           # 依赖清单
  ├── config.example.ini         # SMTP/企微配置示例
  ├── server/
  │   ├── main.py                # FastAPI 主服务 (8张表 + REST API)
  │   └── forecast.py            # 消耗预测模块 (移动平均+促销+误差追踪)
  ├── scripts/
  │   ├── waste_report.py        # 损耗分析脚本 (多维度聚合+图表+邮件)
  │   └── daily_stocktake.py     # 每日盘存脚本 (差异比对+企微推送)
  ├── data/
  │   └── kitchen.db             # SQLite 数据库
  ├── backups/                   # 每日自动备份目录
  └── reports/                   # 报告输出 (MD+Excel+PNG)
""")
    print("  🚀 启动命令:")
    print("     API服务:   python -m uvicorn server.main:app --reload --port 8000")
    print("     损耗报告:  python scripts/waste_report.py --period 7d")
    print("     每日盘存:  python scripts/daily_stocktake.py --store S001 --operator 王店长")
    print("     消耗预测:  python -m server.forecast run")
    print("     预测核对:  python -m server.forecast verify")
    print("     预测不准:  python -m server.forecast flagged --days 7")
    print("     登记促销:  python -m server.forecast promo 5 2026-06-20 --multiplier 1.8 --note 番茄5折")
    print()


if __name__ == "__main__":
    print("=" * 70)
    print(" 餐饮后厨原料库存与智能补货预警系统 - 功能测试")
    print("=" * 70)

    db = SessionLocal()
    try:
        existing = db.query(Ingredient).count()
        if existing == 0:
            seed_test_data(db)
        else:
            print(f"\n  数据库已存在 {existing} 种原料，跳过测试数据初始化")
    finally:
        db.close()

    run_api_tests()
    run_forecast_tests()

    print("\n" + "=" * 70)
    print("  损耗报告 & 盘存脚本，测试运行中...")
    print("=" * 70)

    import subprocess
    print("\n[运行损耗报告脚本]")
    p1 = subprocess.run(
        [sys.executable, "scripts/waste_report.py", "--period", "7d"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8"
    )
    if p1.stdout:
        for line in p1.stdout.strip().splitlines()[:25]:
            print(f"  {line}")
    if p1.stderr and p1.returncode != 0:
        print(f"  [错误] {p1.stderr[-400:]}")
    print(f"  退出码: {p1.returncode}")

    print("\n[盘存脚本 - 打印清单模式]")
    p2 = subprocess.run(
        [sys.executable, "scripts/daily_stocktake.py", "--store", "S001", "--operator", "test", "--list"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8"
    )
    if p2.stdout:
        for line in p2.stdout.strip().splitlines()[:20]:
            print(f"  {line}")
    print(f"  退出码: {p2.returncode}")

    final_report()

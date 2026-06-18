import sys
import argparse
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import create_engine, and_
from sqlalchemy.orm import sessionmaker, Session

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
DB_PATH = BASE_DIR / "data" / "kitchen.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

from server.main import (
    Base, Ingredient, Outbound, ForecastLog, Promotion, Stock
)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_30day_consumption(db: Session, ingredient_id: int, end_date: date) -> pd.DataFrame:
    start_date = end_date - timedelta(days=30)
    rows = db.query(Outbound).filter(
        and_(
            Outbound.ingredient_id == ingredient_id,
            Outbound.outbound_date >= start_date,
            Outbound.outbound_date <= end_date
        )
    ).all()
    if not rows:
        return pd.DataFrame(columns=["date", "qty", "weekday"])
    data = []
    for r in rows:
        data.append({
            "date": r.outbound_date,
            "qty": r.qty,
            "weekday": r.outbound_date.weekday()
        })
    df = pd.DataFrame(data)
    df = df.groupby("date", as_index=False).agg({"qty": "sum", "weekday": "first"})
    return df


def predict_single(
    db: Session,
    ingredient_id: int,
    target_date: date,
    df_hist: pd.DataFrame
) -> Tuple[float, Dict]:
    if df_hist.empty:
        return 0.0, {"method": "no_data", "same_weekday_avg": 0, "recent3_avg": 0}

    target_weekday = target_date.weekday()
    is_target_weekend = target_weekday >= 5

    same_weekday_mask = df_hist["weekday"] == target_weekday
    same_weekday_data = df_hist[same_weekday_mask]
    same_weekday_avg = same_weekday_data["qty"].mean() if len(same_weekday_data) > 0 else 0

    recent3_start = target_date - timedelta(days=4)
    recent3_end = target_date - timedelta(days=1)
    recent3_mask = (df_hist["date"] >= recent3_start) & (df_hist["date"] <= recent3_end)
    recent3_data = df_hist[recent3_mask]
    recent3_avg = recent3_data["qty"].mean() if len(recent3_data) > 0 else df_hist["qty"].mean()

    if same_weekday_avg > 0 and recent3_avg > 0:
        predicted = 0.6 * same_weekday_avg + 0.4 * recent3_avg
    elif same_weekday_avg > 0:
        predicted = same_weekday_avg
    else:
        predicted = recent3_avg

    promo = db.query(Promotion).filter(
        and_(
            Promotion.ingredient_id == ingredient_id,
            Promotion.promo_date == target_date
        )
    ).first()

    method_details = {
        "method": "weekday_weighted_ma",
        "same_weekday_avg": round(same_weekday_avg, 3),
        "recent3_avg": round(recent3_avg, 3),
        "is_target_weekend": is_target_weekend,
        "promo_applied": False,
        "promo_multiplier": 1.0,
        "promo_note": ""
    }

    if promo:
        predicted = predicted * promo.multiplier
        method_details["promo_applied"] = True
        method_details["promo_multiplier"] = promo.multiplier
        method_details["promo_note"] = promo.note

    return round(predicted, 3), method_details


def run_forecast(
    target_date: Optional[str] = None,
    ingredient_id: Optional[int] = None,
    verbose: bool = True
) -> Dict:
    db = SessionLocal()
    try:
        if target_date:
            t_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        else:
            t_date = date.today() + timedelta(days=1)

        ingredients = db.query(Ingredient)
        if ingredient_id:
            ingredients = ingredients.filter(Ingredient.id == ingredient_id)
        ingredients = ingredients.all()

        results = []
        flagged_count = 0

        for ing in ingredients:
            df = get_30day_consumption(db, ing.id, t_date - timedelta(days=1))
            pred_qty, details = predict_single(db, ing.id, t_date, df)

            existing = db.query(ForecastLog).filter(
                and_(
                    ForecastLog.ingredient_id == ing.id,
                    ForecastLog.forecast_date == t_date
                )
            ).first()

            if existing:
                existing.forecast_qty = pred_qty
                fl = existing
            else:
                fl = ForecastLog(
                    ingredient_id=ing.id,
                    forecast_date=t_date,
                    forecast_qty=pred_qty
                )
                db.add(fl)

            stock = db.query(Stock).filter(Stock.ingredient_id == ing.id).first()
            current_stock = stock.current_qty if stock else 0.0

            entry = {
                "ingredient_id": ing.id,
                "ingredient_name": ing.name,
                "category": ing.category,
                "unit": ing.unit,
                "target_date": t_date.isoformat(),
                "forecast_qty": pred_qty,
                "current_stock": round(current_stock, 3),
                "sufficient_for_days": round(current_stock / pred_qty, 1) if pred_qty > 0 else 999,
                "need_restock": pred_qty > current_stock,
                "details": details
            }
            results.append(entry)

            if details["same_weekday_avg"] > 0 or details["recent3_avg"] > 0:
                if verbose:
                    print(
                        f"  [{ing.id:>4}] {ing.name:<20} 预测: {pred_qty:>8.3f} {ing.unit:<4} "
                        f"| 同工作日均值: {details['same_weekday_avg']:>7.3f} | 近3日均: {details['recent3_avg']:>7.3f}"
                        f"{' | 促销x' + str(details['promo_multiplier']) if details['promo_applied'] else ''}"
                    )

        db.commit()

        if verbose:
            print(f"\n预测完成，共 {len(results)} 条，日期: {t_date.isoformat()}")

        return {
            "generated_at": datetime.now().isoformat(),
            "target_date": t_date.isoformat(),
            "total_ingredients": len(results),
            "items": results
        }
    finally:
        db.close()


def verify_forecast_errors(
    check_date: Optional[str] = None,
    threshold: float = 0.30,
    verbose: bool = True
) -> Dict:
    db = SessionLocal()
    try:
        if check_date:
            c_date = datetime.strptime(check_date, "%Y-%m-%d").date()
        else:
            c_date = date.today()

        logs = db.query(ForecastLog).filter(ForecastLog.forecast_date == c_date).all()
        flagged_list = []

        for fl in logs:
            actual_rows = db.query(Outbound).filter(
                and_(
                    Outbound.ingredient_id == fl.ingredient_id,
                    Outbound.outbound_date == c_date
                )
            ).all()
            total_actual = sum(r.qty for r in actual_rows)

            fl.actual_qty = total_actual
            if fl.forecast_qty > 0:
                err = abs(total_actual - fl.forecast_qty) / fl.forecast_qty
                fl.error_rate = round(err, 4)
                if err > threshold:
                    fl.is_flagged = True
                    flagged_list.append({
                        "ingredient_id": fl.ingredient_id,
                        "ingredient_name": fl.ingredient.name if fl.ingredient else "",
                        "forecast_qty": fl.forecast_qty,
                        "actual_qty": total_actual,
                        "diff": round(total_actual - fl.forecast_qty, 3),
                        "error_rate": round(err * 100, 2),
                        "threshold": f"{int(threshold * 100)}%"
                    })
                else:
                    fl.is_flagged = False
            else:
                fl.error_rate = None
                fl.is_flagged = False

        db.commit()

        if verbose:
            print(f"\n=== 预测误差核对 (日期: {c_date.isoformat()}, 阈值: {int(threshold * 100)}%) ===")
            print(f"共核对 {len(logs)} 条预测记录")
            if flagged_list:
                print(f"\n⚠️  预测不准清单 ({len(flagged_list)} 项，建议采购负责人复查):")
                print(f"{'ID':>4} | {'名称':<18} | {'预测':>8} | {'实际':>8} | {'差值':>8} | {'误差率':>8}")
                print("-" * 70)
                for f in flagged_list:
                    print(
                        f"{f['ingredient_id']:>4} | {f['ingredient_name']:<18} | "
                        f"{f['forecast_qty']:>8.3f} | {f['actual_qty']:>8.3f} | "
                        f"{f['diff']:>+8.3f} | {f['error_rate']:>7.1f}%"
                    )
            else:
                print("✅ 所有原料预测误差均在阈值内")

        return {
            "check_date": c_date.isoformat(),
            "threshold": threshold,
            "total_checked": len(logs),
            "flagged_count": len(flagged_list),
            "flagged_items": flagged_list
        }
    finally:
        db.close()


def get_flagged_list(
    days: int = 7,
    verbose: bool = True
) -> Dict:
    db = SessionLocal()
    try:
        since = date.today() - timedelta(days=days)
        logs = db.query(ForecastLog).filter(
            and_(
                ForecastLog.is_flagged == True,
                ForecastLog.forecast_date >= since
            )
        ).order_by(ForecastLog.forecast_date.desc()).all()

        counter: Dict[int, Dict] = {}
        for fl in logs:
            if fl.ingredient_id not in counter:
                counter[fl.ingredient_id] = {
                    "ingredient_id": fl.ingredient_id,
                    "ingredient_name": fl.ingredient.name if fl.ingredient else "",
                    "flagged_count": 0,
                    "avg_error_rate": 0.0,
                    "last_error_rate": 0.0,
                    "dates": []
                }
            c = counter[fl.ingredient_id]
            c["flagged_count"] += 1
            c["last_error_rate"] = fl.error_rate or 0
            c["avg_error_rate"] += (fl.error_rate or 0)
            c["dates"].append(fl.forecast_date.isoformat())

        summary = []
        for cid, c in counter.items():
            c["avg_error_rate"] = round(c["avg_error_rate"] / c["flagged_count"] * 100, 2)
            c["last_error_rate"] = round(c["last_error_rate"] * 100, 2)
            summary.append(c)
        summary.sort(key=lambda x: x["flagged_count"], reverse=True)

        if verbose:
            print(f"\n=== 近 {days} 天预测不准清单 (累计统计) ===")
            if summary:
                print(f"{'ID':>4} | {'名称':<18} | {'标记次数':>6} | {'平均误差':>8} | {'最近误差':>8} | 出现日期")
                print("-" * 85)
                for s in summary:
                    dates_str = ", ".join(s["dates"][:3]) + ("..." if len(s["dates"]) > 3 else "")
                    print(
                        f"{s['ingredient_id']:>4} | {s['ingredient_name']:<18} | "
                        f"{s['flagged_count']:>6} | {s['avg_error_rate']:>7.1f}% | "
                        f"{s['last_error_rate']:>7.1f}% | {dates_str}"
                    )
            else:
                print("✅ 近期无预测不准记录")

        return {"days": days, "total_flagged_ingredients": len(summary), "items": summary}
    finally:
        db.close()


def add_promotion(
    ingredient_id: int,
    promo_date: str,
    multiplier: float = 1.8,
    note: str = ""
) -> Dict:
    db = SessionLocal()
    try:
        from server.main import Promotion as P
        ing = db.query(Ingredient).filter(Ingredient.id == ingredient_id).first()
        if not ing:
            return {"success": False, "error": "原料不存在"}

        pd = datetime.strptime(promo_date, "%Y-%m-%d").date()
        existing = db.query(P).filter(
            and_(P.ingredient_id == ingredient_id, P.promo_date == pd)
        ).first()
        if existing:
            existing.multiplier = multiplier
            existing.note = note
            p = existing
        else:
            p = P(ingredient_id=ingredient_id, promo_date=pd, multiplier=multiplier, note=note)
            db.add(p)
        db.commit()
        print(f"✅ 促销已登记: [{ing.id}] {ing.name} @ {pd.isoformat()} x{multiplier}  {note}")
        return {"success": True, "ingredient": ing.name, "promo_date": pd.isoformat(), "multiplier": multiplier}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="餐饮后厨消耗预测模块")
    sub = parser.add_subparsers(dest="cmd", help="命令")

    p_run = sub.add_parser("run", help="执行明日消耗量预测")
    p_run.add_argument("--date", type=str, help="目标预测日期 YYYY-MM-DD，默认明天")
    p_run.add_argument("--id", type=int, help="只预测指定原料ID")

    p_ver = sub.add_parser("verify", help="核对历史预测误差")
    p_ver.add_argument("--date", type=str, help="核对日期 YYYY-MM-DD，默认今天")
    p_ver.add_argument("--threshold", type=float, default=0.30, help="误差阈值，默认0.30(30%)")

    sub.add_parser("flagged", help="查看近期预测不准清单").add_argument("--days", type=int, default=7)

    p_promo = sub.add_parser("promo", help="登记促销计划（覆盖算法预测）")
    p_promo.add_argument("ingredient_id", type=int)
    p_promo.add_argument("date", type=str, help="促销日期 YYYY-MM-DD")
    p_promo.add_argument("--multiplier", type=float, default=1.8, help="消耗量乘数，默认1.8")
    p_promo.add_argument("--note", type=str, default="", help="备注")

    args = parser.parse_args()

    if args.cmd == "run" or args.cmd is None:
        d = getattr(args, "date", None)
        i = getattr(args, "id", None)
        run_forecast(target_date=d, ingredient_id=i)
    elif args.cmd == "verify":
        verify_forecast_errors(check_date=args.date, threshold=args.threshold)
    elif args.cmd == "flagged":
        get_flagged_list(days=args.days)
    elif args.cmd == "promo":
        add_promotion(args.ingredient_id, args.date, args.multiplier, args.note)


if __name__ == "__main__":
    main()

import sys
import argparse
import configparser
import smtplib
from datetime import datetime, timedelta, date
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Dict, List, Optional

import pandas as pd
from tabulate import tabulate
from sqlalchemy import and_
from sqlalchemy.orm import Session

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
DB_PATH = BASE_DIR / "data" / "kitchen.db"
REPORT_DIR = BASE_DIR / "reports"
CONFIG_PATH = BASE_DIR / "config.ini"
DATABASE_URL = f"sqlite:///{DB_PATH}"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

from server.main import (
    Ingredient, Stock, Order, Waste, Outbound, ForecastLog, Promotion,
    Stocktake, engine, SessionLocal
)


def load_smtp_config() -> Optional[Dict]:
    if not CONFIG_PATH.exists():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    if "smtp" not in cfg:
        return None
    s = cfg["smtp"]
    return {
        "host": s.get("host", ""),
        "port": int(s.get("port", 465)),
        "use_ssl": s.getboolean("use_ssl", True),
        "user": s.get("user", ""),
        "password": s.get("password", ""),
        "sender": s.get("sender", s.get("user", "")),
        "recipients": [x.strip() for x in s.get("recipients", "").split(",") if x.strip()]
    }


def get_restock_alerts(db: Session, report_date: Optional[date] = None) -> Dict:
    rd = report_date or date.today()
    seven_days_ago = rd - timedelta(days=7)
    tomorrow = rd + timedelta(days=1)

    ingredients = db.query(Ingredient).all()
    alerts = []
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

        fl = db.query(ForecastLog).filter(
            ForecastLog.ingredient_id == ing.id, ForecastLog.forecast_date == tomorrow
        ).first()
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

        alerts.append({
            "ingredient_id": ing.id, "ingredient_name": ing.name,
            "category": ing.category, "unit": ing.unit,
            "current_stock": round(current_qty, 3),
            "in_transit_qty": in_transit_qty,
            "effective_qty": effective_qty,
            "daily_avg_7d": round(daily_avg, 3),
            "forecast_qty_tomorrow": round(forecast_qty, 3) if forecast_qty else None,
            "safety_stock_days": ing.safety_stock_days,
            "shelf_life_days": ing.shelf_life_days,
            "shortfall": shortfall,
            "suggested_purchase_qty": suggest_qty,
            "days_supported": days_supported,
            "risk_level": risk,
            "last_unit_price": last_unit_price,
            "estimated_cost": round(suggest_qty * last_unit_price, 2)
        })

    risk_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    alerts.sort(key=lambda x: (risk_order.get(x["risk_level"], 9), -x["shortfall"]))
    return {
        "total": len(alerts),
        "urgent_count": sum(1 for a in alerts if a["risk_level"] == "urgent"),
        "high_count": sum(1 for a in alerts if a["risk_level"] == "high"),
        "total_estimated_cost": round(sum(a["estimated_cost"] for a in alerts), 2),
        "items": alerts
    }


def get_forecast_errors(db: Session, report_date: Optional[date] = None) -> Dict:
    rd = report_date or date.today()
    seven_days_ago = rd - timedelta(days=7)
    logs = db.query(ForecastLog).filter(
        and_(ForecastLog.is_flagged == True, ForecastLog.forecast_date >= seven_days_ago, ForecastLog.forecast_date <= rd)
    ).order_by(ForecastLog.forecast_date.desc()).all()

    counter: Dict[int, Dict] = {}
    for fl in logs:
        if fl.ingredient_id not in counter:
            counter[fl.ingredient_id] = {
                "ingredient_id": fl.ingredient_id,
                "ingredient_name": fl.ingredient.name if fl.ingredient else "",
                "category": fl.ingredient.category if fl.ingredient else "",
                "flagged_count": 0,
                "avg_error_rate": 0.0,
                "last_error_rate": 0.0,
                "last_date": None,
                "last_forecast": 0,
                "last_actual": 0,
            }
        c = counter[fl.ingredient_id]
        c["flagged_count"] += 1
        c["last_error_rate"] = fl.error_rate or 0
        c["avg_error_rate"] += (fl.error_rate or 0)
        c["last_date"] = fl.forecast_date.isoformat()
        c["last_forecast"] = fl.forecast_qty
        c["last_actual"] = fl.actual_qty if fl.actual_qty is not None else 0
    items = []
    for c in counter.values():
        c["avg_error_rate"] = round(c["avg_error_rate"] / c["flagged_count"] * 100, 2)
        c["last_error_rate"] = round(c["last_error_rate"] * 100, 2)
        items.append(c)
    items.sort(key=lambda x: x["flagged_count"], reverse=True)
    return {"total": len(items), "items": items, "period": f"{seven_days_ago.isoformat()} ~ {rd.isoformat()}"}


def get_flagged_stores(db: Session, period_days: int = 7, report_date: Optional[date] = None) -> Dict:
    rd = report_date or date.today()
    end = rd
    start = end - timedelta(days=period_days - 1)
    prev_start = start - timedelta(days=period_days)
    prev_end = start - timedelta(days=1)

    wastes = db.query(Waste).filter(Waste.waste_date >= prev_start, Waste.waste_date <= end).all()
    outbounds = db.query(Outbound).filter(Outbound.outbound_date >= prev_start, Outbound.outbound_date <= end).all()

    w_data = []
    for w in wastes:
        w_data.append({
            "store_id": w.store_id, "waste_amount": w.waste_amount,
            "waste_date": w.waste_date
        })
    df_w = pd.DataFrame(w_data)

    ob_data = []
    for o in outbounds:
        ing = o.ingredient
        cost = ing.unit_cost if ing else 0
        ob_data.append({
            "store_id": o.store_id, "outbound_date": o.outbound_date,
            "value": o.qty * cost
        })
    df_ob = pd.DataFrame(ob_data)

    if df_w.empty and df_ob.empty:
        return {
            "current_period": f"{start.isoformat()} ~ {end.isoformat()}",
            "prev_period": f"{prev_start.isoformat()} ~ {prev_end.isoformat()}",
            "total": 0, "items": [], "has_flagged": False
        }

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
    return {
        "current_period": f"{start.isoformat()} ~ {end.isoformat()}",
        "prev_period": f"{prev_start.isoformat()} ~ {prev_end.isoformat()}",
        "total_stores": len(items),
        "flagged_count": len(flagged),
        "items": items,
        "flagged": flagged,
        "has_flagged": bool(len(flagged) > 0)
    }


def build_markdown(restock: Dict, fcerrors: Dict, stores: Dict, report_date: date) -> str:
    L = []
    L.append(f"# 📋 餐饮中央厨房每日运营摘要")
    L.append(f"> 报告日期：**{report_date.isoformat()}**  生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

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

    return "\n".join(L)


def print_console(restock: Dict, fcerrors: Dict, stores: Dict, report_date: date):
    print("\n" + "=" * 80)
    print(f"  📋 餐饮中央厨房每日运营摘要  |  报告日期: {report_date.isoformat()}")
    print("=" * 80)
    print(f"  📦 补货预警: {restock['total']} 种 (紧急{restock['urgent_count']}/高风险{restock['high_count']})  预估采购 ¥{restock['total_estimated_cost']:,.2f}")
    print(f"  📈 预测不准: {fcerrors['total']} 种 (近7天误差>30%)")
    print(f"  🗑️  高损耗门店: {stores['flagged_count']} 家 (连续两周>5%)  对比周期: {stores['prev_period']}")
    print("-" * 80)

    if restock["items"]:
        print("\n【补货建议 TOP5】")
        risk_icon = {"urgent": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
        tbl = []
        for a in restock["items"][:5]:
            fc = f"{a['forecast_qty_tomorrow']:.1f}" if a['forecast_qty_tomorrow'] else "-"
            tbl.append([
                risk_icon.get(a["risk_level"], "") + a["risk_level"],
                a["ingredient_name"], a["category"],
                f"{a['current_stock']:.1f}+{a['in_transit_qty']:.1f}={a['effective_qty']:.1f}{a['unit']}",
                f"{fc}",
                f"{a['suggested_purchase_qty']:.1f}{a['unit']}",
                f"¥{a['estimated_cost']:,.2f}",
                f"{a['days_supported']:.1f}天"
            ])
        print(tabulate(tbl, headers=["风险", "名称", "分类", "库存+在途=有效", "明日预测", "建议采购", "预估成本", "支撑天数"], tablefmt="github"))

    if fcerrors["items"]:
        print("\n【近7天预测不准清单】")
        tbl = []
        for f in fcerrors["items"][:5]:
            tbl.append([f["ingredient_name"], f["category"], f"{f['flagged_count']}次", f"{f['avg_error_rate']:.1f}%", f"{f['last_error_rate']:.1f}%"])
        print(tabulate(tbl, headers=["名称", "分类", "超标次数", "平均误差", "最近误差"], tablefmt="github"))

    if stores["flagged"]:
        print("\n🚨【连续两周高损耗门店】")
        tbl = []
        for s in stores["flagged"]:
            tbl.append([s["store_id"], f"{s['current_waste_rate']:.2f}%", f"¥{s['current_waste_amount']:,.2f}", f"{s['prev_waste_rate']:.2f}%", f"¥{s['prev_waste_amount']:,.2f}"])
        print(tabulate(tbl, headers=["门店", "当期损耗率", "当期金额", "上期损耗率", "上期金额"], tablefmt="github"))
    else:
        print("\n✅ 无连续两周高损耗门店")

    print("\n" + "=" * 80)


def export_excel(restock: Dict, fcerrors: Dict, stores: Dict, prefix: str, report_date: Optional[date] = None) -> str:
    rd = report_date or date.today()
    excel_path = REPORT_DIR / f"{prefix}_daily_digest.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
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
    return str(excel_path)


def send_email(md_path: Path, excel_path: str, cfg: Dict, subject: str):
    if not cfg or not cfg["host"] or not cfg["recipients"]:
        print("[邮件] SMTP 未配置或无收件人，跳过发送")
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["sender"]
        msg["To"] = ", ".join(cfg["recipients"])
        msg["Subject"] = subject
        with open(md_path, "r", encoding="utf-8") as f:
            html = f.read()
        import re
        html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
        html = re.sub(r"\n", "<br>", html)
        msg.attach(MIMEText(html, "html", "utf-8"))
        for fp in [excel_path]:
            if not Path(fp).exists():
                continue
            with open(fp, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{Path(fp).name}"')
            msg.attach(part)
        if cfg["use_ssl"]:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
            server.starttls()
        if cfg["user"] and cfg["password"]:
            server.login(cfg["user"], cfg["password"])
        server.sendmail(cfg["sender"], cfg["recipients"], msg.as_string())
        server.quit()
        print(f"[邮件] ✅ 已发送至: {', '.join(cfg['recipients'])}")
        return True
    except Exception as e:
        print(f"[邮件] ❌ 发送失败: {e}")
        return False


def generate_digest(report_date: Optional[date] = None) -> Dict:
    db = SessionLocal()
    try:
        rd = report_date or date.today()
        restock = get_restock_alerts(db, rd)
        fcerrors = get_forecast_errors(db, rd)
        stores = get_flagged_stores(db, report_date=rd)
        md = build_markdown(restock, fcerrors, stores, rd)
        return {
            "report_date": rd.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "markdown": md,
            "restock": restock,
            "forecast_errors": fcerrors,
            "stores": stores,
            "_report_date_obj": rd,
        }
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="餐饮中央厨房每日运营摘要")
    parser.add_argument("--date", type=str, help="报告日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--send-email", action="store_true", help="通过 SMTP 发送邮件")
    parser.add_argument("--subject", type=str, help="邮件主题前缀")
    args = parser.parse_args()

    rd = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    data = generate_digest(rd)

    restock = data["restock"]
    fcerrors = data["forecast_errors"]
    stores = data["stores"]

    print_console(restock, fcerrors, stores, rd)

    prefix = f"daily_digest_{rd.isoformat().replace('-', '')}"
    md_path = REPORT_DIR / f"{prefix}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(data["markdown"])
    excel_path = export_excel(restock, fcerrors, stores, prefix)

    print(f"\n📄 Markdown 报告: {md_path}")
    print(f"📊 Excel 明细: {excel_path}")

    if args.send_email:
        cfg = load_smtp_config()
        subj_prefix = args.subject or "[每日运营摘要]"
        subj = (f"{subj_prefix} {data['report_date']} | "
                f"补货{restock['total']}种¥{restock['total_estimated_cost']:,.0f} | "
                f"预测不准{fcerrors['total']}种 | "
                f"高损耗门店{stores['flagged_count']}家")
        send_email(md_path, excel_path, cfg, subj)


if __name__ == "__main__":
    main()

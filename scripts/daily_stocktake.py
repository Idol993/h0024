import sys
import argparse
import json
import configparser
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from tabulate import tabulate
from sqlalchemy import create_engine, and_
from sqlalchemy.orm import sessionmaker, Session

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
DB_PATH = BASE_DIR / "data" / "kitchen.db"
REPORT_DIR = BASE_DIR / "reports"
CONFIG_PATH = BASE_DIR / "config.ini"
DATABASE_URL = f"sqlite:///{DB_PATH}"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

from server.main import (
    Ingredient, Stock, Stocktake, Promotion
)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

ANOMALY_THRESHOLD = 0.10
QUICK_MODE_UNIT_COST = 50.0


def load_wecom_config() -> Optional[Dict]:
    if not CONFIG_PATH.exists():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    if "wecom" not in cfg:
        return None
    s = cfg["wecom"]
    return {
        "webhook_url": s.get("webhook_url", ""),
        "mentioned_mobile_list": [x.strip() for x in s.get("mentioned_mobile_list", "").split(",") if x.strip()]
    }


def push_wecom(webhook_url: str, content: str, mobiles: List[str] = None) -> bool:
    if not webhook_url:
        print("[企微] 未配置 webhook，跳过推送")
        return False
    try:
        import httpx
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": content}
        }
        if mobiles:
            payload["markdown"]["mentioned_mobile_list"] = mobiles
        resp = httpx.post(webhook_url, json=payload, timeout=15)
        result = resp.json()
        if result.get("errcode") == 0:
            print("[企微] ✅ 消息推送成功")
            return True
        else:
            print(f"[企微] ❌ 推送失败: {result}")
            return False
    except Exception as e:
        print(f"[企微] ❌ 推送异常: {e}")
        return False


def get_ingredient_list(db: Session, quick_mode: bool = False) -> List[Dict]:
    q = db.query(Ingredient)
    if quick_mode:
        q = q.filter(Ingredient.unit_cost > QUICK_MODE_UNIT_COST)
    items = q.order_by(Ingredient.category, Ingredient.id).all()
    result = []
    for i in items:
        stk = db.query(Stock).filter(Stock.ingredient_id == i.id).first()
        result.append({
            "ingredient_id": i.id,
            "barcode": i.barcode or "",
            "name": i.name,
            "category": i.category,
            "unit": i.unit,
            "unit_cost": i.unit_cost,
            "system_qty": round(stk.current_qty, 3) if stk else 0.0,
            "is_high_value": i.unit_cost > QUICK_MODE_UNIT_COST,
        })
    return result


def interactive_input(ingredients: List[Dict], store_id: str, quick_mode: bool) -> List[Dict]:
    print(f"\n{'='*70}")
    print(f"  每日盘存录入  |  门店: {store_id}  |  快速盘存: {'是 (仅高价值原料)' if quick_mode else '否 (全部原料)'}")
    print(f"  今日日期: {date.today().isoformat()}  |  录入项数: {len(ingredients)}")
    print(f"{'='*70}")
    print("\n提示: 直接回车 = 采用系统库存数量  |  输入数值 = 实际盘点数量\n")

    actuals = []
    for idx, ing in enumerate(ingredients, 1):
        mark = " 💎" if ing["is_high_value"] else ""
        while True:
            prompt = (f"[{idx}/{len(ingredients)}] {ing['name']} ({ing['category']}){mark}\n"
                      f"     系统库存: {ing['system_qty']:>10.3f} {ing['unit']}  |  单位成本: ¥{ing['unit_cost']:.2f}/单位\n"
                      f"     实际库存 (回车={ing['system_qty']:.3f}): ")
            raw = input(prompt).strip()
            if raw == "":
                actual = ing["system_qty"]
                break
            try:
                actual = float(raw)
                if actual < 0:
                    print("       ⚠️  数量不能为负，请重新输入")
                    continue
                break
            except ValueError:
                print("       ⚠️  输入无效，请输入数字")
                continue

        diff = round(actual - ing["system_qty"], 3)
        diff_rate = round(abs(diff) / ing["system_qty"], 4) if ing["system_qty"] > 0 else 0.0
        is_anom = " 🔴 异常!" if diff_rate > ANOMALY_THRESHOLD and ing["system_qty"] > 0 else ""
        print(f"     → 差值: {diff:>+10.3f} {ing['unit']}  差异率: {diff_rate*100:>6.2f}%{is_anom}\n")

        actuals.append({
            "ingredient_id": ing["ingredient_id"],
            "name": ing["name"],
            "category": ing["category"],
            "unit": ing["unit"],
            "unit_cost": ing["unit_cost"],
            "system_qty": ing["system_qty"],
            "actual_qty": round(actual, 3),
            "diff_qty": diff,
            "diff_rate": diff_rate,
            "is_anomaly": diff_rate > ANOMALY_THRESHOLD and ing["system_qty"] > 0,
            "diff_amount": round(diff * ing["unit_cost"], 2),
        })
    return actuals


def load_from_json(filepath: str, ingredients: List[Dict]) -> List[Dict]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    id_map = {i["ingredient_id"]: i for i in ingredients}
    id_barcode_map = {i["barcode"]: i for i in ingredients if i["barcode"]}
    actuals = []
    for entry in data:
        key = None
        if "ingredient_id" in entry:
            key = id_map.get(entry["ingredient_id"])
        elif "barcode" in entry and entry["barcode"] in id_barcode_map:
            key = id_barcode_map[entry["barcode"]]
        if not key:
            continue
        actual = float(entry.get("actual_qty", 0))
        diff = round(actual - key["system_qty"], 3)
        diff_rate = round(abs(diff) / key["system_qty"], 4) if key["system_qty"] > 0 else 0.0
        actuals.append({
            "ingredient_id": key["ingredient_id"],
            "name": key["name"],
            "category": key["category"],
            "unit": key["unit"],
            "unit_cost": key["unit_cost"],
            "system_qty": key["system_qty"],
            "actual_qty": round(actual, 3),
            "diff_qty": diff,
            "diff_rate": diff_rate,
            "is_anomaly": diff_rate > ANOMALY_THRESHOLD and key["system_qty"] > 0,
            "diff_amount": round(diff * key["unit_cost"], 2),
        })
    return actuals


def save_stocktake(db: Session, actuals: List[Dict], store_id: str, operator: str) -> List[Dict]:
    today = date.today()
    anomalies = []
    for a in actuals:
        st = Stocktake(
            ingredient_id=a["ingredient_id"],
            store_id=store_id,
            system_qty=a["system_qty"],
            actual_qty=a["actual_qty"],
            diff_qty=a["diff_qty"],
            diff_rate=a["diff_rate"],
            stocktake_date=today,
            operator=operator
        )
        db.add(st)
        if a["is_anomaly"]:
            anomalies.append(a)
    db.commit()
    return anomalies


def build_report(actuals: List[Dict], store_id: str, operator: str, anomalies: List[Dict]) -> Tuple[str, Dict]:
    today = date.today().isoformat()
    total_system = sum(a["system_qty"] * a["unit_cost"] for a in actuals)
    total_actual = sum(a["actual_qty"] * a["unit_cost"] for a in actuals)
    total_diff_amount = sum(a["diff_amount"] for a in actuals)
    anom_count = len(anomalies)
    over_count = sum(1 for a in actuals if a["diff_qty"] > 0)
    short_count = sum(1 for a in actuals if a["diff_qty"] < 0)
    match_count = sum(1 for a in actuals if a["diff_qty"] == 0)

    summary = {
        "date": today,
        "store_id": store_id,
        "operator": operator,
        "total_items": len(actuals),
        "anomaly_count": anom_count,
        "anomaly_rate": round(anom_count / len(actuals) * 100, 2) if actuals else 0,
        "total_system_value": round(total_system, 2),
        "total_actual_value": round(total_actual, 2),
        "total_diff_amount": round(total_diff_amount, 2),
        "over_count": over_count,
        "short_count": short_count,
        "match_count": match_count,
        "anomaly_threshold": f"{int(ANOMALY_THRESHOLD*100)}%",
    }

    md_lines = []
    md_lines.append(f"# 📋 每日盘存报告 - {store_id}")
    md_lines.append(f"> 盘存日期：**{today}**  操作人：**{operator or '未填'}**\n")
    md_lines.append("## 一、盘存汇总\n")
    md_lines.append("| 指标 | 数值 |")
    md_lines.append("|------|------|")
    md_lines.append(f"| 盘存原料总数 | {summary['total_items']} 项 |")
    md_lines.append(f"| 异常项数 (>{summary['anomaly_threshold']}) | {anom_count} 项 ({summary['anomaly_rate']:.1f}%) |")
    md_lines.append(f"| 账面总金额 | ¥ {total_system:,.2f} |")
    md_lines.append(f"| 实际总金额 | ¥ {total_actual:,.2f} |")
    md_lines.append(f"| 盈亏总金额 | ¥ {total_diff_amount:+,.2f} |")
    md_lines.append(f"| 盘盈 / 盘亏 / 一致 | {over_count} / {short_count} / {match_count} |")

    if anomalies:
        md_lines.append("\n## 二、🚨 盘盈/盘亏明细表 (差异>10%)\n")
        md_lines.append("| 原料名称 | 分类 | 账面 | 实际 | 差值 | 差异率 | 盈亏金额 | 方向 |")
        md_lines.append("|----------|------|------|------|------|--------|----------|------|")
        for a in sorted(anomalies, key=lambda x: x["diff_rate"], reverse=True):
            direction = "盘盈 🔺" if a["diff_qty"] > 0 else "盘亏 🔻"
            md_lines.append(
                f"| {a['name']} | {a['category']} | {a['system_qty']:.3f}{a['unit']} | "
                f"{a['actual_qty']:.3f}{a['unit']} | {a['diff_qty']:+.3f}{a['unit']} | "
                f"{a['diff_rate']*100:.1f}% | ¥{a['diff_amount']:+,.2f} | {direction} |"
            )

    md_lines.append("\n## 三、全部明细\n")
    md_lines.append("| # | 原料名称 | 账面 | 实际 | 差值 | 差异率 | 状态 |")
    md_lines.append("|---|----------|------|------|------|--------|------|")
    for i, a in enumerate(actuals, 1):
        if a["is_anomaly"]:
            status = "🔴 异常"
        elif a["diff_qty"] == 0:
            status = "✅ 一致"
        elif a["diff_rate"] > 0.05:
            status = "🟡 接近"
        else:
            status = "🟢 正常"
        md_lines.append(
            f"| {i} | {a['name']} | {a['system_qty']:.3f}{a['unit']} | "
            f"{a['actual_qty']:.3f}{a['unit']} | {a['diff_qty']:+.3f}{a['unit']} | "
            f"{a['diff_rate']*100:.1f}% | {status} |"
        )

    return "\n".join(md_lines), summary


def print_console_report(actuals: List[Dict], summary: Dict, anomalies: List[Dict]):
    print("\n" + "=" * 70)
    print(f"  盘存报告汇总  |  日期: {summary['date']}  门店: {summary['store_id']}")
    print("=" * 70)
    print(f"  盘存项数:      {summary['total_items']:>10} 项")
    print(f"  异常项数:      {summary['anomaly_count']:>10} 项 ({summary['anomaly_rate']:.1f}%)")
    print(f"  账面总金额: ¥ {summary['total_system_value']:>12,.2f}")
    print(f"  实际总金额: ¥ {summary['total_actual_value']:>12,.2f}")
    print(f"  盈亏总金额: ¥ {summary['total_diff_amount']:>+12,.2f}")
    print(f"  盘盈/盘亏/一致: {summary['over_count']} / {summary['short_count']} / {summary['match_count']}")
    print("-" * 70)

    if anomalies:
        print("\n🚨 【盘盈/盘亏明细 - 差异>10%】")
        tbl = []
        for a in sorted(anomalies, key=lambda x: x["diff_rate"], reverse=True):
            direction = "盘盈" if a["diff_qty"] > 0 else "盘亏"
            tbl.append([
                a["name"], a["category"],
                f"{a['system_qty']:.3f}{a['unit']}",
                f"{a['actual_qty']:.3f}{a['unit']}",
                f"{a['diff_qty']:+.3f}{a['unit']}",
                f"{a['diff_rate']*100:.1f}%",
                f"¥{a['diff_amount']:+,.2f}",
                direction
            ])
        print(tabulate(tbl, headers=["名称", "分类", "账面", "实际", "差值", "差异率", "金额", "方向"], tablefmt="github"))

    print("=" * 70)


def export_excel(actuals: List[Dict], summary: Dict, prefix: str) -> str:
    excel_path = REPORT_DIR / f"{prefix}_stocktake.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        pd.DataFrame([summary]).T.to_excel(writer, sheet_name="汇总", header=False)
        df = pd.DataFrame(actuals)
        df.to_excel(writer, sheet_name="明细", index=False)
    return str(excel_path)


def build_wecom_message(summary: Dict, anomalies: List[Dict], store_id: str) -> str:
    today = summary["date"]
    lines = [f"# 📋 每日盘存 - {store_id}"]
    lines.append(f"> 日期：{today}  操作人：{summary['operator'] or '未填'}")
    lines.append(f"\n**异常项数：<font color=\"warning\">{summary['anomaly_count']}</font> / {summary['total_items']} 项 ({summary['anomaly_rate']:.1f}%)**")
    lines.append(f"> 盈亏总金额：**¥ {summary['total_diff_amount']:+,.2f}**")
    lines.append(f"> 账面：¥{summary['total_system_value']:,.2f}  实际：¥{summary['total_actual_value']:,.2f}")
    lines.append(f"> 盘盈 {summary['over_count']} / 盘亏 {summary['short_count']} / 一致 {summary['match_count']}")

    if anomalies:
        lines.append("\n<font color=\"warning\">**🚨 差异明细 (>10%)**</font>")
        for a in sorted(anomalies, key=lambda x: x["diff_rate"], reverse=True)[:15]:
            d = "盘盈" if a["diff_qty"] > 0 else "盘亏"
            color = "info" if a["diff_qty"] > 0 else "warning"
            lines.append(
                f"> **{a['name']}** ({a['category']})  <font color=\"{color}\">{d}</font>\n"
                f"> 账面 {a['system_qty']:.2f}{a['unit']} → 实际 {a['actual_qty']:.2f}{a['unit']}  "
                f"差 <font color=\"warning\">{a['diff_rate']*100:.1f}%</font>  ¥{a['diff_amount']:+,.2f}"
            )
        if len(anomalies) > 15:
            lines.append(f"> ... 另有 {len(anomalies) - 15} 项异常，详见报告附件")
    else:
        lines.append("\n<font color=\"info\">✅ 本次盘存无异常，所有原料差异均在阈值内</font>")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="餐饮后厨每日盘存脚本")
    parser.add_argument("--store", type=str, default="central", help="门店ID (默认 central)")
    parser.add_argument("--operator", type=str, default="", help="操作人姓名")
    parser.add_argument("--quick", action="store_true", help=f"快速盘存模式 - 只盘单位成本>¥{QUICK_MODE_UNIT_COST}的高价值原料")
    parser.add_argument("--input", type=str, help="从 JSON 文件导入盘点数据 (格式: [{ingredient_id, actual_qty}])")
    parser.add_argument("--push-wecom", action="store_true", help="推送异常明细到企业微信群 (需配置 config.ini)")
    parser.add_argument("--list", action="store_true", help="仅打印待盘存原料清单，不执行录入")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        ingredients = get_ingredient_list(db, quick_mode=args.quick)
        if not ingredients:
            print("❌ 无可用原料，请先在系统中添加原料")
            return

        if args.list:
            print(f"\n📋 待盘存原料清单 ({len(ingredients)} 项，快速盘存: {'是' if args.quick else '否'})")
            tbl = []
            for i in ingredients:
                mark = "💎高价值" if i["is_high_value"] else ""
                tbl.append([i["ingredient_id"], i["barcode"], i["name"], i["category"], f"{i['system_qty']:.3f}{i['unit']}", f"¥{i['unit_cost']:.2f}", mark])
            print(tabulate(tbl, headers=["ID", "条码", "名称", "分类", "系统库存", "单位成本", "标记"], tablefmt="github"))
            return

        if args.input:
            if not Path(args.input).exists():
                print(f"❌ 输入文件不存在: {args.input}")
                return
            actuals = load_from_json(args.input, ingredients)
            print(f"📥 从 {args.input} 导入了 {len(actuals)} 条盘点数据")
        else:
            actuals = interactive_input(ingredients, args.store, args.quick)

        if not actuals:
            print("❌ 无盘点数据")
            return

        anomalies = save_stocktake(db, actuals, args.store, args.operator)
        md_content, summary = build_report(actuals, args.store, args.operator, anomalies)

        prefix = f"stocktake_{args.store}_{date.today().strftime('%Y%m%d')}_{datetime.now().strftime('%H%M%S')}"
        md_path = REPORT_DIR / f"{prefix}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        excel_path = export_excel(actuals, summary, prefix)

        print_console_report(actuals, summary, anomalies)
        print(f"📄 Markdown 报告: {md_path}")
        print(f"📊 Excel 明细: {excel_path}")

        if args.push_wecom:
            cfg = load_wecom_config()
            if cfg and cfg["webhook_url"]:
                msg = build_wecom_message(summary, anomalies, args.store)
                push_wecom(cfg["webhook_url"], msg, cfg.get("mentioned_mobile_list"))

    finally:
        db.close()


if __name__ == "__main__":
    main()

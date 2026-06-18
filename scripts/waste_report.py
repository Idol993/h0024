import sys
import os
import smtplib
import json
import argparse
import configparser
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams
from sqlalchemy import create_engine, and_, func
from sqlalchemy.orm import sessionmaker, Session
from tabulate import tabulate

rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
DB_PATH = BASE_DIR / "data" / "kitchen.db"
REPORT_DIR = BASE_DIR / "reports"
CONFIG_PATH = BASE_DIR / "config.ini"
DATABASE_URL = f"sqlite:///{DB_PATH}"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

from server.main import (
    Waste, Ingredient, Stock, Outbound, Order
)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


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


def get_date_range(period: str = "last_week") -> tuple:
    today = date.today()
    if period == "last_week":
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=6)
    elif period == "this_week":
        start = today - timedelta(days=today.weekday())
        end = today
    elif period == "last_month":
        first = today.replace(day=1)
        end = first - timedelta(days=1)
        start = end.replace(day=1)
    elif period == "this_month":
        start = today.replace(day=1)
        end = today
    elif period == "7d":
        end = today
        start = today - timedelta(days=6)
    elif period == "30d":
        end = today
        start = today - timedelta(days=29)
    else:
        end = today
        start = today - timedelta(days=6)
    return start, end


def load_data(db: Session, start: date, end: date) -> Dict[str, pd.DataFrame]:
    extended_start = start - timedelta(days=(end - start).days + 1)

    wastes = db.query(Waste).filter(
        and_(Waste.waste_date >= extended_start, Waste.waste_date <= end)
    ).all()
    waste_data = []
    for w in wastes:
        ing = w.ingredient
        waste_data.append({
            "id": w.id,
            "ingredient_id": w.ingredient_id,
            "ingredient_name": ing.name if ing else "未知",
            "category": ing.category if ing else "其他",
            "unit": ing.unit if ing else "",
            "store_id": w.store_id,
            "waste_qty": w.waste_qty,
            "waste_reason": w.waste_reason,
            "unit_price": w.unit_price,
            "waste_amount": w.waste_amount,
            "waste_date": w.waste_date,
            "year_month": w.waste_date.strftime("%Y-%m"),
            "week": w.waste_date.isocalendar()[1],
        })
    df_waste = pd.DataFrame(waste_data)

    outbounds = db.query(Outbound).filter(
        and_(Outbound.outbound_date >= extended_start, Outbound.outbound_date <= end)
    ).all()
    ob_data = []
    for o in outbounds:
        ing = o.ingredient
        ob_data.append({
            "ingredient_id": o.ingredient_id,
            "ingredient_name": ing.name if ing else "未知",
            "category": ing.category if ing else "其他",
            "store_id": o.store_id,
            "qty": o.qty,
            "unit_cost": ing.unit_cost if ing else 0,
            "outbound_date": o.outbound_date,
        })
    df_ob = pd.DataFrame(ob_data)

    stocks = db.query(Stock).all()
    stk_data = []
    for s in stocks:
        ing = s.ingredient
        stk_data.append({
            "ingredient_id": s.ingredient_id,
            "ingredient_name": ing.name if ing else "未知",
            "category": ing.category if ing else "其他",
            "unit_cost": ing.unit_cost if ing else 0,
            "current_qty": s.current_qty,
            "stock_value": s.current_qty * s.last_unit_price,
        })
    df_stock = pd.DataFrame(stk_data)

    return {"waste": df_waste, "outbound": df_ob, "stock": df_stock, "report_start": start, "report_end": end}


def analyze(dfs: Dict[str, pd.DataFrame], start: date, end: date) -> Dict:
    df_w_all = dfs["waste"]
    df_ob_all = dfs["outbound"]

    if not df_ob_all.empty:
        df_ob_all = df_ob_all.copy()
        df_ob_all["value"] = df_ob_all["qty"] * df_ob_all["unit_cost"]

    if not df_w_all.empty:
        df_w = df_w_all[(df_w_all["waste_date"] >= start) & (df_w_all["waste_date"] <= end)].copy()
    else:
        df_w = df_w_all

    if not df_ob_all.empty:
        df_ob = df_ob_all[(df_ob_all["outbound_date"] >= start) & (df_ob_all["outbound_date"] <= end)].copy()
    else:
        df_ob = df_ob_all
    result = {
        "period": f"{start.isoformat()} ~ {end.isoformat()}",
        "period_start": start,
        "period_end": end,
        "days": (end - start).days + 1,
        "total_waste_amount": 0.0,
        "total_waste_qty": 0.0,
        "total_waste_count": 0,
        "total_outbound_value": 0.0,
        "overall_waste_rate": 0.0,
        "_df_waste": df_w,
        "_df_outbound": df_ob,
    }

    if df_w.empty and df_ob.empty:
        return result

    if not df_w.empty:
        result["total_waste_amount"] = round(df_w["waste_amount"].sum(), 2)
        result["total_waste_qty"] = round(df_w["waste_qty"].sum(), 3)
        result["total_waste_count"] = len(df_w)

    if not df_ob.empty:
        df_ob["value"] = df_ob["qty"] * df_ob["unit_cost"]
        result["total_outbound_value"] = round(df_ob["value"].sum(), 2)
        if result["total_outbound_value"] > 0:
            result["overall_waste_rate"] = round(
                result["total_waste_amount"] / result["total_outbound_value"] * 100, 2
            )

    if not df_w.empty:
        by_cat = df_w.groupby("category", as_index=False).agg(
            waste_amount=("waste_amount", "sum"),
            waste_qty=("waste_qty", "sum"),
            count=("id", "count")
        ).sort_values("waste_amount", ascending=False)
        by_cat["pct"] = round(by_cat["waste_amount"] / by_cat["waste_amount"].sum() * 100, 2) if by_cat["waste_amount"].sum() > 0 else 0
        result["by_category"] = by_cat.to_dict("records")

        by_reason = df_w.groupby("waste_reason", as_index=False).agg(
            waste_amount=("waste_amount", "sum"),
            waste_qty=("waste_qty", "sum"),
            count=("id", "count")
        ).sort_values("waste_amount", ascending=False)
        by_reason["pct"] = round(by_reason["waste_amount"] / by_reason["waste_amount"].sum() * 100, 2) if by_reason["waste_amount"].sum() > 0 else 0
        result["by_reason"] = by_reason.to_dict("records")

        by_month = df_w.groupby("year_month", as_index=False).agg(
            waste_amount=("waste_amount", "sum"),
            waste_qty=("waste_qty", "sum"),
            count=("id", "count")
        ).sort_values("year_month")
        result["by_month"] = by_month.to_dict("records")

        top10 = df_w.groupby(["ingredient_id", "ingredient_name", "category", "unit"], as_index=False).agg(
            waste_amount=("waste_amount", "sum"),
            waste_qty=("waste_qty", "sum"),
            count=("id", "count")
        ).sort_values("waste_amount", ascending=False).head(10)
        result["top10_ingredients"] = top10.to_dict("records")

        store_data = []
        all_stores = list(set(df_w["store_id"].tolist() + df_ob["store_id"].tolist())) if not df_ob.empty else df_w["store_id"].tolist()
        for sid in all_stores:
            w_val = df_w[df_w["store_id"] == sid]["waste_amount"].sum() if not df_w.empty else 0
            ob_val = 0
            if not df_ob.empty:
                ob_val = df_ob[df_ob["store_id"] == sid]["value"].sum()
            rate = round(w_val / ob_val * 100, 2) if ob_val > 0 else 0
            store_data.append({
                "store_id": sid,
                "waste_amount": round(w_val, 2),
                "outbound_value": round(ob_val, 2),
                "waste_rate": rate,
                "flagged_high": rate > 5
            })
        result["by_store"] = sorted(store_data, key=lambda x: x["waste_rate"], reverse=True)

        result["trend_daily"] = df_w.groupby("waste_date", as_index=False).agg(
            waste_amount=("waste_amount", "sum")
        ).sort_values("waste_date").to_dict("records")

        flagged_stores = []
        prev_start = start - timedelta(days=(end - start).days + 1)
        prev_end = start - timedelta(days=1)
        for s_data in result["by_store"]:
            if s_data["waste_rate"] <= 5:
                continue
            prev_w_val = 0
            prev_ob_val = 0
            if not df_w_all.empty:
                prev_w_val = df_w_all[
                    (df_w_all["store_id"] == s_data["store_id"]) &
                    (df_w_all["waste_date"] >= prev_start) &
                    (df_w_all["waste_date"] <= prev_end)
                ]["waste_amount"].sum()
            if not df_ob_all.empty:
                prev_ob_val = df_ob_all[
                    (df_ob_all["store_id"] == s_data["store_id"]) &
                    (df_ob_all["outbound_date"] >= prev_start) &
                    (df_ob_all["outbound_date"] <= prev_end)
                ]["value"].sum()
            prev_rate = round(prev_w_val / prev_ob_val * 100, 2) if prev_ob_val > 0 else 0
            if prev_rate > 5:
                flagged_stores.append({
                    **s_data,
                    "prev_period": f"{prev_start.isoformat()} ~ {prev_end.isoformat()}",
                    "prev_waste_rate": prev_rate,
                    "prev_waste_amount": round(prev_w_val, 2),
                    "prev_outbound_value": round(prev_ob_val, 2),
                    "consecutive_high": True
                })
        result["flagged_stores"] = flagged_stores

    return result


def make_charts(dfs: Dict[str, pd.DataFrame], result: Dict, report_dir: Path, prefix: str) -> List[str]:
    chart_files = []
    df_w = dfs["waste"]

    if df_w.empty:
        return chart_files

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle(f"损耗分析报告 ({result['period']})", fontsize=14, fontweight="bold")

    if "by_category" in result and result["by_category"]:
        cats = [x["category"] for x in result["by_category"]]
        vals = [x["waste_amount"] for x in result["by_category"]]
        axes[0, 0].bar(cats, vals, color="#FF6B6B", edgecolor="white")
        axes[0, 0].set_title("按原料分类损耗金额 (元)")
        axes[0, 0].tick_params(axis="x", rotation=30)
        for i, v in enumerate(vals):
            axes[0, 0].text(i, v, f"¥{v:,.0f}", ha="center", va="bottom", fontsize=8)

    if "by_reason" in result and result["by_reason"]:
        reasons = [x["waste_reason"] for x in result["by_reason"]]
        vals2 = [x["waste_amount"] for x in result["by_reason"]]
        colors = ["#FF9F43", "#54A0FF", "#5F27CD", "#1DD1A1", "#FF6B6B", "#FECA57"]
        axes[0, 1].pie(vals2, labels=reasons, autopct="%1.1f%%", colors=colors[:len(reasons)], startangle=90)
        axes[0, 1].set_title("按损耗原因占比")

    if "trend_daily" in result and result["trend_daily"]:
        dates = [x["waste_date"].strftime("%m-%d") if isinstance(x["waste_date"], date) else str(x["waste_date"]) for x in result["trend_daily"]]
        vals3 = [x["waste_amount"] for x in result["trend_daily"]]
        axes[1, 0].plot(dates, vals3, marker="o", color="#EE5A24", linewidth=2)
        axes[1, 0].fill_between(dates, vals3, alpha=0.2, color="#FF6B6B")
        axes[1, 0].set_title("损耗金额趋势 (每日)")
        axes[1, 0].tick_params(axis="x", rotation=45)
        axes[1, 0].grid(True, alpha=0.3)

    if "top10_ingredients" in result and result["top10_ingredients"]:
        names = [x["ingredient_name"] for x in result["top10_ingredients"]][::-1]
        vals4 = [x["waste_amount"] for x in result["top10_ingredients"]][::-1]
        bars = axes[1, 1].barh(names, vals4, color="#0ABDE3")
        axes[1, 1].set_title("损耗金额 TOP10 原料 (元)")
        for bar, v in zip(bars, vals4):
            axes[1, 1].text(v, bar.get_y() + bar.get_height() / 2, f"¥{v:,.0f}", va="center", fontsize=8)

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    chart_path = report_dir / f"{prefix}_waste_analysis.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    plt.close()
    chart_files.append(str(chart_path))

    if "by_store" in result and result["by_store"]:
        fig2, ax = plt.subplots(figsize=(10, 6))
        stores = [x["store_id"] for x in result["by_store"]]
        rates = [x["waste_rate"] for x in result["by_store"]]
        colors = ["#FF3838" if r > 5 else "#26de81" for r in rates]
        bars = ax.bar(stores, rates, color=colors, edgecolor="white")
        ax.axhline(y=5, color="red", linestyle="--", linewidth=1.5, label="警戒线 5%")
        ax.set_title(f"各门店损耗率对比 ({result['period']})")
        ax.set_ylabel("损耗率 (%)")
        ax.legend()
        for bar, v in zip(bars, rates):
            ax.text(bar.get_x() + bar.get_width() / 2, v, f"{v:.1f}%", ha="center", va="bottom", fontsize=9)
        plt.xticks(rotation=30)
        plt.tight_layout()
        chart2 = report_dir / f"{prefix}_store_comparison.png"
        plt.savefig(chart2, dpi=150, bbox_inches="tight")
        plt.close()
        chart_files.append(str(chart2))

    return chart_files


def export_excel(dfs: Dict[str, pd.DataFrame], result: Dict, report_dir: Path, prefix: str) -> str:
    df_w = result.get("_df_waste", dfs["waste"])
    excel_path = report_dir / f"{prefix}_waste_report.xlsx"
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        summary_df = pd.DataFrame([{
            "统计周期": result["period"],
            "天数": result["days"],
            "损耗总金额(元)": result["total_waste_amount"],
            "损耗总数量": result["total_waste_qty"],
            "损耗记录数": result["total_waste_count"],
            "出库总金额(元)": result["total_outbound_value"],
            "综合损耗率(%)": result["overall_waste_rate"],
        }])
        summary_df.T.to_excel(writer, sheet_name="汇总", header=False)

        if not df_w.empty:
            df_w.to_excel(writer, sheet_name="明细", index=False)

        if "by_category" in result and result["by_category"]:
            pd.DataFrame(result["by_category"]).to_excel(writer, sheet_name="分类汇总", index=False)
        if "by_reason" in result and result["by_reason"]:
            pd.DataFrame(result["by_reason"]).to_excel(writer, sheet_name="原因汇总", index=False)
        if "top10_ingredients" in result and result["top10_ingredients"]:
            pd.DataFrame(result["top10_ingredients"]).to_excel(writer, sheet_name="TOP10原料", index=False)
        if "by_store" in result and result["by_store"]:
            pd.DataFrame(result["by_store"]).to_excel(writer, sheet_name="门店对比", index=False)
        if "by_month" in result and result["by_month"]:
            pd.DataFrame(result["by_month"]).to_excel(writer, sheet_name="月度趋势", index=False)
    return str(excel_path)


def build_markdown(result: Dict, chart_files: List[str], excel_path: str, prefix: str) -> str:
    lines = []
    lines.append(f"# 餐饮后厨损耗分析报告\n")
    lines.append(f"> 统计周期：**{result['period']}**  （共 {result['days']} 天）\n")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    lines.append("\n## 一、关键指标\n")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 损耗总金额 | ¥ {result['total_waste_amount']:,.2f} |")
    lines.append(f"| 损耗总数量 | {result['total_waste_qty']:,.3f} |")
    lines.append(f"| 损耗记录数 | {result['total_waste_count']} 条 |")
    lines.append(f"| 出库总金额 | ¥ {result['total_outbound_value']:,.2f} |")
    lines.append(f"| **综合损耗率** | **{result['overall_waste_rate']:.2f}%** |")

    lines.append("\n## 二、损耗金额 TOP10 原料\n")
    if "top10_ingredients" in result and result["top10_ingredients"]:
        lines.append("| 排名 | 原料名称 | 分类 | 损耗量 | 损耗金额 | 占比 |")
        lines.append("|------|----------|------|--------|----------|------|")
        total = result["total_waste_amount"] or 1
        for i, x in enumerate(result["top10_ingredients"], 1):
            pct = x["waste_amount"] / total * 100 if total > 0 else 0
            lines.append(f"| {i} | {x['ingredient_name']} | {x['category']} | {x['waste_qty']:,.3f} {x['unit']} | ¥ {x['waste_amount']:,.2f} | {pct:.1f}% |")
    else:
        lines.append("*暂无损耗记录*")

    lines.append("\n## 三、按分类维度聚合\n")
    if "by_category" in result and result["by_category"]:
        lines.append("| 分类 | 损耗金额 | 占比 | 损耗次数 |")
        lines.append("|------|----------|------|----------|")
        for x in result["by_category"]:
            lines.append(f"| {x['category']} | ¥ {x['waste_amount']:,.2f} | {x['pct']:.1f}% | {x['count']} |")

    lines.append("\n## 四、按损耗原因维度聚合\n")
    if "by_reason" in result and result["by_reason"]:
        lines.append("| 原因 | 损耗金额 | 占比 | 次数 |")
        lines.append("|------|----------|------|------|")
        for x in result["by_reason"]:
            lines.append(f"| {x['waste_reason']} | ¥ {x['waste_amount']:,.2f} | {x['pct']:.1f}% | {x['count']} |")

    lines.append("\n## 五、门店维度对比\n")
    if "by_store" in result and result["by_store"]:
        lines.append("| 门店 | 损耗金额 | 出库金额 | 损耗率 | 状态 |")
        lines.append("|------|----------|----------|--------|------|")
        for x in result["by_store"]:
            flag = "🔴 **超标**" if x["flagged_high"] else "✅ 正常"
            lines.append(f"| {x['store_id']} | ¥ {x['waste_amount']:,.2f} | ¥ {x['outbound_value']:,.2f} | {x['waste_rate']:.2f}% | {flag} |")

    if "flagged_stores" in result and result["flagged_stores"]:
        lines.append("\n### ⚠️ 连续两周高损耗门店预警\n")
        lines.append("| 门店 | 当期损耗率 | 上期损耗率 | 上期周期 | 当期损耗金额 | 上期损耗金额 |")
        lines.append("|------|------------|------------|----------|--------------|--------------|")
        for x in result["flagged_stores"]:
            lines.append(
                f"| {x['store_id']} | {x['waste_rate']:.2f}% 🔴 | {x['prev_waste_rate']:.2f}% 🔴 | "
                f"{x['prev_period']} | ¥{x['waste_amount']:,.2f} | ¥{x['prev_waste_amount']:,.2f} |"
            )
        lines.append("\n> 上述门店连续两周损耗率均超过5%警戒线，**建议管理组现场核查**")

    lines.append("\n## 六、月度趋势\n")
    if "by_month" in result and result["by_month"]:
        lines.append("| 月份 | 损耗金额 | 损耗量 | 记录数 |")
        lines.append("|------|----------|--------|--------|")
        for x in result["by_month"]:
            lines.append(f"| {x['year_month']} | ¥ {x['waste_amount']:,.2f} | {x['waste_qty']:,.3f} | {x['count']} |")

    if chart_files:
        lines.append("\n## 七、图表分析\n")
        for cf in chart_files:
            rel = Path(cf).name
            lines.append(f"![分析图]({rel})\n")

    lines.append("\n---\n")
    lines.append(f"\n📎 **附件**：明细数据 Excel - [{Path(excel_path).name}]({Path(excel_path).name})\n")

    return "\n".join(lines)


def send_email(md_path: Path, attachments: List[str], cfg: Dict, subject: str):
    if not cfg or not cfg["host"] or not cfg["recipients"]:
        print("[邮件] SMTP 未配置或无收件人，跳过发送")
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["sender"]
        msg["To"] = ", ".join(cfg["recipients"])
        msg["Subject"] = subject

        with open(md_path, "r", encoding="utf-8") as f:
            html_body = f.read()
        import re
        html_body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_body)
        html_body = re.sub(r"\n", "<br>", html_body)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        for fp in attachments:
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
        print(f"[邮件] ✅ 报告已发送至: {', '.join(cfg['recipients'])}")
        return True
    except Exception as e:
        print(f"[邮件] ❌ 发送失败: {e}")
        return False


def print_console_summary(result: Dict):
    print("\n" + "=" * 70)
    print(f"  餐饮后厨损耗分析报告  |  周期: {result['period']}  ({result['days']}天)")
    print("=" * 70)
    print(f"  损耗总金额:  ¥ {result['total_waste_amount']:>12,.2f}")
    print(f"  出库总金额:  ¥ {result['total_outbound_value']:>12,.2f}")
    print(f"  综合损耗率:     {result['overall_waste_rate']:>10.2f} %")
    print("-" * 70)

    if "top10_ingredients" in result and result["top10_ingredients"]:
        print("\n【损耗金额 TOP10 原料】")
        tbl = []
        for i, x in enumerate(result["top10_ingredients"], 1):
            tbl.append([i, x["ingredient_name"], x["category"], f"{x['waste_qty']:,.3f}{x['unit']}", f"¥{x['waste_amount']:,.2f}"])
        print(tabulate(tbl, headers=["#", "名称", "分类", "损耗量", "金额"], tablefmt="github"))

    if "by_reason" in result and result["by_reason"]:
        print("\n【按损耗原因】")
        tbl = [[x["waste_reason"], f"¥{x['waste_amount']:,.2f}", f"{x['pct']:.1f}%"] for x in result["by_reason"]]
        print(tabulate(tbl, headers=["原因", "金额", "占比"], tablefmt="github"))

    if "flagged_stores" in result and result["flagged_stores"]:
        print("\n⚠️  【高损耗预警门店 - 连续两周 > 5%】")
        tbl = []
        for s in result["flagged_stores"]:
            tbl.append([s["store_id"], f"{s['waste_rate']:.2f}%", f"{s['prev_waste_rate']:.2f}%", s["prev_period"], f"¥{s['waste_amount']:,.2f}", f"¥{s['prev_waste_amount']:,.2f}"])
        print(tabulate(tbl, headers=["门店", "当期损耗率", "上期损耗率", "上期周期", "当期金额", "上期金额"], tablefmt="github"))

    if "by_store" in result and result["by_store"]:
        print("\n【各门店损耗率】")
        tbl = []
        for x in result["by_store"]:
            flag = "🔴超标" if x["flagged_high"] else "✅正常"
            tbl.append([x["store_id"], f"¥{x['waste_amount']:,.2f}", f"¥{x['outbound_value']:,.2f}", f"{x['waste_rate']:.2f}%", flag])
        print(tabulate(tbl, headers=["门店", "损耗金额", "出库金额", "损耗率", "状态"], tablefmt="github"))

    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="餐饮后厨损耗分析报告脚本")
    parser.add_argument("--period", type=str, default="last_week",
                        choices=["last_week", "this_week", "last_month", "this_month", "7d", "30d"],
                        help="统计周期（默认上周）")
    parser.add_argument("--start", type=str, help="自定义开始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="自定义结束日期 YYYY-MM-DD")
    parser.add_argument("--send-email", action="store_true", help="通过 SMTP 发送邮件（需配置 config.ini）")
    parser.add_argument("--subject", type=str, help="邮件主题前缀")
    args = parser.parse_args()

    if args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        start, end = get_date_range(args.period)

    db = SessionLocal()
    try:
        dfs = load_data(db, start, end)
        result = analyze(dfs, start, end)

        prefix = f"waste_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
        chart_files = make_charts(dfs, result, REPORT_DIR, prefix)
        excel_path = export_excel(dfs, result, REPORT_DIR, prefix)

        md_content = build_markdown(result, chart_files, excel_path, prefix)
        md_path = REPORT_DIR / f"{prefix}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        print_console_summary(result)
        print(f"📄 Markdown 报告: {md_path}")
        print(f"📊 数据附件: {excel_path}")
        for cf in chart_files:
            print(f"📈 分析图表: {cf}")

        if args.send_email:
            cfg = load_smtp_config()
            subj_prefix = args.subject or f"[损耗报告] {result['period']}"
            subj = f"{subj_prefix} | 综合损耗率 {result['overall_waste_rate']:.2f}% | 金额¥{result['total_waste_amount']:,.2f}"
            all_attach = [excel_path, md_path] + chart_files
            send_email(md_path, all_attach, cfg, subj)

    finally:
        db.close()


if __name__ == "__main__":
    main()

import sys
from pathlib import Path
from datetime import datetime, date, timedelta

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

print("=== 验证 waste_report.py 核心功能 ===")
from sqlalchemy.orm import Session
from server.main import SessionLocal
from scripts.waste_report import load_data, analyze, make_charts, export_excel, build_markdown, print_console_summary, REPORT_DIR

db = SessionLocal()
try:
    end = date.today()
    start = end - timedelta(days=6)
    dfs = load_data(db, start, end)
    result = analyze(dfs, start, end)
    prefix = f"waste_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
    chart_files = make_charts(dfs, result, REPORT_DIR, prefix)
    excel_path = export_excel(dfs, result, REPORT_DIR, prefix)
    md = build_markdown(result, chart_files, excel_path, prefix)
    md_path = REPORT_DIR / f"{prefix}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print_console_summary(result)
    print(f"  📄 报告生成: {md_path}")
    print(f"  📊 Excel: {excel_path}")
    for c in chart_files:
        print(f"  📈 图表: {c}")
finally:
    db.close()

print("\n=== 验证 daily_stocktake.py 核心功能 (清单模式) ===")
from scripts.daily_stocktake import get_ingredient_list, build_report, export_excel as st_export, REPORT_DIR as ST_REPORT

db = SessionLocal()
try:
    ings = get_ingredient_list(db, quick_mode=False)
    print(f"  待盘存原料: {len(ings)} 项")
    actuals = []
    import random
    random.seed(42)
    for i in ings:
        system = i["system_qty"]
        if system > 0:
            drift_pct = random.uniform(-0.15, 0.15)
            actual = round(system * (1 + drift_pct), 3)
        else:
            actual = 0
        diff = round(actual - system, 3)
        rate = round(abs(diff)/system, 4) if system > 0 else 0
        actuals.append({
            "ingredient_id": i["ingredient_id"], "name": i["name"], "category": i["category"],
            "unit": i["unit"], "unit_cost": i["unit_cost"],
            "system_qty": system, "actual_qty": actual,
            "diff_qty": diff, "diff_rate": rate,
            "is_anomaly": rate > 0.10 and system > 0,
            "diff_amount": round(diff * i["unit_cost"], 2),
        })
    anomalies = [a for a in actuals if a["is_anomaly"]]
    md, summary = build_report(actuals, "central", "测试", anomalies)
    print(f"  异常项: {len(anomalies)} 项 ({summary['anomaly_rate']:.1f}%)")
    print(f"  盘盈/盘亏/一致: {summary['over_count']}/{summary['short_count']}/{summary['match_count']}")
    prefix2 = f"stocktake_central_{date.today().strftime('%Y%m%d')}_test"
    md_path2 = ST_REPORT / f"{prefix2}.md"
    with open(md_path2, "w", encoding="utf-8") as f:
        f.write(md)
    excel2 = st_export(actuals, summary, prefix2)
    print(f"  📄 盘存报告: {md_path2}")
    print(f"  📊 盘存Excel: {excel2}")
finally:
    db.close()

print("\n✅ 所有脚本核心功能验证通过！")

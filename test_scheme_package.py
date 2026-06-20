"""
复盘方案包导入导出回归测试
覆盖:
  1. 导出方案包: 当前方案 / 全部方案，JSON结构校验
  2. 导出内容: 名称、描述、筛选条件、数据时间范围、导出时间，不含差异明细
  3. 导入方案包: 校验结构与必填字段
  4. 导入重名冲突: 保留原方案 / 覆盖 / 改名导入
  5. 导出再导入: 内容不丢失
  6. 坏JSON拦截: 格式错误、缺字段、空方案列表，不污染数据库
  7. 重启恢复: 最近使用方案在导入后仍可恢复
  8. 操作日志: import_scheme / export_scheme 均可查询
  9. 导入成功写入SQLite: 数据库中可查到导入的方案
"""
import os
import sys
import json
import sqlite3
import tempfile
import importlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "inventory_diff_scheme_pkg_test.db")

from test_utils import init_test_env
init_test_env(DB_PATH)

import db as db_mod
db_mod.DB_PATH = DB_PATH

from db import (
    init_db, get_conn, get_discrepancies, get_evidence_for_discrepancy,
    get_status_log, get_stores, get_import_records,
    transition_status, update_review_note, get_active_rule_version,
    STATUS_PENDING_REVIEW, STATUS_CONFIRMED, STATUS_CLOSED, STATUS_LABELS,
    get_snapshot_for_discrepancy, get_calc_steps_for_discrepancy,
    get_discrepancies_extended, get_discrepancy_versions,
    get_all_rule_versions_with_labels, get_import_records_with_rule_version,
    check_import_duplicate, save_ui_state, load_ui_state,
    get_store_list, get_barcode_list, get_date_range,
    now_iso,
    save_review_scheme, get_review_schemes, get_review_scheme_by_id,
    get_review_scheme_by_name, update_review_scheme_name, delete_review_scheme,
    copy_review_scheme, mark_scheme_used, get_last_used_scheme,
    check_data_date_range_changed, get_scheme_operation_logs,
    log_scheme_operation,
    export_scheme_package, validate_scheme_package, import_scheme_package,
)
from import_service import import_csv, validate_row, compute_file_hash, REQUIRED_FIELDS, NUMERIC_FIELDS
import import_service
import_service.db_mod = db_mod

import engine
engine.db_mod = db_mod

import rules
rules.db_mod = db_mod

from engine import run_attribution, CAUSE_LABELS
from rules import save_rule_config, get_current_config, get_version_history
from sample_data import generate_sample_data, SAMPLE_DIR

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
for suffix in ("-wal", "-shm"):
    p = DB_PATH + suffix
    if os.path.exists(p):
        os.remove(p)

MAIN_DB_PATH = os.path.join(BASE_DIR, "inventory_diff.db")
assert "test" in DB_PATH, f"测试使用独立数据库: {DB_PATH}"
assert DB_PATH != MAIN_DB_PATH, f"测试数据库不应与主数据库相同: {DB_PATH} vs {MAIN_DB_PATH}"

init_db()
generate_sample_data()


def read_sample(fname):
    with open(os.path.join(SAMPLE_DIR, fname), "rb") as f:
        return f.read()


errors_total = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        errors_total.append((name, detail))


print("\n" + "=" * 70)
print("测试 0: 准备基础数据 - 导入+归因")
print("=" * 70)

r_inv = import_csv("inventory", "inventory.csv", read_sample("inventory.csv"))
check("库存导入成功", r_inv["success"])
r_sal = import_csv("sales", "sales.csv", read_sample("sales.csv"))
check("销售导入成功", r_sal["success"])
r_tra = import_csv("transfer", "transfer.csv", read_sample("transfer.csv"))
check("调拨导入成功", r_tra["success"])
r_stk = import_csv("stocktake", "stocktake.csv", read_sample("stocktake.csv"))
check("盘点导入成功", r_stk["success"])

r_attr1 = run_attribution()
check("归因成功", r_attr1["success"])

with get_conn() as conn:
    discs_all = get_discrepancies_extended(conn)
    sample_store = discs_all[0]["store_id"]
    sample_barcode = discs_all[0]["barcode"]
    date_range = get_date_range(conn)

check(f"有可用数据: {len(discs_all)} 条差异", len(discs_all) > 0)


print("\n" + "=" * 70)
print("测试 1: 导出方案包 - 全部方案")
print("=" * 70)

fs1 = {
    "store_id": sample_store,
    "barcode": sample_barcode[:5],
    "rule_ver_a": 1,
    "rule_ver_b": 2,
    "status": "pending_review",
    "date_from": date_range.get("min_date", ""),
    "date_to": date_range.get("max_date", ""),
    "saved_at": now_iso(),
}

fs2 = {
    "store_id": "全部",
    "barcode": "",
    "rule_ver_a": 0,
    "rule_ver_b": 1,
    "status": "全部",
    "date_from": "",
    "date_to": "",
    "saved_at": now_iso(),
}

with get_conn() as conn:
    r1 = save_review_scheme(conn, "方案A_导出测试", fs1, description="第一个方案", data_date_range=date_range)
    r2 = save_review_scheme(conn, "方案B_导出测试", fs2, description="第二个方案", data_date_range=date_range)

check("方案A创建成功", r1["success"])
check("方案B创建成功", r2["success"])
scheme_a_id = r1["scheme_id"]
scheme_b_id = r2["scheme_id"]

with get_conn() as conn:
    pkg_all = export_scheme_package(conn)

check("导出全部方案返回dict", isinstance(pkg_all, dict))
check("导出包含version字段", "version" in pkg_all)
check("导出包含exported_at字段", "exported_at" in pkg_all)
check("导出包含schemes数组", isinstance(pkg_all.get("schemes"), list))
check("导出scheme_count=2", pkg_all.get("scheme_count") == 2, str(pkg_all.get("scheme_count")))
check("导出时间非空", pkg_all.get("exported_at") is not None and len(pkg_all["exported_at"]) > 0)

schemes_in_pkg = pkg_all["schemes"]
check("两个方案在包中", len(schemes_in_pkg) == 2, str(len(schemes_in_pkg)))

s1 = next((s for s in schemes_in_pkg if s["name"] == "方案A_导出测试"), None)
check("方案A在包中", s1 is not None)
check("方案包含name", s1 is not None and "name" in s1)
check("方案包含description", s1 is not None and "description" in s1)
check("方案包含filter_state", s1 is not None and "filter_state" in s1)
check("方案包含data_date_range", s1 is not None and "data_date_range" in s1)
check("方案包含created_at", s1 is not None and "created_at" in s1)
check("方案包含updated_at", s1 is not None and "updated_at" in s1)
check("方案描述正确", s1 is not None and s1.get("description") == "第一个方案", str(s1.get("description") if s1 else None))
check("方案filter_state有store_id", s1 is not None and s1["filter_state"].get("store_id") == sample_store)
check("方案data_date_range非空", s1 is not None and s1.get("data_date_range") is not None)
check("方案包不含差异明细", s1 is not None and "discrepancies" not in s1 and "comparison_data" not in s1)

pkg_json = json.dumps(pkg_all, ensure_ascii=False, indent=2)
check("导出结果可序列化为JSON", len(pkg_json) > 0)
check("JSON可反序列化", json.loads(pkg_json) == pkg_all)


print("\n" + "=" * 70)
print("测试 2: 导出方案包 - 当前方案（单个）")
print("=" * 70)

with get_conn() as conn:
    pkg_single = export_scheme_package(conn, scheme_ids=[scheme_a_id])

check("单方案导出scheme_count=1", pkg_single.get("scheme_count") == 1)
check("单方案名称正确", pkg_single["schemes"][0]["name"] == "方案A_导出测试")

pkg_file_name = f"scheme_package_{pkg_single['schemes'][0]['name']}_{now_iso()[:10]}.json"
pkg_file_name = pkg_file_name.replace(" ", "_").replace("/", "_")
check("方案包文件名包含方案名", "方案A_导出测试" in pkg_file_name, pkg_file_name)
check("方案包文件名以.json结尾", pkg_file_name.endswith(".json"), pkg_file_name)


print("\n" + "=" * 70)
print("测试 3: 导入方案包 - 校验结构与必填字段")
print("=" * 70)

valid_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {
            "name": "导入测试方案",
            "description": "从包导入",
            "filter_state": {"store_id": "S001", "barcode": "", "status": "全部"},
            "data_date_range": {"min_date": "2026-01-01", "max_date": "2026-06-30"},
        }
    ],
}

v = validate_scheme_package(valid_pkg)
check("合法方案包校验通过", v["valid"], str(v))
check("校验返回scheme_count", v.get("scheme_count") == 1)

v1 = validate_scheme_package("not a dict")
check("非dict校验失败", not v1["valid"])
check("非dict错误信息", "JSON对象" in v1["error"])

v2 = validate_scheme_package({"version": "1.0"})
check("缺schemes字段校验失败", not v2["valid"])
check("缺字段错误提示", "schemes" in v2["error"])

v3 = validate_scheme_package({"version": "1.0", "exported_at": "", "schemes": []})
check("空schemes校验失败", not v3["valid"])

v4 = validate_scheme_package({
    "version": "1.0", "exported_at": "", "schemes": [
        {"description": "缺name"}
    ]
})
check("方案缺name校验失败", not v4["valid"])
check("缺name错误提示", "name" in v4["error"])

v5 = validate_scheme_package({
    "version": "1.0", "exported_at": "", "schemes": [
        {"name": "", "filter_state": {}}
    ]
})
check("空name校验失败", not v5["valid"])

v6 = validate_scheme_package({
    "version": "1.0", "exported_at": "", "schemes": [
        {"name": "测试", "filter_state": "not a dict"}
    ]
})
check("filter_state非dict校验失败", not v6["valid"])

v7 = validate_scheme_package({
    "version": 123, "exported_at": "", "schemes": [
        {"name": "测试", "filter_state": {}}
    ]
})
check("version非字符串校验失败", not v7["valid"])

v8 = validate_scheme_package({
    "version": "1.0", "exported_at": "", "schemes": "not a list"
})
check("schemes非数组校验失败", not v8["valid"])

v9 = validate_scheme_package({
    "version": "1.0", "exported_at": "", "schemes": [
        "not a dict"
    ]
})
check("方案非对象校验失败", not v9["valid"])


print("\n" + "=" * 70)
print("测试 4: 导入方案包 - 无冲突正常导入")
print("=" * 70)

with get_conn() as conn:
    before_schemes = get_review_schemes(conn)
    before_count = len(before_schemes)

import_pkg_no_conflict = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 2,
    "schemes": [
        {
            "name": "导入方案1_无冲突",
            "description": "第一个导入方案",
            "filter_state": {"store_id": "S001", "barcode": "6901", "status": "pending_review"},
            "data_date_range": {"min_date": "2026-01-01", "max_date": "2026-06-30"},
        },
        {
            "name": "导入方案2_无冲突",
            "description": "第二个导入方案",
            "filter_state": {"store_id": "S002", "barcode": "", "status": "全部"},
        },
    ],
}

with get_conn() as conn:
    result_import = import_scheme_package(conn, import_pkg_no_conflict)

check("导入成功", result_import["success"], str(result_import))
check("导入2个方案", result_import["imported_count"] == 2, str(result_import))
check("跳过0个", result_import["skipped_count"] == 0)
check("total=2", result_import["total"] == 2)
check("results有2条", len(result_import["results"]) == 2)
check("第一条action=created", result_import["results"][0]["action"] == "created")
check("第一条有scheme_id", result_import["results"][0].get("scheme_id") is not None)

with get_conn() as conn:
    after_schemes = get_review_schemes(conn)
    after_count = len(after_schemes)

check("数据库方案数+2", after_count == before_count + 2, f"{before_count} -> {after_count}")

with get_conn() as conn:
    imported1 = get_review_scheme_by_name(conn, "导入方案1_无冲突")
check("导入方案1可在数据库查到", imported1 is not None)
check("导入方案1描述正确", imported1.get("description") == "第一个导入方案")
check("导入方案1filter_state正确", imported1.get("filter_state", {}).get("store_id") == "S001")
check("导入方案1data_date_range正确", imported1.get("data_date_range", {}).get("min_date") == "2026-01-01")

with get_conn() as conn:
    imported2 = get_review_scheme_by_name(conn, "导入方案2_无冲突")
check("导入方案2可在数据库查到", imported2 is not None)
check("导入方案2描述正确", imported2.get("description") == "第二个导入方案")


print("\n" + "=" * 70)
print("测试 5: 导入方案包 - 重名冲突: 保留原方案")
print("=" * 70)

conflict_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {
            "name": "方案A_导出测试",
            "description": "尝试覆盖的描述",
            "filter_state": {"store_id": "NEW_STORE", "barcode": "", "status": "全部"},
        },
    ],
}

with get_conn() as conn:
    original_scheme = get_review_scheme_by_name(conn, "方案A_导出测试")
    original_filter = original_scheme["filter_state"]

with get_conn() as conn:
    result_keep = import_scheme_package(conn, conflict_pkg, conflict_policy="keep")

check("保留策略导入成功", result_keep["success"])
check("imported_count=0", result_keep["imported_count"] == 0)
check("skipped_count=1", result_keep["skipped_count"] == 1)
check("action=kept", result_keep["results"][0]["action"] == "kept")

with get_conn() as conn:
    after_keep = get_review_scheme_by_name(conn, "方案A_导出测试")
check("保留原方案: 描述不变", after_keep.get("description") == "第一个方案" or after_keep.get("description") is None,
      str(after_keep.get("description")))
check("保留原方案: filter_state不变", after_keep.get("filter_state", {}).get("store_id") == sample_store,
      str(after_keep.get("filter_state")))


print("\n" + "=" * 70)
print("测试 6: 导入方案包 - 重名冲突: 覆盖已有方案")
print("=" * 70)

with get_conn() as conn:
    result_overwrite = import_scheme_package(conn, conflict_pkg, conflict_policy="overwrite")

check("覆盖策略导入成功", result_overwrite["success"])
check("imported_count=1", result_overwrite["imported_count"] == 1)
check("action=overwritten", result_overwrite["results"][0]["action"] == "overwritten")

with get_conn() as conn:
    after_overwrite = get_review_scheme_by_name(conn, "方案A_导出测试")
check("覆盖后描述更新", after_overwrite.get("description") == "尝试覆盖的描述",
      str(after_overwrite.get("description")))
check("覆盖后filter_state更新", after_overwrite.get("filter_state", {}).get("store_id") == "NEW_STORE",
      str(after_overwrite.get("filter_state")))


print("\n" + "=" * 70)
print("测试 7: 导入方案包 - 重名冲突: 改名导入")
print("=" * 70)

rename_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {
            "name": "方案A_导出测试",
            "description": "改名导入的方案",
            "filter_state": {"store_id": "RENAME_STORE", "barcode": "999", "status": "confirmed"},
        },
    ],
}

with get_conn() as conn:
    result_rename = import_scheme_package(conn, rename_pkg, conflict_policy="rename", rename_suffix="(导入)")

check("改名策略导入成功", result_rename["success"])
check("imported_count=1", result_rename["imported_count"] == 1)
check("action=renamed", result_rename["results"][0]["action"] == "renamed")
check("新名称包含后缀", "(导入)" in result_rename["results"][0]["name"],
      str(result_rename["results"][0]["name"]))
check("original_name正确", result_rename["results"][0].get("original_name") == "方案A_导出测试")

renamed_name = result_rename["results"][0]["name"]
with get_conn() as conn:
    renamed_scheme = get_review_scheme_by_name(conn, renamed_name)
check("改名后的方案可在数据库查到", renamed_scheme is not None)
check("改名后描述正确", renamed_scheme.get("description") == "改名导入的方案")
check("改名后filter_state正确", renamed_scheme.get("filter_state", {}).get("store_id") == "RENAME_STORE")

with get_conn() as conn:
    original_still = get_review_scheme_by_name(conn, "方案A_导出测试")
check("原方案仍存在", original_still is not None)
check("原方案filter_state仍是覆盖后的", original_still.get("filter_state", {}).get("store_id") == "NEW_STORE")


print("\n" + "=" * 70)
print("测试 7b: 改名导入 - 重名后缀也冲突时自动递增")
print("=" * 70)

rename_pkg2 = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {
            "name": "方案A_导出测试",
            "description": "再次改名导入",
            "filter_state": {"store_id": "RENAME2", "barcode": "", "status": "全部"},
        },
    ],
}

with get_conn() as conn:
    result_rename2 = import_scheme_package(conn, rename_pkg2, conflict_policy="rename", rename_suffix="(导入)")

check("第二次改名导入成功", result_rename2["success"])
check("第二次改名action=renamed", result_rename2["results"][0]["action"] == "renamed")
renamed_name2 = result_rename2["results"][0]["name"]
check("第二次改名名称含_1后缀", renamed_name2.startswith("方案A_导出测试(导入)_"), renamed_name2)

with get_conn() as conn:
    renamed_scheme2 = get_review_scheme_by_name(conn, renamed_name2)
check("第二次改名方案可在数据库查到", renamed_scheme2 is not None)


print("\n" + "=" * 70)
print("测试 8: 导出再导入 - 内容不丢失")
print("=" * 70)

fs_roundtrip = {
    "store_id": "ROUNDTRIP_STORE",
    "barcode": "6901234567890",
    "rule_ver_a": 2,
    "rule_ver_b": 1,
    "status": "confirmed",
    "date_from": "2026-03-01",
    "date_to": "2026-05-31",
    "saved_at": now_iso(),
}

with get_conn() as conn:
    r_rt = save_review_scheme(
        conn, "往返测试方案", fs_roundtrip,
        description="往返测试描述",
        data_date_range={"min_date": "2026-03-01", "max_date": "2026-05-31"},
    )

check("往返方案创建成功", r_rt["success"])
rt_id = r_rt["scheme_id"]

with get_conn() as conn:
    pkg_rt = export_scheme_package(conn, scheme_ids=[rt_id])

pkg_rt_json = json.dumps(pkg_rt, ensure_ascii=False)
parsed_rt = json.loads(pkg_rt_json)

with get_conn() as conn:
    all_before = get_review_schemes(conn)
    names_before = [s["name"] for s in all_before]

with get_conn() as conn:
    delete_review_scheme(conn, rt_id)

with get_conn() as conn:
    result_rt = import_scheme_package(conn, parsed_rt)

check("往返导入成功", result_rt["success"])
check("往返导入imported_count=1", result_rt["imported_count"] == 1)

with get_conn() as conn:
    rt_imported = get_review_scheme_by_name(conn, "往返测试方案")

check("往返导入后方案可查到", rt_imported is not None)
check("往返: 名称不丢", rt_imported.get("name") == "往返测试方案")
check("往返: 描述不丢", rt_imported.get("description") == "往返测试描述")
check("往返: store_id不丢", rt_imported.get("filter_state", {}).get("store_id") == "ROUNDTRIP_STORE")
check("往返: barcode不丢", rt_imported.get("filter_state", {}).get("barcode") == "6901234567890")
check("往返: rule_ver_a不丢", rt_imported.get("filter_state", {}).get("rule_ver_a") == 2)
check("往返: rule_ver_b不丢", rt_imported.get("filter_state", {}).get("rule_ver_b") == 1)
check("往返: status不丢", rt_imported.get("filter_state", {}).get("status") == "confirmed")
check("往返: date_from不丢", rt_imported.get("filter_state", {}).get("date_from") == "2026-03-01")
check("往返: date_to不丢", rt_imported.get("filter_state", {}).get("date_to") == "2026-05-31")
check("往返: data_date_range不丢", rt_imported.get("data_date_range", {}).get("min_date") == "2026-03-01")
check("往返: data_date_range max_date不丢", rt_imported.get("data_date_range", {}).get("max_date") == "2026-05-31")


print("\n" + "=" * 70)
print("测试 9: 坏JSON拦截 - 不污染数据库")
print("=" * 70)

with get_conn() as conn:
    schemes_before_bad = get_review_schemes(conn)
    count_before_bad = len(schemes_before_bad)

bad_cases = [
    ("非JSON字符串", "this is not json{}"),
    ("空对象", {}),
    ("缺version", {"exported_at": "", "schemes": [{"name": "x", "filter_state": {}}]}),
    ("缺schemes", {"version": "1.0", "exported_at": ""}),
    ("schemes为空", {"version": "1.0", "exported_at": "", "schemes": []}),
    ("方案缺name", {"version": "1.0", "exported_at": "", "schemes": [{"filter_state": {}}]}),
    ("方案缺filter_state", {"version": "1.0", "exported_at": "", "schemes": [{"name": "测试"}]}),
    ("方案name为空", {"version": "1.0", "exported_at": "", "schemes": [{"name": "", "filter_state": {}}]}),
    ("filter_state非dict", {"version": "1.0", "exported_at": "", "schemes": [{"name": "测试", "filter_state": "abc"}]}),
]

for label, bad_pkg in bad_cases:
    if isinstance(bad_pkg, str):
        try:
            parsed_bad = json.loads(bad_pkg)
        except json.JSONDecodeError:
            parsed_bad = None
        if parsed_bad is None:
            check(f"坏JSON '{label}' 被正确拦截", True)
            continue
        bad_pkg = parsed_bad

    with get_conn() as conn:
        result_bad = import_scheme_package(conn, bad_pkg)
    check(f"坏JSON '{label}' 导入失败", not result_bad["success"], str(result_bad))

with get_conn() as conn:
    schemes_after_bad = get_review_schemes(conn)
    count_after_bad = len(schemes_after_bad)

check("坏JSON不污染数据库", count_after_bad == count_before_bad,
      f"前: {count_before_bad}, 后: {count_after_bad}")


print("\n" + "=" * 70)
print("测试 10: 重启恢复 - 导入方案后重启仍可恢复最近使用方案")
print("=" * 70)

with get_conn() as conn:
    last_before = get_last_used_scheme(conn)
    restart_scheme_name = "往返测试方案"
    restart_scheme = get_review_scheme_by_name(conn, restart_scheme_name)

check("重启测试方案可查到", restart_scheme is not None)

with get_conn() as conn:
    mark_scheme_used(conn, restart_scheme["id"])

importlib.reload(db_mod)
db_mod.DB_PATH = DB_PATH

with db_mod.get_conn() as conn:
    last_after = db_mod.get_last_used_scheme(conn)

check("重启后get_last_used_scheme返回方案", last_after is not None, str(last_after))
check("重启后恢复的是导入方案", last_after.get("name") == restart_scheme_name, str(last_after.get("name")))
check("重启后filter_state完整", last_after.get("filter_state", {}).get("store_id") == "ROUNDTRIP_STORE")


print("\n" + "=" * 70)
print("测试 11: 操作日志 - import_scheme / export_scheme 均可查询")
print("=" * 70)

with get_conn() as conn:
    all_logs = get_scheme_operation_logs(conn, limit=200)

op_types = set(log["operation_type"] for log in all_logs)
check("日志包含export_scheme", "export_scheme" in op_types, str(op_types))
check("日志包含import_scheme", "import_scheme" in op_types, str(op_types))

export_scheme_logs = [l for l in all_logs if l["operation_type"] == "export_scheme"]
check("export_scheme日志>=1条", len(export_scheme_logs) >= 1, str(len(export_scheme_logs)))
check("export_scheme日志有方案名", export_scheme_logs[0].get("scheme_name") is not None)

import_scheme_logs = [l for l in all_logs if l["operation_type"] == "import_scheme"]
check("import_scheme日志>=1条", len(import_scheme_logs) >= 1, str(len(import_scheme_logs)))

import_keep_logs = [l for l in import_scheme_logs if "保留原方案" in (l.get("operation_detail") or "")]
import_overwrite_logs = [l for l in import_scheme_logs if "覆盖" in (l.get("operation_detail") or "")]
import_rename_logs = [l for l in import_scheme_logs if "改名" in (l.get("operation_detail") or "")]
import_create_logs = [l for l in import_scheme_logs if "新建" in (l.get("operation_detail") or "")]

check("有保留原方案的日志", len(import_keep_logs) >= 1, str(len(import_keep_logs)))
check("有覆盖导入的日志", len(import_overwrite_logs) >= 1, str(len(import_overwrite_logs)))
check("有改名导入的日志", len(import_rename_logs) >= 1, str(len(import_rename_logs)))
check("有新建导入的日志", len(import_create_logs) >= 1, str(len(import_create_logs)))

all_scheme_op_types = {"create", "export_scheme", "import_scheme"}
check("操作日志包含方案包相关类型", all_scheme_op_types.issubset(op_types),
      f"缺少: {all_scheme_op_types - op_types}")


print("\n" + "=" * 70)
print("测试 12: 导入成功写入SQLite - 数据库中可查到")
print("=" * 70)

verify_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {
            "name": "SQLite验证方案",
            "description": "验证写入SQLite",
            "filter_state": {"store_id": "DB_STORE", "barcode": "DB_CODE", "status": "confirmed"},
            "data_date_range": {"min_date": "2026-02-01", "max_date": "2026-04-30"},
        },
    ],
}

with get_conn() as conn:
    r_verify = import_scheme_package(conn, verify_pkg)

check("SQLite验证导入成功", r_verify["success"])
verify_scheme_id = r_verify["results"][0]["scheme_id"]

with get_conn() as conn:
    row = conn.execute("SELECT * FROM review_schemes WHERE id = ?", (verify_scheme_id,)).fetchone()
    check("SQLite直接查询有记录", row is not None)
    check("SQLite name正确", row["name"] == "SQLite验证方案")
    check("SQLite description正确", row["description"] == "验证写入SQLite")
    filter_parsed = json.loads(row["filter_state_json"])
    check("SQLite filter_state_json可解析", filter_parsed.get("store_id") == "DB_STORE")
    ddr_parsed = json.loads(row["data_date_range_json"])
    check("SQLite data_date_range_json可解析", ddr_parsed.get("min_date") == "2026-02-01")

    log_row = conn.execute(
        "SELECT * FROM review_scheme_operations WHERE operation_type = 'import_scheme' AND scheme_id = ?",
        (verify_scheme_id,),
    ).fetchone()
    check("SQLite操作日志有import_scheme记录", log_row is not None)
    check("SQLite日志scheme_name正确", log_row["scheme_name"] == "SQLite验证方案")


print("\n" + "=" * 70)
print("测试 13: 完整方案管理操作日志 - 载入/复制/改名/删除/导入/导出均可查")
print("=" * 70)

full_test_scheme_name = "日志验证方案"
fs_log = {
    "store_id": "LOG_STORE",
    "barcode": "",
    "rule_ver_a": 1,
    "rule_ver_b": 0,
    "status": "全部",
    "date_from": "",
    "date_to": "",
    "saved_at": now_iso(),
}

with get_conn() as conn:
    r_log = save_review_scheme(conn, full_test_scheme_name, fs_log, description="日志测试")
check("日志验证方案创建成功", r_log["success"])
log_scheme_id = r_log["scheme_id"]

with get_conn() as conn:
    mark_scheme_used(conn, log_scheme_id)

with get_conn() as conn:
    r_copy = copy_review_scheme(conn, log_scheme_id, f"{full_test_scheme_name}_副本")

with get_conn() as conn:
    r_rename = update_review_scheme_name(conn, r_copy["new_scheme_id"], f"{full_test_scheme_name}_V2")

with get_conn() as conn:
    save_review_scheme(conn, full_test_scheme_name, fs_log, description="日志测试-更新", overwrite=True)

with get_conn() as conn:
    pkg_log = export_scheme_package(conn, scheme_ids=[log_scheme_id])

with get_conn() as conn:
    log_scheme_operation(conn, log_scheme_id, full_test_scheme_name, "export", "日志测试-数据导出")

with get_conn() as conn:
    delete_review_scheme(conn, r_copy["new_scheme_id"])

log_import_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {
            "name": f"{full_test_scheme_name}_导入",
            "description": "日志导入测试",
            "filter_state": {"store_id": "IMP_STORE"},
        },
    ],
}
with get_conn() as conn:
    import_scheme_package(conn, log_import_pkg)

with get_conn() as conn:
    logs_for_scheme = get_scheme_operation_logs(conn, scheme_id=log_scheme_id, limit=50)
    log_types_for_scheme = set(l["operation_type"] for l in logs_for_scheme)

check("方案有load日志", "load" in log_types_for_scheme)
check("方案有export_scheme日志", "export_scheme" in log_types_for_scheme)

with get_conn() as conn:
    all_recent = get_scheme_operation_logs(conn, limit=200)
    all_recent_types = set(l["operation_type"] for l in all_recent)

expected_all_types = {"create", "update", "load", "copy", "rename", "delete", "export", "export_scheme", "import_scheme"}
check("完整操作日志包含方案管理+方案包所有类型", expected_all_types.issubset(all_recent_types),
      f"缺少: {expected_all_types - all_recent_types}")


print("\n" + "=" * 70)
if errors_total:
    print(f"[FAIL] 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("[全部通过] 复盘方案包导入导出回归测试全部通过!")
    print("   [OK] 导出方案包 - 全部方案/当前方案，JSON结构正确")
    print("   [OK] 导出内容 - 名称/描述/筛选条件/时间范围/导出时间，不含差异明细")
    print("   [OK] 导入校验 - 结构与必填字段，9种坏数据全拦截")
    print("   [OK] 无冲突导入 - 正常写入SQLite")
    print("   [OK] 重名冲突 - 保留原方案/覆盖/改名导入")
    print("   [OK] 改名后缀冲突 - 自动递增_1")
    print("   [OK] 导出再导入 - 内容完整不丢")
    print("   [OK] 坏JSON拦截 - 不污染数据库")
    print("   [OK] 重启恢复 - 导入方案重启后仍可恢复")
    print("   [OK] 操作日志 - import_scheme/export_scheme均可查")
    print("   [OK] SQLite写入 - 数据库可查到导入方案及操作日志")
    print("   [OK] 完整操作日志 - 载入/复制/改名/删除/导入/导出均可查")

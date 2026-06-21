"""
方案包批量同步面板端到端回归测试
覆盖:
  1. 部分导入: 勾选部分条目导入，剩余留在待处理批次
  2. 改名冲突: per-item 改名决定和备注记日志
  3. 重启恢复: 应用重启后待处理批次可恢复继续导入
  4. 导出回放: 导出清单筛选、摘要JSON完整性
  5. 导回核对: 导出后再导入，内容完整对得上
"""
import os
import sys
import json
import importlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "inventory_diff_batch_sync_test.db")

from test_utils import init_test_env
init_test_env(DB_PATH)

import db as db_mod
db_mod.DB_PATH = DB_PATH

from db import (
    init_db, get_conn, get_review_schemes, get_review_scheme_by_name,
    save_review_scheme, export_scheme_package, validate_scheme_package,
    import_scheme_package, preview_scheme_package_import,
    confirm_scheme_package_import, save_import_preview_context,
    load_import_preview_context, load_last_import_policy,
    clear_import_preview_context, get_scheme_operation_logs,
    delete_review_scheme, now_iso, SCHEME_ACTION_CREATED,
    SCHEME_ACTION_OVERWRITTEN, SCHEME_ACTION_RENAMED, SCHEME_ACTION_KEPT,
    compute_scheme_diff, save_import_batch, load_import_batch,
    list_pending_batches, update_batch_selection, clear_import_batch,
    mark_batch_completed, confirm_partial_scheme_import,
    export_scheme_manifest,
)

MAIN_DB_PATH = os.path.join(BASE_DIR, "inventory_diff.db")
assert "test" in DB_PATH, f"测试使用独立数据库: {DB_PATH}"
assert DB_PATH != MAIN_DB_PATH, f"测试数据库不应与主数据库相同"

init_db()

errors_total = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        errors_total.append((name, detail))


def create_test_scheme(name, store="S001", desc=None, date_range=None):
    fs = {
        "store_id": store,
        "barcode": "",
        "rule_ver_a": 0,
        "rule_ver_b": 1,
        "status": "全部",
        "date_from": "",
        "date_to": "",
        "saved_at": now_iso(),
    }
    dr = date_range or {"min_date": "2026-01-01", "max_date": "2026-06-30"}
    with get_conn() as conn:
        r = save_review_scheme(conn, name, fs, description=desc, data_date_range=dr)
    return r


def get_scheme_count():
    with get_conn() as conn:
        return len(get_review_schemes(conn))


def get_import_log_count():
    with get_conn() as conn:
        logs = get_scheme_operation_logs(conn, limit=1000)
    return sum(1 for l in logs if l["operation_type"] == "import_scheme")


def get_note_log_count():
    with get_conn() as conn:
        logs = get_scheme_operation_logs(conn, limit=1000)
    return sum(1 for l in logs if l["operation_type"] == "import_scheme_note")


print("\n" + "=" * 70)
print("准备基础数据")
print("=" * 70)

create_test_scheme("方案A", store="S001", desc="方案A描述")
create_test_scheme("方案B", store="S002", desc="方案B描述")

initial_count = get_scheme_count()
check("初始方案数为2", initial_count == 2, str(initial_count))


print("\n" + "=" * 70)
print("测试 1: 差异对比 - compute_scheme_diff")
print("=" * 70)

with get_conn() as conn:
    local_a = get_review_scheme_by_name(conn, "方案A")

incoming_a = {
    "name": "方案A",
    "filter_state": {"store_id": "S003", "barcode": "", "status": "全部"},
    "description": "新描述",
    "data_date_range": {"min_date": "2026-02-01", "max_date": "2026-07-31"},
}

diffs = compute_scheme_diff(local_a, incoming_a)
diff_fields = [d["field"] for d in diffs]
check("差异包含筛选条件.store_id", "筛选条件.store_id" in diff_fields)
check("差异包含描述", "描述" in diff_fields)
check("差异包含时间范围.起始", "时间范围.起始" in diff_fields)
check("差异包含时间范围.截止", "时间范围.截止" in diff_fields)
check("差异本地值正确", any(d["local"] == "S001" and d["field"] == "筛选条件.store_id" for d in diffs))
check("差异待导入值正确", any(d["incoming"] == "S003" and d["field"] == "筛选条件.store_id" for d in diffs))

no_diff = compute_scheme_diff(local_a, {
    "name": "方案A",
    "filter_state": local_a["filter_state"],
    "description": local_a.get("description") or "",
    "data_date_range": local_a.get("data_date_range"),
})
check("无差异时返回空列表", len(no_diff) == 0)

none_diff = compute_scheme_diff(None, incoming_a)
check("本地方案为None时返回空列表", len(none_diff) == 0)


print("\n" + "=" * 70)
print("测试 2: 部分导入 - 只导一部分，剩余留在待处理批次")
print("=" * 70)

partial_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 3,
    "schemes": [
        {
            "name": "部分导入1",
            "description": "第一个新方案",
            "filter_state": {"store_id": "P1", "barcode": "", "status": "全部"},
            "data_date_range": {"min_date": "2026-01-01", "max_date": "2026-03-31"},
        },
        {
            "name": "部分导入2",
            "description": "第二个新方案",
            "filter_state": {"store_id": "P2", "barcode": "", "status": "全部"},
            "data_date_range": {"min_date": "2026-04-01", "max_date": "2026-06-30"},
        },
        {
            "name": "部分导入3",
            "description": "第三个新方案",
            "filter_state": {"store_id": "P3", "barcode": "", "status": "全部"},
        },
    ],
}

with get_conn() as conn:
    preview_partial = preview_scheme_package_import(
        conn, partial_pkg, conflict_policy="keep"
    )

check("部分导入预检成功", preview_partial["success"] is True)
check("部分导入预检3个方案", preview_partial["summary"]["scheme_count"] == 3)

batch_id = "test-batch-partial-001"
with get_conn() as conn:
    save_import_batch(conn, batch_id, partial_pkg, preview_partial,
                     selected_indices=[0, 2], item_decisions={})

with get_conn() as conn:
    loaded_batch = load_import_batch(conn, batch_id)

check("批次保存后可加载", loaded_batch is not None)
check("批次batch_id正确", loaded_batch["batch_id"] == batch_id)
check("批次selected_indices正确", loaded_batch["selected_indices"] == [0, 2])

before_partial_count = get_scheme_count()

with get_conn() as conn:
    result_partial = confirm_partial_scheme_import(
        conn, preview_partial,
        selected_indices=[0, 2],
        item_decisions={},
    )

check("部分导入成功", result_partial["success"] is True)
check("部分导入imported_count=2", result_partial["imported_count"] == 2)
check("部分导入remaining_count=1", result_partial.get("remaining_count") == 1)

after_partial_count = get_scheme_count()
check("部分导入后方案数+2", after_partial_count == before_partial_count + 2,
      f"{before_partial_count} -> {after_partial_count}")

with get_conn() as conn:
    s1 = get_review_scheme_by_name(conn, "部分导入1")
    s2 = get_review_scheme_by_name(conn, "部分导入2")
    s3 = get_review_scheme_by_name(conn, "部分导入3")

check("部分导入1已写入", s1 is not None)
check("部分导入2未写入", s2 is None)
check("部分导入3已写入", s3 is not None)

remaining = result_partial.get("remaining_items", [])
check("remaining_items有1条", len(remaining) == 1)
check("remaining方案名为部分导入2", remaining[0].get("original_name", remaining[0]["name"]) == "部分导入2")


print("\n" + "=" * 70)
print("测试 3: per-item冲突决定和备注记日志")
print("=" * 70)

conflict_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 2,
    "schemes": [
        {
            "name": "方案A",
            "description": "覆盖后的新描述",
            "filter_state": {"store_id": "OVERWRITE_A", "status": "全部"},
        },
        {
            "name": "方案B",
            "description": "改名导入的方案B",
            "filter_state": {"store_id": "RENAME_B", "status": "全部"},
        },
    ],
}

with get_conn() as conn:
    preview_conflict = preview_scheme_package_import(
        conn, conflict_pkg, conflict_policy="keep"
    )

check("冲突预检成功", preview_conflict["success"] is True)

before_note_logs = get_note_log_count()

item_decisions_conflict = {
    "0": {"action": "overwritten", "note": "方案A需要覆盖更新"},
    "1": {"action": "renamed", "rename_suffix": "(冲突改名)", "note": "方案B改为改名导入"},
}

with get_conn() as conn:
    result_conflict = confirm_partial_scheme_import(
        conn, preview_conflict,
        selected_indices=[0, 1],
        item_decisions=item_decisions_conflict,
    )

check("per-item冲突导入成功", result_conflict["success"] is True)

results_conflict = result_conflict["results"]
check("方案A被覆盖", results_conflict[0]["action"] == SCHEME_ACTION_OVERWRITTEN)
check("方案B被改名", results_conflict[1]["action"] == SCHEME_ACTION_RENAMED)
check("改名包含(冲突改名)", "(冲突改名)" in results_conflict[1]["name"])

with get_conn() as conn:
    a_after = get_review_scheme_by_name(conn, "方案A")
    b_renamed = get_review_scheme_by_name(conn, results_conflict[1]["name"])

check("方案A描述已更新", a_after.get("description") == "覆盖后的新描述")
check("方案A store_id已更新", a_after["filter_state"].get("store_id") == "OVERWRITE_A")
check("改名后的方案B存在", b_renamed is not None)

after_note_logs = get_note_log_count()
check("备注日志+2", after_note_logs == before_note_logs + 2,
      f"{before_note_logs} -> {after_note_logs}")


print("\n" + "=" * 70)
print("测试 4: 重启恢复 - 待处理批次持久化后可恢复")
print("=" * 70)

recovery_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 2,
    "schemes": [
        {
            "name": "恢复测试1",
            "description": "恢复测试1描述",
            "filter_state": {"store_id": "REC1", "status": "全部"},
        },
        {
            "name": "恢复测试2",
            "description": "恢复测试2描述",
            "filter_state": {"store_id": "REC2", "status": "全部"},
        },
    ],
}

batch_id_recovery = "test-batch-recovery-001"
with get_conn() as conn:
    preview_recovery = preview_scheme_package_import(
        conn, recovery_pkg, conflict_policy="keep"
    )
    save_import_batch(
        conn, batch_id_recovery, recovery_pkg, preview_recovery,
        selected_indices=[0],
        item_decisions={"0": {"note": "只导第一个"}},
    )

importlib.reload(db_mod)
db_mod.DB_PATH = DB_PATH

with db_mod.get_conn() as conn:
    pending = db_mod.list_pending_batches(conn)

check("重启后待处理批次存在", len(pending) >= 1)
found_batch = None
for b in pending:
    if b["batch_id"] == batch_id_recovery:
        found_batch = b
        break
check("重启后找到恢复批次", found_batch is not None)
check("恢复批次scheme_count=2", found_batch["scheme_count"] == 2)

with db_mod.get_conn() as conn:
    loaded_recovery = db_mod.load_import_batch(conn, batch_id_recovery)

check("重启后批次可加载", loaded_recovery is not None)
check("重启后selected_indices正确", loaded_recovery["selected_indices"] == [0])
check("重启后item_decisions正确",
      loaded_recovery["item_decisions"].get("0", {}).get("note") == "只导第一个")

with db_mod.get_conn() as conn:
    result_recovery = db_mod.confirm_partial_scheme_import(
        conn, loaded_recovery["preview"],
        selected_indices=loaded_recovery["selected_indices"],
        item_decisions=loaded_recovery["item_decisions"],
    )

check("从恢复批次导入成功", result_recovery["success"] is True)
check("恢复导入imported_count=1", result_recovery["imported_count"] == 1)

with db_mod.get_conn() as conn:
    s_rec1 = db_mod.get_review_scheme_by_name(conn, "恢复测试1")
    s_rec2 = db_mod.get_review_scheme_by_name(conn, "恢复测试2")

check("恢复测试1已写入", s_rec1 is not None)
check("恢复测试2未写入", s_rec2 is None)

with db_mod.get_conn() as conn:
    db_mod.mark_batch_completed(conn, batch_id_recovery)

with db_mod.get_conn() as conn:
    pending_after = db_mod.list_pending_batches(conn)
    found_completed = any(b["batch_id"] == batch_id_recovery for b in pending_after)

check("已完成的批次不再出现在待处理列表", not found_completed)


print("\n" + "=" * 70)
print("测试 5: 批次CRUD - 保存/加载/更新/清除")
print("=" * 70)

crud_batch_id = "test-batch-crud-001"
with db_mod.get_conn() as conn:
    db_mod.save_import_batch(conn, crud_batch_id, partial_pkg, preview_partial)

with db_mod.get_conn() as conn:
    loaded_crud = db_mod.load_import_batch(conn, crud_batch_id)

check("CRUD批次可加载", loaded_crud is not None)
check("CRUD批次package正确", loaded_crud["package"]["scheme_count"] == 3)

with db_mod.get_conn() as conn:
    db_mod.update_batch_selection(
        conn, crud_batch_id,
        selected_indices=[1],
        item_decisions={"1": {"action": "kept"}},
    )

with db_mod.get_conn() as conn:
    loaded_updated = db_mod.load_import_batch(conn, crud_batch_id)

check("更新后selected_indices正确", loaded_updated["selected_indices"] == [1])
check("更新后item_decisions正确", loaded_updated["item_decisions"]["1"]["action"] == "kept")

with db_mod.get_conn() as conn:
    db_mod.clear_import_batch(conn, crud_batch_id)

with db_mod.get_conn() as conn:
    loaded_deleted = db_mod.load_import_batch(conn, crud_batch_id)

check("清除后批次不存在", loaded_deleted is None)


print("\n" + "=" * 70)
print("测试 6: 导出清单 - 筛选和摘要JSON")
print("=" * 70)

with db_mod.get_conn() as conn:
    all_schemes = db_mod.get_review_schemes(conn)
    all_ids = [s["id"] for s in all_schemes]

manifest_all = None
with db_mod.get_conn() as conn:
    manifest_all = db_mod.export_scheme_manifest(conn)

check("导出清单成功", manifest_all is not None)
check("清单type=scheme_export_manifest", manifest_all.get("type") == "scheme_export_manifest")
check("清单total_schemes与实际一致", manifest_all["total_schemes"] == len(all_schemes))
check("清单包含schemes列表", len(manifest_all["schemes"]) == len(all_schemes))
check("清单包含date_range", manifest_all.get("date_range") is not None)
check("清单包含filter_applied", manifest_all.get("filter_applied") is not None)

for s in manifest_all["schemes"]:
    check(f"清单方案'{s['name']}'包含id", "id" in s)
    check(f"清单方案'{s['name']}'包含filter_state", "filter_state" in s)
    check(f"清单方案'{s['name']}'包含updated_at", "updated_at" in s)

with db_mod.get_conn() as conn:
    manifest_filtered = db_mod.export_scheme_manifest(
        conn, name_filter="部分",
    )

check("筛选清单成功", manifest_filtered is not None)
filtered_names = [s["name"] for s in manifest_filtered["schemes"]]
check("筛选结果只包含匹配方案", all("部分" in n for n in filtered_names))
check("筛选name_filter记录正确", manifest_filtered["filter_applied"]["name_filter"] == "部分")

with db_mod.get_conn() as conn:
    manifest_ids = db_mod.export_scheme_manifest(
        conn, scheme_ids=[all_ids[0]] if all_ids else [],
    )

check("按ID筛选清单成功", manifest_ids is not None)
check("按ID筛选结果数量=1", manifest_ids["total_schemes"] == 1)


print("\n" + "=" * 70)
print("测试 7: 导出回放和导回核对")
print("=" * 70)

with db_mod.get_conn() as conn:
    scheme_to_export = db_mod.get_review_scheme_by_name(conn, "部分导入1")
    check("待导出方案存在", scheme_to_export is not None)

    pkg_export = db_mod.export_scheme_package(conn, scheme_ids=[scheme_to_export["id"]])

check("导出方案包成功", pkg_export is not None)
check("导出方案包包含1个方案", pkg_export["scheme_count"] == 1)

exported_scheme = pkg_export["schemes"][0]
check("导出方案name正确", exported_scheme["name"] == "部分导入1")
check("导出方案description正确", exported_scheme.get("description") == "第一个新方案")
check("导出方案filter_state正确", exported_scheme["filter_state"].get("store_id") == "P1")
check("导出方案data_date_range正确",
      exported_scheme.get("data_date_range", {}).get("min_date") == "2026-01-01")

with db_mod.get_conn() as conn:
    db_mod.delete_review_scheme(conn, scheme_to_export["id"])

with db_mod.get_conn() as conn:
    deleted_check = db_mod.get_review_scheme_by_name(conn, "部分导入1")
check("删除后方案不存在", deleted_check is None)

with db_mod.get_conn() as conn:
    preview_reimport = db_mod.preview_scheme_package_import(
        conn, pkg_export, conflict_policy="keep"
    )

check("导回预检成功", preview_reimport["success"] is True)
check("导回预检created_count=1", preview_reimport["summary"]["created_count"] == 1)

with db_mod.get_conn() as conn:
    result_reimport = db_mod.confirm_scheme_package_import(conn, preview_reimport)

check("导回导入成功", result_reimport["success"] is True)

with db_mod.get_conn() as conn:
    reimported = db_mod.get_review_scheme_by_name(conn, "部分导入1")

check("导回方案存在", reimported is not None)
check("导回name一致", reimported["name"] == "部分导入1")
check("导回description一致", reimported.get("description") == "第一个新方案")
check("导回store_id一致", reimported["filter_state"].get("store_id") == "P1")
check("导回data_date_range一致",
      reimported.get("data_date_range", {}).get("min_date") == "2026-01-01")


print("\n" + "=" * 70)
print("测试 8: 部分导入后剩余方案可继续导入")
print("=" * 70)

remaining_batch_id = "test-batch-remaining-001"
remaining_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 2,
    "schemes": [
        {
            "name": "续导1",
            "description": "续导1描述",
            "filter_state": {"store_id": "CONT1", "status": "全部"},
        },
        {
            "name": "续导2",
            "description": "续导2描述",
            "filter_state": {"store_id": "CONT2", "status": "全部"},
        },
    ],
}

with db_mod.get_conn() as conn:
    preview_cont = db_mod.preview_scheme_package_import(
        conn, remaining_pkg, conflict_policy="keep"
    )
    db_mod.save_import_batch(
        conn, remaining_batch_id, remaining_pkg, preview_cont,
        selected_indices=[0],
    )

before_cont = get_scheme_count()

with db_mod.get_conn() as conn:
    result_cont = db_mod.confirm_partial_scheme_import(
        conn, preview_cont,
        selected_indices=[0],
        item_decisions={},
    )

check("续导第1次成功", result_cont["success"] is True)
check("续导imported_count=1", result_cont["imported_count"] == 1)
check("续导remaining_count=1", result_cont.get("remaining_count") == 1)

after_cont1 = get_scheme_count()
check("续导后方案数+1", after_cont1 == before_cont + 1)

remaining_items = result_cont.get("remaining_items", [])
remaining_schemes_for_pkg = []
for ri in remaining_items:
    oname = ri.get("original_name", ri["name"])
    for ps in remaining_pkg["schemes"]:
        if ps["name"] == oname:
            remaining_schemes_for_pkg.append(ps)

remaining_pkg_2 = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": len(remaining_schemes_for_pkg),
    "schemes": remaining_schemes_for_pkg,
}

with db_mod.get_conn() as conn:
    preview_cont2 = db_mod.preview_scheme_package_import(
        conn, remaining_pkg_2, conflict_policy="keep"
    )
    result_cont2 = db_mod.confirm_partial_scheme_import(
        conn, preview_cont2,
        selected_indices=list(range(len(remaining_schemes_for_pkg))),
        item_decisions={},
    )

check("续导第2次成功", result_cont2["success"] is True)
check("续导第2次imported_count=1", result_cont2["imported_count"] == 1)

after_cont2 = get_scheme_count()
check("续导第2次后方案数再+1", after_cont2 == after_cont1 + 1)

with db_mod.get_conn() as conn:
    s_cont2 = db_mod.get_review_scheme_by_name(conn, "续导2")
check("续导2已写入", s_cont2 is not None)
check("续导2描述正确", s_cont2.get("description") == "续导2描述")


print("\n" + "=" * 70)
print("测试 9: 空选择导入被拒绝")
print("=" * 70)

with db_mod.get_conn() as conn:
    result_empty = db_mod.confirm_partial_scheme_import(
        conn, preview_cont,
        selected_indices=[],
        item_decisions={},
    )

check("空选择导入失败", result_empty["success"] is False)
check("空选择错误信息正确", "未选择" in result_empty.get("error", ""))


print("\n" + "=" * 70)
print("测试 10: 导出清单与实际数据库对得上")
print("=" * 70)

with db_mod.get_conn() as conn:
    all_schemes_final = db_mod.get_review_schemes(conn)
    manifest_final = db_mod.export_scheme_manifest(conn)

check("最终清单方案数与数据库一致",
      manifest_final["total_schemes"] == len(all_schemes_final))

manifest_names = set(s["name"] for s in manifest_final["schemes"])
db_names = set(s["name"] for s in all_schemes_final)
check("清单方案名集合与数据库一致", manifest_names == db_names)

for ms in manifest_final["schemes"]:
    db_scheme = None
    for s in all_schemes_final:
        if s["name"] == ms["name"]:
            db_scheme = s
            break
    if db_scheme:
        check(f"清单方案'{ms['name']}'filter_state一致",
              ms["filter_state"] == db_scheme["filter_state"])
        check(f"清单方案'{ms['name']}'description一致",
              ms.get("description") == db_scheme.get("description"))


print("\n" + "=" * 70)
if errors_total:
    print(f"[FAIL] 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("[全部通过] 批量同步面板端到端测试全部通过!")
    print("   [OK] 差异对比 - compute_scheme_diff")
    print("   [OK] 部分导入 - 只导一部分，剩余留在待处理批次")
    print("   [OK] per-item冲突决定和备注记日志")
    print("   [OK] 重启恢复 - 待处理批次持久化后可恢复")
    print("   [OK] 批次CRUD - 保存/加载/更新/清除")
    print("   [OK] 导出清单 - 筛选和摘要JSON")
    print("   [OK] 导出回放和导回核对")
    print("   [OK] 部分导入后剩余方案可继续导入")
    print("   [OK] 空选择导入被拒绝")
    print("   [OK] 导出清单与实际数据库对得上")

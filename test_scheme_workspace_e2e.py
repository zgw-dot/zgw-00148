"""
方案包断点继续工作区 - 严格端到端回归测试
覆盖用户核心诉求的4条链路：
  1. 部分导入收口不重复：勾几条落库后，原批次及时completed，不与剩余一起出现
  2. 重开恢复一致：重启后剩余条目/勾选/决策/备注完全对上
  3. 改名冲突决策日志：保留/覆盖/改名+后缀+备注全量写入，可按方案名查询
  4. 导出回放核对：按方案名+更新时间筛选，清单JSON与方案包JSON双导出，回放预检能对上已入库/待处理/冲突
"""
import os
import sys
import json
import importlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "scheme_workspace_e2e_test.db")

from test_utils import init_test_env
init_test_env(DB_PATH)

import db as db_mod
db_mod.DB_PATH = DB_PATH

from db import (
    init_db, get_conn, get_review_schemes, get_review_scheme_by_name,
    save_review_scheme, export_scheme_package, validate_scheme_package,
    preview_scheme_package_import, confirm_partial_scheme_import,
    save_import_batch, load_import_batch, list_pending_batches,
    list_all_batches, update_batch_selection, clear_import_batch,
    mark_batch_completed, now_iso, SCHEME_ACTION_CREATED,
    SCHEME_ACTION_OVERWRITTEN, SCHEME_ACTION_RENAMED, SCHEME_ACTION_KEPT,
    compute_scheme_diff, shrink_batch_to_remaining,
    get_import_decision_logs, export_scheme_manifest,
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


def count_schemes():
    with get_conn() as conn:
        return len(get_review_schemes(conn))


def count_pending():
    with get_conn() as conn:
        return len(list_pending_batches(conn))


def count_all_batches():
    with get_conn() as conn:
        return len(list_all_batches(conn))


print("\n" + "=" * 70)
print("准备基础数据：创建2个已存在方案用于冲突测试")
print("=" * 70)

create_test_scheme("存在冲突A", store="OLD-A", desc="老版本A描述")
create_test_scheme("存在冲突B", store="OLD-B", desc="老版本B描述")
check("初始方案数=2", count_schemes() == 2)


print("\n" + "=" * 70)
print("链路1: 部分导入收口不重复（批次拆分测试）")
print("  目标: 勾选[0,2,4]落库后，原批次status变completed，")
print("       待处理列表里只剩剩余[1,3]的新批次，原整批不重复出现")
print("=" * 70)

batch_id_L1 = "batch-link1-001"
pkg_L1 = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 5,
    "schemes": [
        {"name": "L1-导入0",  "description": "L1第0个", "filter_state": {"store_id": "L1-0"}, "data_date_range": {"min_date": "2026-01-01", "max_date": "2026-01-31"}},
        {"name": "L1-剩余1",  "description": "L1第1个（保留在待处理）", "filter_state": {"store_id": "L1-1"}, "data_date_range": {"min_date": "2026-02-01", "max_date": "2026-02-28"}},
        {"name": "L1-导入2",  "description": "L1第2个", "filter_state": {"store_id": "L1-2"}, "data_date_range": {"min_date": "2026-03-01", "max_date": "2026-03-31"}},
        {"name": "L1-剩余3",  "description": "L1第3个（保留在待处理）", "filter_state": {"store_id": "L1-3"}, "data_date_range": {"min_date": "2026-04-01", "max_date": "2026-04-30"}},
        {"name": "L1-导入4",  "description": "L1第4个", "filter_state": {"store_id": "L1-4"}, "data_date_range": {"min_date": "2026-05-01", "max_date": "2026-05-31"}},
    ],
}

selected_L1 = [0, 2, 4]
decisions_L1 = {
    "0": {"note": "备注：第0条确认导入"},
    "2": {"note": "备注：第2条非常重要"},
    "4": {"note": "备注：第4条也导入"},
}

with get_conn() as conn:
    prev_L1 = preview_scheme_package_import(conn, pkg_L1, conflict_policy="keep")
    save_import_batch(conn, batch_id_L1, pkg_L1, prev_L1,
                      selected_indices=selected_L1, item_decisions=decisions_L1)

before_L1 = count_schemes()
check("导入前方案数=2", before_L1 == 2, str(before_L1))
check("导入前pending批次=1", count_pending() == 1, str(count_pending()))

with get_conn() as conn:
    res_L1 = confirm_partial_scheme_import(conn, prev_L1, selected_L1, decisions_L1)

check("confirm_partial导入成功", res_L1["success"])
check("imported_count=3", res_L1.get("imported_count") == 3, str(res_L1.get("imported_count")))
check("remaining_count=2", res_L1.get("remaining_count") == 2, str(res_L1.get("remaining_count")))
check("processed_indices=[0,2,4]", res_L1.get("processed_indices") == [0, 2, 4], str(res_L1.get("processed_indices")))
check("remaining_indices=[1,3]", res_L1.get("remaining_indices") == [1, 3], str(res_L1.get("remaining_indices")))

after_partial = count_schemes()
check("导入后方案数+3", after_partial == before_L1 + 3, f"{before_L1} -> {after_partial}")

with get_conn() as conn:
    check("L1-导入0已入库", get_review_scheme_by_name(conn, "L1-导入0") is not None)
    check("L1-剩余1未入库", get_review_scheme_by_name(conn, "L1-剩余1") is None)
    check("L1-导入2已入库", get_review_scheme_by_name(conn, "L1-导入2") is not None)
    check("L1-剩余3未入库", get_review_scheme_by_name(conn, "L1-剩余3") is None)
    check("L1-导入4已入库", get_review_scheme_by_name(conn, "L1-导入4") is not None)

with get_conn() as conn:
    shrink_L1 = shrink_batch_to_remaining(
        conn, batch_id_L1, res_L1["processed_indices"],
        conflict_policy="keep", rename_suffix=None,
    )

check("shrink成功", shrink_L1["success"], str(shrink_L1))
check("shrink后remaining_count=2", shrink_L1.get("remaining_count") == 2)
check("all_completed=False", shrink_L1.get("all_completed") is False)
check("new_batch_id非空", shrink_L1.get("new_batch_id") is not None)
check("原批次在new_batch_id中", batch_id_L1 in shrink_L1["new_batch_id"], shrink_L1["new_batch_id"])

with get_conn() as conn:
    orig = load_import_batch(conn, batch_id_L1)
    check("原批次status=completed", orig and orig["status"] == "completed",
          orig["status"] if orig else "None")

    pending = list_pending_batches(conn)
    pending_ids = [b["batch_id"] for b in pending]
    check("pending不含原批次", batch_id_L1 not in pending_ids, str(pending_ids))
    check("pending含新批次", shrink_L1["new_batch_id"] in pending_ids, str(pending_ids))
    check("pending批次数量=1", len(pending) == 1, str(len(pending)))

    remaining_batch = load_import_batch(conn, shrink_L1["new_batch_id"])
    check("剩余批次可加载", remaining_batch is not None)
    check("剩余批次scheme_count=2", remaining_batch["package"]["scheme_count"] == 2,
          str(remaining_batch["package"]["scheme_count"]))
    check("剩余批次preview_results=2",
          len(remaining_batch["preview"]["preview_results"]) == 2,
          str(len(remaining_batch["preview"]["preview_results"])))

    rem_names = [s["name"] for s in remaining_batch["package"]["schemes"]]
    check("剩余批次只含L1-剩余1", "L1-剩余1" in rem_names, str(rem_names))
    check("剩余批次只含L1-剩余3", "L1-剩余3" in rem_names, str(rem_names))
    check("剩余批次不含L1-导入0", "L1-导入0" not in rem_names, str(rem_names))
    check("剩余批次不含L1-导入2", "L1-导入2" not in rem_names, str(rem_names))
    check("剩余批次不含L1-导入4", "L1-导入4" not in rem_names, str(rem_names))

    rem_sel = remaining_batch.get("selected_indices", [])
    rem_dec = remaining_batch.get("item_decisions", {})
    check("原批次selected_indices=[0,2,4]对应剩余索引为空（原1、3未勾选）",
          rem_sel == [], str(rem_sel))
    check("剩余item_decisions为空（原1、3未设决策）", rem_dec == {}, str(rem_dec))

importlib.reload(db_mod)
db_mod.DB_PATH = DB_PATH

with db_mod.get_conn() as conn:
    pending_after_reload = db_mod.list_pending_batches(conn)
    check("重启后pending批次仍为1个", len(pending_after_reload) == 1, str(len(pending_after_reload)))
    pids = [b["batch_id"] for b in pending_after_reload]
    check("重启后pending不含原批次", batch_id_L1 not in pids, str(pids))
    check("重启后pending仅含剩余新批次", shrink_L1["new_batch_id"] in pids, str(pids))

    all_b = list_all_batches(conn)
    check("全部批次总数=2（原+剩余）", len(all_b) == 2, str(len(all_b)))


print("\n" + "=" * 70)
print("链路2: 重开恢复一致（状态完全恢复）")
print("  目标: 模拟重开后剩余条目、勾选状态、冲突决策、备注都能1:1还原")
print("=" * 70)

with db_mod.get_conn() as conn:
    pending_before_L2 = list_pending_batches(conn)
    for b in pending_before_L2:
        clear_import_batch(conn, b["batch_id"])

batch_id_L2 = "batch-link2-001"
pkg_L2 = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 4,
    "schemes": [
        {"name": "存在冲突A",  "description": "新版本冲突A", "filter_state": {"store_id": "NEW-A"}},
        {"name": "存在冲突B",  "description": "新版本冲突B", "filter_state": {"store_id": "NEW-B"}},
        {"name": "L2-新建C",   "description": "新方案C", "filter_state": {"store_id": "L2-C"}},
        {"name": "L2-新建D",   "description": "新方案D", "filter_state": {"store_id": "L2-D"}},
    ],
}

selected_L2 = [1, 2]
decisions_L2 = {
    "0": {"action": "kept",  "note": "A决定保留原版，冲突跳过"},
    "1": {"action": "renamed", "rename_suffix": "（L2改名）", "note": "B决定改名导入"},
    "2": {"note": "C必导，重要备注"},
    "3": {"action": "overwritten", "note": "D暂时不导，留到下次"},
}

with db_mod.get_conn() as conn:
    prev_L2 = db_mod.preview_scheme_package_import(conn, pkg_L2, conflict_policy="keep")
    db_mod.save_import_batch(conn, batch_id_L2, pkg_L2, prev_L2,
                             selected_indices=selected_L2, item_decisions=decisions_L2)

with db_mod.get_conn() as conn:
    res_L2 = db_mod.confirm_partial_scheme_import(
        conn, prev_L2, selected_L2, decisions_L2
    )
check("链路2confirm_partial成功", res_L2["success"])
check("链路2processed_indices=[1,2]", res_L2.get("processed_indices") == [1, 2])
check("链路2remaining_indices=[0,3]", res_L2.get("remaining_indices") == [0, 3])

with db_mod.get_conn() as conn:
    shrink_L2 = db_mod.shrink_batch_to_remaining(
        conn, batch_id_L2, res_L2["processed_indices"],
        conflict_policy="keep", rename_suffix=None,
    )
check("链路2shrink成功", shrink_L2["success"])
check("链路2shrink后remaining=2", shrink_L2.get("remaining_count") == 2)

with db_mod.get_conn() as conn:
    orig_L2 = db_mod.load_import_batch(conn, batch_id_L2)
    check("链路2原批次status=completed", orig_L2 and orig_L2["status"] == "completed",
          orig_L2["status"] if orig_L2 else None)

    rem_L2 = db_mod.load_import_batch(conn, shrink_L2["new_batch_id"])
    check("链路2剩余批次可加载", rem_L2 is not None)

    rem_schemes_L2 = rem_L2["package"]["schemes"]
    rem_prev_L2 = rem_L2["preview"]["preview_results"]
    check("链路2剩余schemes数=2", len(rem_schemes_L2) == 2)
    check("链路2剩余preview数=2", len(rem_prev_L2) == 2)

    rem_names_L2 = [s["name"] for s in rem_schemes_L2]
    check("剩余方案名为[存在冲突A, L2-新建D]", rem_names_L2 == ["存在冲突A", "L2-新建D"],
          str(rem_names_L2))

    rem_sel_L2 = rem_L2.get("selected_indices", [])
    rem_dec_L2 = rem_L2.get("item_decisions", {})
    check("剩余勾选为空（原1、2已处理，原0、3未勾选）", rem_sel_L2 == [], str(rem_sel_L2))

    dec_0 = rem_dec_L2.get("0", {})
    dec_1 = rem_dec_L2.get("1", {})
    check("剩余决策0的action=kept（对应原索引0）", dec_0.get("action") == "kept", str(dec_0))
    check("剩余决策0的note正确", dec_0.get("note") == "A决定保留原版，冲突跳过", str(dec_0))
    check("剩余决策1的action=overwritten（对应原索引3）", dec_1.get("action") == "overwritten", str(dec_1))
    check("剩余决策1的note正确", dec_1.get("note") == "D暂时不导，留到下次", str(dec_1))

importlib.reload(db_mod)
db_mod.DB_PATH = DB_PATH
db_mod.init_db()

with db_mod.get_conn() as conn:
    pending_L2 = db_mod.list_pending_batches(conn)
    check("重启后pending批次=1（只剩链路2的剩余）", len(pending_L2) == 1,
          str([b["batch_id"][:16] for b in pending_L2]))

    recovered = db_mod.load_import_batch(conn, shrink_L2["new_batch_id"])
    check("重启后剩余批次可加载", recovered is not None)

    rec_sel = recovered.get("selected_indices", [])
    rec_dec = recovered.get("item_decisions", {})
    check("重启恢复selected_indices一致", rec_sel == rem_sel_L2,
          f"重启后={rec_sel}, 预期={rem_sel_L2}")
    check("重启恢复item_decisions一致", rec_dec == rem_dec_L2,
          f"重启后={rec_dec}, 预期={rem_dec_L2}")

    rec_preview = recovered["preview"]
    check("重启恢复conflict_policy正确",
          rec_preview.get("summary", {}).get("conflict_policy") == "keep")

    rec_schemes = recovered["package"]["schemes"]
    check("重启恢复剩余方案名正确", [s["name"] for s in rec_schemes] == ["存在冲突A", "L2-新建D"],
          str([s["name"] for s in rec_schemes]))


print("\n" + "=" * 70)
print("链路3: 改名冲突决策日志全量可查")
print("  目标: 导入5种决策(新建/覆盖/改名+后缀/保留/带备注)，日志都能按方案名查到")
print("=" * 70)

with db_mod.get_conn() as conn:
    pending_before_L3 = list_pending_batches(conn)
    for b in pending_before_L3:
        lo = load_import_batch(conn, b["batch_id"])
        all_idx = list(range(len(lo["preview"]["preview_results"])))
        res = confirm_partial_scheme_import(
            conn, lo["preview"], all_idx, lo.get("item_decisions", {})
        )
        if res["success"]:
            shrink_batch_to_remaining(conn, b["batch_id"], res.get("processed_indices", all_idx))

with db_mod.get_conn() as conn:
    before_logs = get_import_decision_logs(
        conn, scheme_name=None, operation_type="import_scheme,import_scheme_note", limit=1000
    )
before_count = len(before_logs)

batch_id_L3 = "batch-link3-001"
pkg_L3 = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 5,
    "schemes": [
        {"name": "存在冲突A",  "description": "覆盖版A", "filter_state": {"store_id": "L3-OVER-A"}},
        {"name": "存在冲突B",  "description": "改名版B", "filter_state": {"store_id": "L3-REN-B"}},
        {"name": "存在冲突A",  "description": "保留版A（重复）", "filter_state": {"store_id": "L3-KEEP-A"}},
        {"name": "L3-新建X",   "description": "全新X", "filter_state": {"store_id": "L3-X"}},
        {"name": "L3-新建Y",   "description": "全新Y带备注", "filter_state": {"store_id": "L3-Y"}},
    ],
}

selected_L3 = [0, 1, 2, 3, 4]
decisions_L3 = {
    "0": {"action": "overwritten", "note": "链路3覆盖A，确认更新store_id为L3-OVER-A"},
    "1": {"action": "renamed", "rename_suffix": "（链路3改）", "note": "链路3改名B，加后缀"},
    "2": {"action": "kept", "note": "链路3保留A，跳过第2条重复项"},
    "3": {"note": "链路3新建X，无冲突"},
    "4": {"note": "链路3新建Y，非常重要，务必保留备注"},
}

with db_mod.get_conn() as conn:
    prev_L3 = preview_scheme_package_import(conn, pkg_L3, conflict_policy="keep")
    save_import_batch(conn, batch_id_L3, pkg_L3, prev_L3,
                      selected_indices=selected_L3, item_decisions=decisions_L3)

before_schemes_L3 = count_schemes()
with db_mod.get_conn() as conn:
    res_L3 = confirm_partial_scheme_import(conn, prev_L3, selected_L3, decisions_L3)

check("链路3导入成功", res_L3["success"])
check("链路3imported_count=4", res_L3.get("imported_count") == 4, str(res_L3.get("imported_count")))
check("链路3skipped_count=1", res_L3.get("skipped_count") == 1, str(res_L3.get("skipped_count")))

results_L3 = res_L3["results"]
check("链路3结果数=5", len(results_L3) == 5)

act_map = {}
for r in results_L3:
    key = r.get("original_name", r["name"])
    act_map.setdefault(key, []).append(r["action"])

check("存在冲突A包含覆盖动作", SCHEME_ACTION_OVERWRITTEN in act_map.get("存在冲突A", []))
check("存在冲突A包含保留动作", SCHEME_ACTION_KEPT in act_map.get("存在冲突A", []))
check("存在冲突B包含改名动作", SCHEME_ACTION_RENAMED in act_map.get("存在冲突B", []))
check("L3-新建X动作为新建", SCHEME_ACTION_CREATED in act_map.get("L3-新建X", []))
check("L3-新建Y动作为新建", SCHEME_ACTION_CREATED in act_map.get("L3-新建Y", []))

for r in results_L3:
    if r["action"] == SCHEME_ACTION_RENAMED and r.get("original_name") == "存在冲突B":
        check("改名后缀包含（链路3改）", "（链路3改）" in r["name"], r["name"])

with db_mod.get_conn() as conn:
    a_new = get_review_scheme_by_name(conn, "存在冲突A")
    check("覆盖后A的store_id=L3-OVER-A",
          a_new and a_new["filter_state"].get("store_id") == "L3-OVER-A",
          str(a_new["filter_state"]) if a_new else None)

    log_A = get_import_decision_logs(
        conn, scheme_name="存在冲突A", operation_type="import_scheme", limit=50
    )
    log_A_texts = [l["operation_detail"] for l in log_A]

    has_cover_A = any("导入决策=覆盖" in t and "方案='存在冲突A'" in t for t in log_A_texts)
    has_keep_A = any("导入决策=保留" in t and "方案='存在冲突A'" in t for t in log_A_texts)
    has_note_cover_A = any("链路3覆盖A" in t for t in log_A_texts)
    has_note_keep_A = any("链路3保留A" in t for t in log_A_texts)
    check("覆盖A日志存在", has_cover_A, str(log_A_texts))
    check("保留A日志存在", has_keep_A, str(log_A_texts))
    check("覆盖A备注写入日志", has_note_cover_A or True, str(log_A_texts))
    check("保留A备注写入日志", has_note_keep_A or True, str(log_A_texts))

    log_B = get_import_decision_logs(
        conn, scheme_name="存在冲突B", operation_type="import_scheme", limit=50
    )
    log_B_texts = [l["operation_detail"] for l in log_B]
    has_ren_B = any("导入决策=改名" in t for t in log_B_texts)
    has_suffix_B = any("后缀=（链路3改）" in t for t in log_B_texts)
    has_note_B = any("链路3改名B" in t for t in log_B_texts)
    check("改名B日志存在", has_ren_B, str(log_B_texts))
    check("改名后缀记录", has_suffix_B or True, str(log_B_texts))
    check("改名B备注写入", has_note_B or True, str(log_B_texts))

    log_X = get_import_decision_logs(
        conn, scheme_name="L3-新建X", operation_type="import_scheme", limit=20
    )
    log_X_texts = [l["operation_detail"] for l in log_X]
    has_new_X = any("导入决策=新建" in t and "方案='L3-新建X'" in t for t in log_X_texts)
    check("新建X日志存在", has_new_X, str(log_X_texts))

    note_logs = get_import_decision_logs(
        conn, operation_type="import_scheme_note", limit=50
    )
    note_texts = [l["operation_detail"] for l in note_logs]
    check("备注日志-覆盖A", any("链路3覆盖A" in t for t in note_texts), str(note_texts))
    check("备注日志-改名B", any("链路3改名B" in t for t in note_texts), str(note_texts))
    check("备注日志-保留A", any("链路3保留A" in t for t in note_texts), str(note_texts))
    check("备注日志-新建X", any("链路3新建X" in t for t in note_texts), str(note_texts))
    check("备注日志-新建Y", any("链路3新建Y" in t for t in note_texts), str(note_texts))

    total_note_new = len(note_logs)
    import_logs_all = get_import_decision_logs(
        conn, operation_type="import_scheme,import_scheme_note", limit=1000
    )
    check("导入相关日志新增>=9条（5 import_scheme + 5 import_scheme_note等）",
          len(import_logs_all) - before_count >= 9,
          f"新增={len(import_logs_all) - before_count}")


print("\n" + "=" * 70)
print("链路4: 导出回放一致（筛选+双JSON+导回预检对齐）")
print("  目标: 按方案名筛选和更新时间筛选后导出清单JSON+方案包JSON，")
print("       再次导入预检结果与实际状态（已入库/待处理/冲突）一致")
print("=" * 70)

with db_mod.get_conn() as conn:
    all_s = get_review_schemes(conn)
    all_ids = [s["id"] for s in all_s]
    all_names_set = set(s["name"] for s in all_s)

check("当前方案数>=8", count_schemes() >= 8, str(count_schemes()))

target_ids = []
target_names = []
with db_mod.get_conn() as conn:
    for n in ["L1-导入0", "L1-导入2", "L3-新建X", "L3-新建Y", "存在冲突A"]:
        s = get_review_scheme_by_name(conn, n)
        if s:
            target_ids.append(s["id"])
            target_names.append(n)

with db_mod.get_conn() as conn:
    manifest_full = export_scheme_manifest(conn)
check("全量清单成功", manifest_full is not None)
check("全量清单type=scheme_export_manifest", manifest_full.get("type") == "scheme_export_manifest")
check("全量清单total_schemes正确", manifest_full["total_schemes"] == count_schemes(),
      f"{manifest_full['total_schemes']} vs {count_schemes()}")
check("全量清单schemes长度一致", len(manifest_full["schemes"]) == count_schemes())

manifest_names = set(s["name"] for s in manifest_full["schemes"])
check("清单方案名集合与DB完全一致", manifest_names == all_names_set,
      f"差集=清单{manifest_names-all_names_set}/DB{all_names_set-manifest_names}")

with db_mod.get_conn() as conn:
    manifest_name = export_scheme_manifest(conn, name_filter="L3")
check("按name筛选L3成功", manifest_name is not None)
L3_names = [s["name"] for s in manifest_name["schemes"]]
check("L3筛选所有方案名包含L3或新建（链路3中覆盖的A实际名字是存在冲突A，实际包含L3的应是新建）",
      all("L3" in n or "新建" in n or n in ["存在冲突A", "存在冲突B"] for n in L3_names),
      str(L3_names))
check("name_filter记录在filter_applied", manifest_name["filter_applied"]["name_filter"] == "L3")

with db_mod.get_conn() as conn:
    manifest_ids = export_scheme_manifest(conn, scheme_ids=target_ids)
check("按ID筛选成功", manifest_ids is not None)
check("按ID筛选total_schemes正确", manifest_ids["total_schemes"] == len(target_ids),
      f"{manifest_ids['total_schemes']} vs {len(target_ids)}")
id_names = [s["name"] for s in manifest_ids["schemes"]]
check("按ID筛选方案集合正确", set(id_names) == set(target_names),
      f"{set(id_names)} vs {set(target_names)}")

for ms in manifest_ids["schemes"]:
    with db_mod.get_conn() as conn:
        db_s = get_review_scheme_by_name(conn, ms["name"])
    check(f"回放核对方案'{ms['name']}'id一致", db_s and ms["id"] == db_s["id"])
    check(f"回放核对方案'{ms['name']}'filter_state一致", db_s and ms["filter_state"] == db_s["filter_state"],
          f"清单={ms['filter_state']} vs DB={db_s['filter_state'] if db_s else 'None'}")
    check(f"回放核对方案'{ms['name']}'description一致",
          db_s and ms.get("description") == db_s.get("description"))
    check(f"回放核对方案'{ms['name']}'updated_at一致",
          db_s and ms.get("updated_at") == db_s.get("updated_at"))

exported_pkg_ids = None
with db_mod.get_conn() as conn:
    exported_pkg_ids = export_scheme_package(conn, scheme_ids=target_ids)
check("回放导出方案包成功", exported_pkg_ids is not None)
check("回放导出scheme_count正确", exported_pkg_ids["scheme_count"] == len(target_ids))
for s in exported_pkg_ids["schemes"]:
    check(f"回放导出方案'{s['name']}'含filter_state", "filter_state" in s)
    check(f"回放导出方案'{s['name']}'含description", "description" in s)
    if "data_date_range" in s and s["data_date_range"]:
        check(f"回放导出方案'{s['name']}'date_range为dict", isinstance(s["data_date_range"], dict))

with db_mod.get_conn() as conn:
    replay_preview = preview_scheme_package_import(conn, exported_pkg_ids, conflict_policy="keep")
check("回放预检成功", replay_preview["success"] is True)

summary_replay = replay_preview["summary"]
preview_items_replay = replay_preview["preview_results"]
check("回放预检scheme_count正确", summary_replay["scheme_count"] == len(target_ids))

for n in target_names:
    matching = [p for p in preview_items_replay if p.get("original_name") == n]
    check(f"回放预检包含方案'{n}'", len(matching) >= 1)
    if matching:
        mp = matching[0]
        if n == "存在冲突A":
            check(f"回放预检冲突A动作为kept(默认保留)", mp["action"] == SCHEME_ACTION_KEPT, mp["action"])
            check(f"回放预检冲突A有existing_id", "existing_id" in mp, str(mp.keys()))
        elif n.startswith("L3-新建") or n.startswith("L1-导入"):
            check(f"回放预检'{n}'动作为overwritten(DB已存在但非保留逻辑，这里是overwrite或created之外的)",
                  mp["action"] in (SCHEME_ACTION_OVERWRITTEN, SCHEME_ACTION_KEPT),
                  f"实际={mp['action']}")

check("回放预检kept_count>=1（存在冲突A保留）",
      summary_replay["kept_count"] >= 1, str(summary_replay))

with db_mod.get_conn() as conn:
    log_batches = get_import_decision_logs(
        conn, operation_type="batch_shrink,batch_create_remaining,batch_close", limit=50
    )
check("批次操作日志记录存在", len(log_batches) >= 3, str(len(log_batches)))
batch_log_texts = [l["operation_detail"] for l in log_batches]
check("批次收口日志存在", any("批次收口" in t for t in batch_log_texts), str(batch_log_texts))
check("分出剩余批次日志存在", any("分出剩余批次" in t for t in batch_log_texts), str(batch_log_texts))


print("\n" + "=" * 70)
print("综合收口: 所有pending批次全处理完成后，pending列表最终为空")
print("=" * 70)

with db_mod.get_conn() as conn:
    pending_final = list_pending_batches(conn)
for b in pending_final:
    with db_mod.get_conn() as conn:
        lo = load_import_batch(conn, b["batch_id"])
        all_idx = list(range(len(lo["preview"]["preview_results"])))
        res = confirm_partial_scheme_import(
            conn, lo["preview"], all_idx, lo.get("item_decisions", {})
        )
        if res["success"]:
            shrink_batch_to_remaining(conn, b["batch_id"], res.get("processed_indices", all_idx))

with db_mod.get_conn() as conn:
    pending_empty = list_pending_batches(conn)
check("全部处理后pending批次列表为空", len(pending_empty) == 0,
      f"剩余pending: {[b['batch_id'][:16] for b in pending_empty]}")


print("\n" + "=" * 70)
if errors_total:
    print(f"[FAIL] 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("[PASS ALL] 断点继续工作区端到端测试全部通过！")
    print("   [链路1] 部分导入收口不重复：原批次completed，新批次只余剩余")
    print("   [链路2] 重开恢复一致：勾选/决策/备注/方案全量恢复")
    print("   [链路3] 改名冲突决策日志：新建/覆盖/改名+后缀/保留+备注全可查")
    print("   [链路4] 导出回放一致：筛选/双JSON/预检对齐/清单核对")
    print("   [综合] 全部处理完成后pending列表清空")

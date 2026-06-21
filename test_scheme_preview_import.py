"""
复盘方案包预检-确认链路回归测试
覆盖:
  1. 无冲突导入: 预检 -> 确认 -> 结果与预览一致
  2. 改名冲突导入: 预检显示改名预览 -> 确认 -> 结果与预览一致
  3. 预检取消: 不写SQLite，不留import_scheme日志
  4. 坏包拦截: 损坏包直接拦截，不污染数据库
  5. 导出后再导入: 内容完整不丢失
  6. 预检上下文持久化: 应用重启后可恢复
  7. 最近导入策略记忆: 下次导入自动恢复上次选择
  8. 预检阶段只读: 不修改数据库，不写操作日志
  9. 预览与实际导入一致性: 确认导入结果与预览对得上
"""
import os
import sys
import json
import importlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "inventory_diff_scheme_preview_test.db")

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
)

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


def create_test_scheme(name, store="S001", desc=None):
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
    dr = {"min_date": "2026-01-01", "max_date": "2026-06-30"}
    with get_conn() as conn:
        r = save_review_scheme(conn, name, fs, description=desc, data_date_range=dr)
    return r


errors_total = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        errors_total.append((name, detail))


def get_scheme_count():
    with get_conn() as conn:
        return len(get_review_schemes(conn))


def get_import_log_count():
    with get_conn() as conn:
        logs = get_scheme_operation_logs(conn, limit=1000)
    return sum(1 for l in logs if l["operation_type"] == "import_scheme")


print("\n" + "=" * 70)
print("准备基础数据")
print("=" * 70)

with get_conn() as conn:
    clear_import_preview_context(conn)

create_test_scheme("现有方案A", store="S001", desc="方案A原始描述")
create_test_scheme("现有方案B", store="S002", desc="方案B原始描述")

initial_count = get_scheme_count()
initial_log_count = get_import_log_count()
print(f"  初始方案数: {initial_count}, 初始import日志数: {initial_log_count}")
check("初始方案数为2", initial_count == 2, str(initial_count))


print("\n" + "=" * 70)
print("测试 1: 预检阶段只读 - 不修改数据库，不留日志")
print("=" * 70)

no_conflict_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 2,
    "schemes": [
        {
            "name": "新方案1",
            "description": "第一个新方案",
            "filter_state": {"store_id": "NEW1", "barcode": "", "status": "全部"},
            "data_date_range": {"min_date": "2026-03-01", "max_date": "2026-05-31"},
        },
        {
            "name": "新方案2",
            "description": "第二个新方案",
            "filter_state": {"store_id": "NEW2", "barcode": "123", "status": "pending_review"},
        },
    ],
}

with get_conn() as conn:
    before_preview_count = get_scheme_count()
    before_preview_logs = get_import_log_count()

    preview = preview_scheme_package_import(conn, no_conflict_pkg, conflict_policy="keep")

    after_preview_count = get_scheme_count()
    after_preview_logs = get_import_log_count()

check("预检返回success=True", preview.get("success") is True)
check("预检返回valid=True", preview.get("valid") is True)
check("预检不修改数据库方案数", after_preview_count == before_preview_count,
      f"{before_preview_count} -> {after_preview_count}")
check("预检不留下import_scheme日志", after_preview_logs == before_preview_logs,
      f"{before_preview_logs} -> {after_preview_logs}")

summary = preview.get("summary", {})
check("预检summary包含scheme_count=2", summary.get("scheme_count") == 2)
check("预检summary包含created_count=2", summary.get("created_count") == 2)
check("预检summary包含overwritten_count=0", summary.get("overwritten_count") == 0)
check("预检summary包含renamed_count=0", summary.get("renamed_count") == 0)
check("预检summary包含kept_count=0", summary.get("kept_count") == 0)
check("预检summary包含exported_at", summary.get("exported_at") is not None)
check("预检summary包含min_date", summary.get("min_date") == "2026-03-01")
check("预检summary包含max_date", summary.get("max_date") == "2026-05-31")
check("预检summary包含conflict_policy", summary.get("conflict_policy") == "keep")

preview_results = preview.get("preview_results", [])
check("preview_results有2条", len(preview_results) == 2)
check("第1条action=created", preview_results[0]["action"] == SCHEME_ACTION_CREATED)
check("第1条name=新方案1", preview_results[0]["name"] == "新方案1")
check("第2条action=created", preview_results[1]["action"] == SCHEME_ACTION_CREATED)
check("第2条name=新方案2", preview_results[1]["name"] == "新方案2")

check("preview包含package", preview.get("package") is not None)
check("preview包含scheme_names", len(preview.get("scheme_names", [])) == 2)

print("\n" + "=" * 70)
print("测试 2: 无冲突导入 - 预检后确认导入，结果与预览一致")
print("=" * 70)

before_confirm_count = get_scheme_count()
before_confirm_logs = get_import_log_count()

with get_conn() as conn:
    save_import_preview_context(conn, preview)

with get_conn() as conn:
    result = confirm_scheme_package_import(conn, preview)

after_confirm_count = get_scheme_count()
after_confirm_logs = get_import_log_count()

check("确认导入返回success=True", result.get("success") is True)
check("确认导入imported_count=2", result.get("imported_count") == 2)
check("确认导入skipped_count=0", result.get("skipped_count") == 0)
check("确认导入total=2", result.get("total") == 2)

check("确认导入后方案数+2", after_confirm_count == before_confirm_count + 2,
      f"{before_confirm_count} -> {after_confirm_count}")
check("确认导入后import日志+2", after_confirm_logs == before_confirm_logs + 2,
      f"{before_confirm_logs} -> {after_confirm_logs}")

check("实际导入action与预览一致",
      all(r["action"] == p["action"] for r, p in zip(result["results"], preview_results)))
check("实际导入name与预览一致",
      all(r["name"] == p["name"] for r, p in zip(result["results"], preview_results)))

with get_conn() as conn:
    s1 = get_review_scheme_by_name(conn, "新方案1")
    s2 = get_review_scheme_by_name(conn, "新方案2")
check("新方案1已写入数据库", s1 is not None)
check("新方案1描述正确", s1.get("description") == "第一个新方案")
check("新方案1filter_state正确", s1["filter_state"].get("store_id") == "NEW1")
check("新方案2已写入数据库", s2 is not None)

with get_conn() as conn:
    ctx_after = load_import_preview_context(conn)
check("确认导入后预览上下文已清除", ctx_after is None)

print("\n" + "=" * 70)
print("测试 3: 改名冲突导入 - 预检显示改名预览，结果与预览一致")
print("=" * 70)

rename_conflict_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 2,
    "schemes": [
        {
            "name": "现有方案A",
            "description": "改名导入的方案A",
            "filter_state": {"store_id": "RENAME_A", "status": "confirmed"},
        },
        {
            "name": "现有方案B",
            "description": "改名导入的方案B",
            "filter_state": {"store_id": "RENAME_B", "status": "pending_review"},
        },
    ],
}

with get_conn() as conn:
    before_preview = get_scheme_count()
    before_logs = get_import_log_count()

    preview_rename = preview_scheme_package_import(
        conn, rename_conflict_pkg, conflict_policy="rename", rename_suffix="(导入)"
    )

    after_preview = get_scheme_count()
    after_logs = get_import_log_count()

check("改名预检不修改数据库", after_preview == before_preview)
check("改名预检不留日志", after_logs == before_logs)

summary_rename = preview_rename["summary"]
check("改名预检renamed_count=2", summary_rename["renamed_count"] == 2)
check("改名预检created_count=0", summary_rename["created_count"] == 0)

pr_rename = preview_rename["preview_results"]
check("第1条action=renamed", pr_rename[0]["action"] == SCHEME_ACTION_RENAMED)
check("第1条original_name=现有方案A", pr_rename[0]["original_name"] == "现有方案A")
check("第1条name包含(导入)", "(导入)" in pr_rename[0]["name"], pr_rename[0]["name"])
check("第2条action=renamed", pr_rename[1]["action"] == SCHEME_ACTION_RENAMED)
check("第2条name包含(导入)", "(导入)" in pr_rename[1]["name"], pr_rename[1]["name"])

with get_conn() as conn:
    result_rename = confirm_scheme_package_import(conn, preview_rename)

check("改名导入成功", result_rename["success"] is True)
check("改名导入imported_count=2", result_rename["imported_count"] == 2)
check("改名导入skipped_count=0", result_rename["skipped_count"] == 0)

check("实际改名与预览一致",
      all(r["name"] == p["name"] for r, p in zip(result_rename["results"], pr_rename)))
check("实际original_name与预览一致",
      all(r.get("original_name") == p.get("original_name")
          for r, p in zip(result_rename["results"], pr_rename)))

with get_conn() as conn:
    orig_a = get_review_scheme_by_name(conn, "现有方案A")
    orig_b = get_review_scheme_by_name(conn, "现有方案B")
    renamed_a = get_review_scheme_by_name(conn, pr_rename[0]["name"])
    renamed_b = get_review_scheme_by_name(conn, pr_rename[1]["name"])

check("原方案A仍存在", orig_a is not None)
check("原方案A描述未变", orig_a.get("description") == "方案A原始描述")
check("原方案B仍存在", orig_b is not None)
check("改名后的方案A存在", renamed_a is not None)
check("改名后的方案A描述正确", renamed_a.get("description") == "改名导入的方案A")
check("改名后的方案B存在", renamed_b is not None)

print("\n" + "=" * 70)
print("测试 4: 预检取消 - 不写SQLite，不留import_scheme日志")
print("=" * 70)

cancel_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {
            "name": "取消测试方案",
            "description": "这个方案不应该被写入",
            "filter_state": {"store_id": "CANCEL", "status": "全部"},
        },
    ],
}

with get_conn() as conn:
    before_cancel_count = get_scheme_count()
    before_cancel_logs = get_import_log_count()

    preview_cancel = preview_scheme_package_import(conn, cancel_pkg, conflict_policy="keep")

    save_import_preview_context(conn, preview_cancel)

    after_preview_cancel_count = get_scheme_count()
    after_preview_cancel_logs = get_import_log_count()

check("取消预检不修改数据库", after_preview_cancel_count == before_cancel_count)
check("取消预检不留日志", after_preview_cancel_logs == before_cancel_logs)

with get_conn() as conn:
    clear_import_preview_context(conn)

    after_cancel_count = get_scheme_count()
    after_cancel_logs = get_import_log_count()

    scheme_cancel = get_review_scheme_by_name(conn, "取消测试方案")

check("取消后方案数不变", after_cancel_count == before_cancel_count,
      f"{before_cancel_count} -> {after_cancel_count}")
check("取消后日志数不变", after_cancel_logs == before_cancel_logs,
      f"{before_cancel_logs} -> {after_cancel_logs}")
check("取消测试方案未写入数据库", scheme_cancel is None)

with get_conn() as conn:
    ctx_cancel = load_import_preview_context(conn)
check("取消后预览上下文已清除", ctx_cancel is None)

print("\n" + "=" * 70)
print("测试 5: 坏包拦截 - 损坏包直接拦截，不污染数据库")
print("=" * 70)

with get_conn() as conn:
    before_bad_count = get_scheme_count()
    before_bad_logs = get_import_log_count()

bad_packages = [
    ("scheme_count不一致多报", {
        "version": "1.0",
        "exported_at": now_iso(),
        "scheme_count": 99,
        "schemes": [
            {"name": "坏包方案1", "filter_state": {"store_id": "BAD1"}},
            {"name": "坏包方案2", "filter_state": {"store_id": "BAD2"}},
        ],
    }),
    ("scheme_count不一致少报", {
        "version": "1.0",
        "exported_at": now_iso(),
        "scheme_count": 1,
        "schemes": [
            {"name": "坏包方案1", "filter_state": {"store_id": "BAD1"}},
            {"name": "坏包方案2", "filter_state": {"store_id": "BAD2"}},
        ],
    }),
    ("缺version字段", {
        "exported_at": now_iso(),
        "scheme_count": 1,
        "schemes": [{"name": "坏包方案3", "filter_state": {}}],
    }),
    ("schemes为空", {
        "version": "1.0",
        "exported_at": now_iso(),
        "scheme_count": 0,
        "schemes": [],
    }),
    ("方案缺name", {
        "version": "1.0",
        "exported_at": now_iso(),
        "scheme_count": 1,
        "schemes": [{"filter_state": {}}],
    }),
    ("方案缺filter_state", {
        "version": "1.0",
        "exported_at": now_iso(),
        "scheme_count": 1,
        "schemes": [{"name": "坏包方案4"}],
    }),
]

for label, bad_pkg in bad_packages:
    with get_conn() as conn:
        v = validate_scheme_package(bad_pkg)
        check(f"坏包'{label}'校验失败", not v["valid"], v.get("error"))

        p = preview_scheme_package_import(conn, bad_pkg)
        check(f"坏包'{label}'预检失败", not p.get("success"), p.get("error"))

        r = import_scheme_package(conn, bad_pkg)
        check(f"坏包'{label}'导入失败", not r["success"], r.get("error"))

with get_conn() as conn:
    after_bad_count = get_scheme_count()
    after_bad_logs = get_import_log_count()

    all_schemes = get_review_schemes(conn)
    all_names = [s["name"] for s in all_schemes]

check("坏包不污染数据库", after_bad_count == before_bad_count,
      f"前: {before_bad_count}, 后: {after_bad_count}")
check("坏包不留import_scheme日志", after_bad_logs == before_bad_logs,
      f"前: {before_bad_logs}, 后: {after_bad_logs}")
check("坏包方案1未写入", "坏包方案1" not in all_names)
check("坏包方案2未写入", "坏包方案2" not in all_names)
check("坏包方案3未写入", "坏包方案3" not in all_names)
check("坏包方案4未写入", "坏包方案4" not in all_names)

print("\n" + "=" * 70)
print("测试 6: 导出后再导入 - 内容完整不丢失")
print("=" * 70)

with get_conn() as conn:
    export_test = create_test_scheme(
        "往返测试方案", store="ROUNDTRIP", desc="往返测试描述"
    )
    scheme_id = export_test["scheme_id"]

    all_before_export = get_review_schemes(conn)
    count_before_export = len(all_before_export)

    pkg_rt = export_scheme_package(conn, scheme_ids=[scheme_id])

    delete_review_scheme(conn, scheme_id)

    after_delete = get_review_schemes(conn)
    count_after_delete = len(after_delete)

check("导出成功", pkg_rt is not None)
check("导出包含scheme_count=1", pkg_rt.get("scheme_count") == 1)
check("删除后方案数-1", count_after_delete == count_before_export - 1)

with get_conn() as conn:
    preview_rt = preview_scheme_package_import(conn, pkg_rt, conflict_policy="keep")

check("往返预检成功", preview_rt["success"] is True)
check("往返预检created_count=1", preview_rt["summary"]["created_count"] == 1)

with get_conn() as conn:
    result_rt = confirm_scheme_package_import(conn, preview_rt)

check("往返导入成功", result_rt["success"] is True)
check("往返导入imported_count=1", result_rt["imported_count"] == 1)

with get_conn() as conn:
    rt_imported = get_review_scheme_by_name(conn, "往返测试方案")

check("往返导入后方案存在", rt_imported is not None)
check("往返: name不丢", rt_imported["name"] == "往返测试方案")
check("往返: description不丢", rt_imported.get("description") == "往返测试描述")
check("往返: store_id不丢", rt_imported["filter_state"].get("store_id") == "ROUNDTRIP")
check("往返: data_date_range不丢",
      rt_imported.get("data_date_range", {}).get("min_date") == "2026-01-01")

print("\n" + "=" * 70)
print("测试 7: 预检上下文持久化 - 应用重启后可恢复")
print("=" * 70)

persist_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {
            "name": "持久化测试方案",
            "description": "持久化测试",
            "filter_state": {"store_id": "PERSIST", "status": "confirmed"},
        },
    ],
}

with get_conn() as conn:
    preview_persist = preview_scheme_package_import(
        conn, persist_pkg, conflict_policy="overwrite", rename_suffix="(测试后缀)"
    )
    save_import_preview_context(conn, preview_persist)

importlib.reload(db_mod)
db_mod.DB_PATH = DB_PATH

with db_mod.get_conn() as conn:
    loaded = db_mod.load_import_preview_context(conn)
    last_policy = db_mod.load_last_import_policy(conn)

check("重启后预览上下文可恢复", loaded is not None)
check("恢复的预览success=True", loaded.get("success") is True)
check("恢复的预览scheme_count=1", loaded.get("summary", {}).get("scheme_count") == 1)
check("恢复的预览conflict_policy=overwrite",
      loaded.get("summary", {}).get("conflict_policy") == "overwrite")
check("恢复的预览rename_suffix正确",
      loaded.get("summary", {}).get("rename_suffix") == "(测试后缀)")
check("恢复的预览包含package", loaded.get("package") is not None)

check("重启后最近策略可恢复", last_policy is not None)
check("最近策略conflict_policy=overwrite", last_policy.get("conflict_policy") == "overwrite")
check("最近策略rename_suffix正确", last_policy.get("rename_suffix") == "(测试后缀)")

with db_mod.get_conn() as conn:
    result_persist = db_mod.confirm_scheme_package_import(conn, loaded)

check("从恢复的上下文导入成功", result_persist["success"] is True)
check("从恢复的上下文导入imported_count=1", result_persist["imported_count"] == 1)

with db_mod.get_conn() as conn:
    s_persist = db_mod.get_review_scheme_by_name(conn, "持久化测试方案")
check("持久化测试方案已写入", s_persist is not None)

print("\n" + "=" * 70)
print("测试 8: 最近导入策略记忆 - 下次导入自动恢复上次选择")
print("=" * 70)

policy_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {"name": "策略测试方案", "filter_state": {"store_id": "POLICY"}},
    ],
}

with get_conn() as conn:
    preview_policy = preview_scheme_package_import(
        conn, policy_pkg, conflict_policy="rename", rename_suffix="(策略后缀)"
    )
    save_import_preview_context(conn, preview_policy)

    last_pol = load_last_import_policy(conn)

check("保存策略后可读取", last_pol is not None)
check("策略conflict_policy=rename", last_pol.get("conflict_policy") == "rename")
check("策略rename_suffix正确", last_pol.get("rename_suffix") == "(策略后缀)")

importlib.reload(db_mod)
db_mod.DB_PATH = DB_PATH

with db_mod.get_conn() as conn:
    last_pol_after = db_mod.load_last_import_policy(conn)

check("重启后策略仍可恢复", last_pol_after is not None)
check("重启后conflict_policy=rename", last_pol_after.get("conflict_policy") == "rename")
check("重启后rename_suffix正确", last_pol_after.get("rename_suffix") == "(策略后缀)")

print("\n" + "=" * 70)
print("测试 9: 预览与实际导入一致性 - 覆盖场景")
print("=" * 70)

overwrite_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {
            "name": "现有方案A",
            "description": "覆盖后的新描述",
            "filter_state": {"store_id": "OVERWRITE", "status": "closed"},
        },
    ],
}

with get_conn() as conn:
    before_orig = get_review_scheme_by_name(conn, "现有方案A")
    before_orig_desc = before_orig.get("description")

    preview_overwrite = preview_scheme_package_import(
        conn, overwrite_pkg, conflict_policy="overwrite"
    )

check("覆盖预检success=True", preview_overwrite["success"] is True)
check("覆盖预检overwritten_count=1", preview_overwrite["summary"]["overwritten_count"] == 1)

pr_over = preview_overwrite["preview_results"][0]
check("覆盖预检action=overwritten", pr_over["action"] == SCHEME_ACTION_OVERWRITTEN)
check("覆盖预检name=现有方案A", pr_over["name"] == "现有方案A")
check("覆盖预检existing_id正确", pr_over.get("existing_id") == before_orig["id"])
check("覆盖预检existing_description正确", pr_over.get("existing_description") == before_orig_desc)

with get_conn() as conn:
    result_overwrite = confirm_scheme_package_import(conn, preview_overwrite)

check("覆盖导入success=True", result_overwrite["success"] is True)
check("覆盖导入imported_count=1", result_overwrite["imported_count"] == 1)
check("覆盖导入skipped_count=0", result_overwrite["skipped_count"] == 0)

r_over = result_overwrite["results"][0]
check("实际覆盖action与预览一致", r_over["action"] == pr_over["action"])
check("实际覆盖name与预览一致", r_over["name"] == pr_over["name"])
check("实际覆盖scheme_id与预览一致", r_over["scheme_id"] == pr_over["existing_id"])

with get_conn() as conn:
    after_over = get_review_scheme_by_name(conn, "现有方案A")
check("覆盖后描述已更新", after_over.get("description") == "覆盖后的新描述")
check("覆盖后filter_state已更新", after_over["filter_state"].get("store_id") == "OVERWRITE")

print("\n" + "=" * 70)
print("测试 10: 预览与实际导入一致性 - 保留场景")
print("=" * 70)

keep_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {
            "name": "现有方案B",
            "description": "不应该被覆盖的描述",
            "filter_state": {"store_id": "KEEP", "status": "pending_review"},
        },
    ],
}

with get_conn() as conn:
    before_keep = get_review_scheme_by_name(conn, "现有方案B")
    before_keep_desc = before_keep.get("description")
    before_keep_store = before_keep["filter_state"].get("store_id")

    preview_keep = preview_scheme_package_import(conn, keep_pkg, conflict_policy="keep")

check("保留预检success=True", preview_keep["success"] is True)
check("保留预检kept_count=1", preview_keep["summary"]["kept_count"] == 1)

pr_keep = preview_keep["preview_results"][0]
check("保留预检action=kept", pr_keep["action"] == SCHEME_ACTION_KEPT)
check("保留预检包含reason", "保留原方案" in pr_keep.get("reason", ""))

with get_conn() as conn:
    result_keep = confirm_scheme_package_import(conn, preview_keep)

check("保留导入success=True", result_keep["success"] is True)
check("保留导入imported_count=0", result_keep["imported_count"] == 0)
check("保留导入skipped_count=1", result_keep["skipped_count"] == 1)

r_keep = result_keep["results"][0]
check("实际保留action与预览一致", r_keep["action"] == pr_keep["action"])
check("实际保留name与预览一致", r_keep["name"] == pr_keep["name"])

with get_conn() as conn:
    after_keep = get_review_scheme_by_name(conn, "现有方案B")
check("保留后描述未变", after_keep.get("description") == before_keep_desc)
check("保留后filter_state未变", after_keep["filter_state"].get("store_id") == before_keep_store)

print("\n" + "=" * 70)
print("测试 11: 预检后数据库变化导致结果不一致的检测")
print("=" * 70)

race_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {"name": "并发测试方案", "filter_state": {"store_id": "RACE"}},
    ],
}

with get_conn() as conn:
    preview_race = preview_scheme_package_import(conn, race_pkg, conflict_policy="keep")

check("并发预检created_count=1", preview_race["summary"]["created_count"] == 1)

with get_conn() as conn:
    create_test_scheme("并发测试方案", store="RACE2", desc="预检后插入的")

    before_race = get_review_scheme_by_name(conn, "并发测试方案")
    check("预检后手动插入的方案存在", before_race is not None)

    result_race = confirm_scheme_package_import(conn, preview_race)

check("并发导入仍成功", result_race["success"] is True)
check("并发导入action=kept(冲突)",
      result_race["results"][0]["action"] == SCHEME_ACTION_KEPT)
check("并发导入imported_count=0", result_race["imported_count"] == 0)

with get_conn() as conn:
    delete_review_scheme(conn, before_race["id"])

print("\n" + "=" * 70)
print("测试 12: 旧API import_scheme_package 向后兼容")
print("=" * 70)

compat_pkg = {
    "version": "1.0",
    "exported_at": now_iso(),
    "scheme_count": 1,
    "schemes": [
        {"name": "兼容测试方案", "filter_state": {"store_id": "COMPAT"}},
    ],
}

with get_conn() as conn:
    before_compat = get_scheme_count()
    result_compat = import_scheme_package(conn, compat_pkg, conflict_policy="keep")

check("旧API导入成功", result_compat["success"] is True)
check("旧API imported_count=1", result_compat["imported_count"] == 1)
check("旧API total=1", result_compat["total"] == 1)

with get_conn() as conn:
    after_compat = get_scheme_count()
    compat_scheme = get_review_scheme_by_name(conn, "兼容测试方案")

check("旧API方案数+1", after_compat == before_compat + 1)
check("旧API方案已写入", compat_scheme is not None)

print("\n" + "=" * 70)
if errors_total:
    print(f"[FAIL] 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("[全部通过] 预检-确认链路回归测试全部通过!")
    print("   [OK] 预检阶段只读 - 不修改数据库，不留日志")
    print("   [OK] 无冲突导入 - 预检后确认，结果与预览一致")
    print("   [OK] 改名冲突导入 - 预检显示改名预览，结果一致")
    print("   [OK] 预检取消 - 不写SQLite，不留import_scheme日志")
    print("   [OK] 坏包拦截 - 损坏包直接拦截，不污染数据库")
    print("   [OK] 导出后再导入 - 内容完整不丢失")
    print("   [OK] 预检上下文持久化 - 应用重启后可恢复")
    print("   [OK] 最近导入策略记忆 - 重启后仍可恢复")
    print("   [OK] 覆盖场景 - 预览与实际导入一致")
    print("   [OK] 保留场景 - 预览与实际导入一致")
    print("   [OK] 并发场景检测 - 预检后DB变化可处理")
    print("   [OK] 旧API向后兼容 - import_scheme_package正常工作")

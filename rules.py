import json
from db import (
    get_conn, get_active_rule_version, get_all_rule_versions,
    insert_rule_version, validate_rule_config, now_iso, DEFAULT_RULES,
)
from engine import run_attribution


def save_rule_config(new_config):
    errors = validate_rule_config(new_config)
    if errors:
        return {"success": False, "errors": errors, "message": "规则校验失败，旧规则已保留"}

    with get_conn() as conn:
        try:
            ver = insert_rule_version(conn, new_config)
        except Exception as e:
            return {"success": False, "errors": [str(e)], "message": "保存失败，旧规则已保留"}

    attr_result = run_attribution()
    if attr_result.get("success"):
        return {
            "success": True,
            "version": ver,
            "message": (f"规则 v{ver} 保存成功，新增 {attr_result.get('created', 0)} 条差异"
                        f"（已有 {attr_result.get('skipped', 0)} 条差异保留原归因快照不变）"),
            "recomputed": attr_result,
        }
    else:
        return {
            "success": True,
            "version": ver,
            "message": f"规则 v{ver} 保存成功（差异重算跳过: {attr_result.get('error', '')}）",
        }


def get_current_config():
    with get_conn() as conn:
        rv = get_active_rule_version(conn)
        if rv:
            return json.loads(rv["config_json"]), rv["version"]
        return DEFAULT_RULES.copy(), 0


def get_version_history():
    with get_conn() as conn:
        return get_all_rule_versions(conn)

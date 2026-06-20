import os
import csv

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data")

INVENTORY_ROWS = [
    {"store_id": "S001", "barcode": "6901234567890", "sku_name": "矿泉水550ml", "system_qty": 100},
    {"store_id": "S001", "barcode": "6901234567891", "sku_name": "可乐330ml", "system_qty": 80},
    {"store_id": "S001", "barcode": "6901234567892", "sku_name": "薯片原味", "system_qty": 50},
    {"store_id": "S001", "barcode": "6901234567893", "sku_name": "牛奶1L", "system_qty": 30},
    {"store_id": "S001", "barcode": "6901234567894", "sku_name": "面包切片", "system_qty": 20},
    {"store_id": "S002", "barcode": "6901234567890", "sku_name": "矿泉水550ml", "system_qty": 120},
    {"store_id": "S002", "barcode": "6901234567891", "sku_name": "可乐330ml", "system_qty": 90},
    {"store_id": "S002", "barcode": "6901234567892", "sku_name": "薯片原味", "system_qty": 60},
    {"store_id": "S002", "barcode": "6901234567895", "sku_name": "橙汁1L", "system_qty": 40},
    {"store_id": "S002", "barcode": "6901234567896", "sku_name": "饼干奶油味", "system_qty": 35},
]

SALES_ROWS = [
    {"store_id": "S001", "barcode": "6901234567890", "sku_name": "矿泉水550ml", "sale_qty": 8, "sale_date": "2025-06-18"},
    {"store_id": "S001", "barcode": "6901234567891", "sku_name": "可乐330ml", "sale_qty": 5, "sale_date": "2025-06-19"},
    {"store_id": "S002", "barcode": "6901234567895", "sku_name": "橙汁1L", "sale_qty": 3, "sale_date": "2025-06-19"},
]

TRANSFER_ROWS = [
    {"store_id_from": "S001", "store_id_to": "S002", "barcode": "6901234567892", "sku_name": "薯片原味", "transfer_qty": 10, "transfer_date": "2025-06-19"},
    {"store_id_from": "S002", "store_id_to": "S001", "barcode": "6901234567895", "sku_name": "橙汁1L", "transfer_qty": 5, "transfer_date": "2025-06-20"},
]

STOCKTAKE_ROWS = [
    {"store_id": "S001", "barcode": "6901234567890", "sku_name": "矿泉水550ml", "actual_qty": 88},
    {"store_id": "S001", "barcode": "6901234567891", "sku_name": "可乐330ml", "actual_qty": 73},
    {"store_id": "S001", "barcode": "6901234567892", "sku_name": "薯片原味", "actual_qty": 38},
    {"store_id": "S001", "barcode": "6901234567893", "sku_name": "牛奶1L", "actual_qty": 30},
    {"store_id": "S001", "barcode": "6901234567894", "sku_name": "面包切片", "actual_qty": 15},
    {"store_id": "S002", "barcode": "6901234567890", "sku_name": "矿泉水550ml", "actual_qty": 115},
    {"store_id": "S002", "barcode": "6901234567891", "sku_name": "可乐330ml", "actual_qty": 92},
    {"store_id": "S002", "barcode": "6901234567892", "sku_name": "薯片原味", "actual_qty": 68},
    {"store_id": "S002", "barcode": "6901234567895", "sku_name": "橙汁1L", "actual_qty": 42},
    {"store_id": "S002", "barcode": "6901234567896", "sku_name": "饼干奶油味", "actual_qty": 33},
]

BAD_ROWS_INVENTORY = [
    {"store_id": "S003", "barcode": "", "sku_name": "缺条码商品", "system_qty": 10},
    {"store_id": "S003", "barcode": "6901234567899", "sku_name": "缺数量商品", "system_qty": ""},
    {"store_id": "S003", "barcode": "6901234567900", "sku_name": "", "system_qty": 15},
    {"store_id": "S003", "barcode": "6901234567901", "sku_name": "   ", "system_qty": 20},
    {"store_id": "S003", "barcode": "6901234567902", "sku_name": "库存正常商品", "system_qty": 25},
]

BAD_ROWS_SALES = [
    {"store_id": "S003", "barcode": "", "sku_name": "销售缺条码", "sale_qty": 3, "sale_date": "2025-06-20"},
    {"store_id": "S003", "barcode": "6901234567901", "sku_name": "", "sale_qty": 5, "sale_date": "2025-06-20"},
    {"store_id": "S003", "barcode": "6901234567902", "sku_name": "   ", "sale_qty": 2, "sale_date": "2025-06-20"},
    {"store_id": "S003", "barcode": "6901234567903", "sku_name": "销售缺数量", "sale_qty": "", "sale_date": "2025-06-20"},
    {"store_id": "S003", "barcode": "6901234567904", "sku_name": "销售数量非数值", "sale_qty": "abc", "sale_date": "2025-06-20"},
    {"store_id": "S003", "barcode": "6901234567905", "sku_name": "销售缺日期", "sale_qty": 5, "sale_date": ""},
    {"store_id": "S003", "barcode": "6901234567906", "sku_name": "销售正常行", "sale_qty": 7, "sale_date": "2025-06-20"},
]


def _write_csv(filepath, rows):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    if not rows:
        return
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def generate_sample_data():
    _write_csv(os.path.join(SAMPLE_DIR, "inventory.csv"), INVENTORY_ROWS)
    _write_csv(os.path.join(SAMPLE_DIR, "sales.csv"), SALES_ROWS)
    _write_csv(os.path.join(SAMPLE_DIR, "transfer.csv"), TRANSFER_ROWS)
    _write_csv(os.path.join(SAMPLE_DIR, "stocktake.csv"), STOCKTAKE_ROWS)
    _write_csv(os.path.join(SAMPLE_DIR, "inventory_with_bad_rows.csv"), BAD_ROWS_INVENTORY)
    _write_csv(os.path.join(SAMPLE_DIR, "sales_with_bad_rows.csv"), BAD_ROWS_SALES)
    return SAMPLE_DIR

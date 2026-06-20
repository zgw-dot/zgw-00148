import os
import sys
import time

def setup_stdout_encoding():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, LookupError):
        pass

def safe_remove_db(db_path, max_retries=5, delay=0.3):
    for attempt in range(max_retries):
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
            for suffix in ("-wal", "-shm"):
                p = db_path + suffix
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            return True
        except PermissionError:
            if attempt < max_retries - 1:
                time.sleep(delay)
            else:
                print(f"[WARN] cannot remove {db_path} after {max_retries} attempts, proceeding anyway")
                return False
    return True

def init_test_env(db_path):
    setup_stdout_encoding()
    safe_remove_db(db_path)

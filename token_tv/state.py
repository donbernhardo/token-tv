"""Polling owns provider requests; HTTP readers only see cached state."""
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from token_tv.sources import fetch_account


class UsageStore:
    def __init__(self, accounts, fetch=fetch_account):
        self.accounts = accounts
        self.fetch = fetch
        self.lock = threading.Lock()
        self.updated_at = 0
        self.rows = {a["key"]: {"key": a["key"], "alias": a["alias"], "provider": a["provider"],
                               "status": "loading", "windows": [], "fetched_at": 0,
                               "identity_verified": False, "source": "mini"}
                     for a in accounts}

    def refresh(self):
        def fetch_safely(account):
            try:
                return self.fetch(account)
            except Exception:
                # Account failures must not discard healthy results or expose exception text.
                return {"key": account["key"], "alias": account["alias"], "provider": account["provider"],
                        "status": "error", "error_code": "source_unavailable", "windows": [],
                        "fetched_at": time.time(), "identity_verified": False, "source": "mini"}

        with ThreadPoolExecutor(max_workers=min(4, len(self.accounts))) as executor:
            rows = list(executor.map(fetch_safely, self.accounts))
        with self.lock:
            for row in rows:
                previous = self.rows[row["key"]]
                if row["status"] in ("ok", "quota_unavailable"):
                    row["last_success_at"] = row["fetched_at"]
                elif row["status"] != "identity_mismatch" and previous.get("windows") and previous.get("last_success_at"):
                    row["error_code"] = row.get("error_code", row["status"])
                    row["status"] = "stale"
                    row["windows"] = previous["windows"]
                    row["last_success_at"] = previous["last_success_at"]
                    row["identity_verified"] = previous.get("identity_verified", False)
                self.rows[row["key"]] = row
            self.updated_at = int(time.time())

    def snapshot(self):
        with self.lock:
            rows = copy.deepcopy(self.rows)
            updated = self.updated_at
        now = time.time()
        for row in rows.values():
            if row["status"] == "ok" and now - row.get("last_success_at", row["fetched_at"]) > 900:
                row["status"] = "stale"
                row.setdefault("error_code", "snapshot_old")
            by_label = {w["label"]: w for w in row["windows"]}
            for prefix, label in (("session", "5H"), ("weekly", "WEEK")):
                value = by_label.get(label, {})
                row[prefix + "_percent"] = value.get("used_percent")
                reset = value.get("resets_at")
                row[prefix + "_reset_minutes"] = max(0, int((reset - now + 59) // 60)) if reset else None
        return {"schema": 1, "updated_at": updated, "accounts": rows}

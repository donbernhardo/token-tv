"""Small, explicit usage windows. Missing measurements stay missing."""
import math
import re
from datetime import datetime


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if math.isfinite(value) and value > 0 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.timestamp()) if parsed.tzinfo else None
        except ValueError:
            pass
    return None


def window(label, percent, reset=None, duration=None):
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    if not math.isfinite(percent) or percent < 0:
        return None
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        duration = None
    return {"label": label, "used_percent": float(percent),
            "resets_at": timestamp(reset), "duration_minutes": duration}


def normalize_claude(data):
    result = []
    for key, label, duration in (("five_hour", "5H", 300), ("seven_day", "WEEK", 10080)):
        value = data.get(key) or {}
        item = window(label, value.get("utilization"), value.get("resets_at"), duration)
        if item:
            result.append(item)
    return result


def normalize_codex(data):
    result = []
    limits = data.get("rateLimits") or {}
    for key in ("primary", "secondary"):
        value = limits.get(key) or {}
        duration = value.get("windowDurationMins")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            duration = None
        label = {300: "5H", 10080: "WEEK"}.get(duration, "WINDOW")
        item = window(label, value.get("usedPercent"), value.get("resetsAt"), duration)
        if item:
            result.append(item)
    return result


def extract_codex_banked_resets(data):
    """Extract banked rate-limit reset credits count and earliest expiration timestamp."""
    credits_obj = data.get("rateLimitResetCredits") if isinstance(data, dict) else None
    if not isinstance(credits_obj, dict):
        return None
    count = credits_obj.get("availableCount")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return None
    credits_list = credits_obj.get("credits") or []
    expires_list = []
    for c in credits_list:
        if isinstance(c, dict):
            status = c.get("status")
            if status in (None, "available"):
                exp = timestamp(c.get("expiresAt"))
                if exp:
                    expires_list.append(exp)
    earliest_exp = min(expires_list) if expires_list else None
    return {
        "count": count,
        "earliest_expires_at": earliest_exp,
    }


def normalize_grok(data):
    config = data.get("config") or {}
    percent = config.get("creditUsagePercent")
    cycle = config.get("currentPeriod") or {}
    reset = cycle.get("end") or config.get("billingPeriodEnd")
    item = window("BUDGET", percent, reset)
    if item:
        return [item]
    # CLI credits are a billing budget, not a fabricated web-message quota.
    limit = (data.get("monthlyLimit") or {}).get("val")
    used = ((data.get("usage") or {}).get("includedUsed") or {}).get("val")
    if isinstance(limit, (int, float)) and limit > 0 and isinstance(used, (int, float)):
        item = window("BUDGET", used / limit * 100,
                      (data.get("billingCycle") or {}).get("billingPeriodEnd"))
        if item:
            return [item]
    return []


def normalize_gemini(data):
    result = []
    # 1. Structure from agy command usage data:
    buckets = []
    cmd_data = (data.get("command") or {}).get("data") or {}
    groups = cmd_data.get("groups") or data.get("groups") or []
    for group in groups:
        if "gemini" in group.get("name", "").lower():
            buckets.extend(group.get("buckets", []))
    if not buckets and "buckets" in data:
        buckets = data["buckets"]

    for b in buckets:
        win = b.get("window")
        label = "5H" if win == "5h" else "WEEK" if win in ("weekly", "7d") else "WINDOW"
        duration = 300 if label == "5H" else 10080 if label == "WEEK" else None
        used = None
        if "remaining_fraction" in b and isinstance(b["remaining_fraction"], (int, float)):
            used = max(0.0, min(100.0, round((1.0 - float(b["remaining_fraction"])) * 100.0, 1)))
        elif "used_percent" in b and isinstance(b["used_percent"], (int, float)):
            used = float(b["used_percent"])
        elif "utilization" in b and isinstance(b["utilization"], (int, float)):
            used = float(b["utilization"])
        item = window(label, used, b.get("reset_time") or b.get("resets_at"), duration)
        if item:
            result.append(item)
    if result:
        order = {"5H": 0, "WEEK": 1, "WINDOW": 2}
        result.sort(key=lambda w: order.get(w["label"], 9))
        return result

    # 2. Direct dictionary keys:
    for key, label, duration in (("five_hour", "5H", 300), ("seven_day", "WEEK", 10080), ("weekly", "WEEK", 10080)):
        val = data.get(key) or {}
        used = val.get("utilization") or val.get("used_percent")
        if used is None and "remaining_fraction" in val and isinstance(val["remaining_fraction"], (int, float)):
            used = max(0.0, min(100.0, round((1.0 - float(val["remaining_fraction"])) * 100.0, 1)))
        item = window(label, used, val.get("resets_at") or val.get("reset_time"), duration)
        if item:
            result.append(item)
    if result:
        order = {"5H": 0, "WEEK": 1}
        result.sort(key=lambda w: order.get(w["label"], 9))
        return result

    # 3. Plain text response fallback:
    text = data.get("response") if isinstance(data.get("response"), str) else (data if isinstance(data, str) else "")
    if text:
        for line in text.splitlines():
            if "gemini" in line.lower() and "remaining" in line.lower():
                m = re.search(r'(Weekly|Five\s*Hour)[^%]*?(\d+(?:\.\d+)?)%\s*(.*)', line, re.IGNORECASE)
                if m:
                    kind, rem_str, reset_str = m.group(1), m.group(2), m.group(3).strip()
                    label = "5H" if "five" in kind.lower() else "WEEK"
                    duration = 300 if label == "5H" else 10080
                    used = max(0.0, min(100.0, 100.0 - float(rem_str)))
                    item = window(label, used, reset_str, duration)
                    if item:
                        result.append(item)
        if result:
            order = {"5H": 0, "WEEK": 1}
            result.sort(key=lambda w: order.get(w["label"], 9))
            return result

    return []

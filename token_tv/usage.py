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


def object_value(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Expected a usage object")
    return value


def list_value(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Expected a usage list")
    return value


def normalize_claude(data):
    data = object_value(data)
    result = []
    for key, label, duration in (("five_hour", "5H", 300), ("seven_day", "WEEK", 10080)):
        value = object_value(data.get(key))
        item = window(label, value.get("utilization"), value.get("resets_at"), duration)
        if item:
            result.append(item)
    return result


def normalize_codex(data):
    data = object_value(data)
    result = []
    limits = object_value(data.get("rateLimits"))
    for key in ("primary", "secondary"):
        value = object_value(limits.get(key))
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
    credits_list = list_value(credits_obj.get("credits"))
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
    data = object_value(data)
    config = object_value(data.get("config"))
    percent = config.get("creditUsagePercent")
    cycle = object_value(config.get("currentPeriod"))
    reset = cycle.get("end") or config.get("billingPeriodEnd")
    item = window("BUDGET", percent, reset)
    if item:
        return [item]
    # CLI credits are a billing budget, not a fabricated web-message quota.
    limit = object_value(data.get("monthlyLimit")).get("val")
    used = object_value(object_value(data.get("usage")).get("includedUsed")).get("val")
    if isinstance(limit, (int, float)) and limit > 0 and isinstance(used, (int, float)):
        item = window("BUDGET", used / limit * 100,
                      object_value(data.get("billingCycle")).get("billingPeriodEnd"))
        if item:
            return [item]
    return []


def normalize_gemini(data):
    data = {'response': data} if isinstance(data, str) else object_value(data)
    result = []
    # 1. Structure from agy command usage data:
    buckets = []
    cmd_data = object_value(object_value(data.get("command")).get("data"))
    groups = list_value(cmd_data.get("groups") if cmd_data.get("groups") is not None else data.get("groups"))
    for group in groups:
        group = object_value(group)
        if "gemini" in group.get("name", "").lower():
            buckets.extend(list_value(group.get("buckets")))
    if not buckets and "buckets" in data:
        buckets = list_value(data["buckets"])

    for b in buckets:
        b = object_value(b)
        win = b.get("window")
        label = "5H" if win == "5h" else "WEEK" if win in ("weekly", "7d") else "WINDOW"
        duration = 300 if label == "5H" else 10080 if label == "WEEK" else None
        used = None
        if "remaining_fraction" in b:
            used = used_from_remaining(b["remaining_fraction"])
        elif "used_percent" in b:
            used = b["used_percent"]
        elif "utilization" in b:
            used = b["utilization"]
        item = window(label, used, b.get("reset_time") or b.get("resets_at"), duration)
        if item:
            result.append(item)
    if result:
        order = {"5H": 0, "WEEK": 1, "WINDOW": 2}
        result.sort(key=lambda w: order.get(w["label"], 9))
        return result

    # 2. Direct dictionary keys:
    for key, label, duration in (("five_hour", "5H", 300), ("seven_day", "WEEK", 10080), ("weekly", "WEEK", 10080)):
        val = object_value(data.get(key))
        used = val.get("utilization")
        if used is None:
            used = val.get("used_percent")
        if used is None and "remaining_fraction" in val:
            used = used_from_remaining(val["remaining_fraction"])
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


def used_from_remaining(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return round((1.0 - value) * 100.0, 1)

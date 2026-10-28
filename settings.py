"""Admin-editable settings, kept in `settings.json` next to the accounts file.

Each value starts from its environment variable (or built-in default) and can be changed from the
admin screen's Settings tab without a restart.  Lengths are seconds (the UI shows minutes); the
batch size is a count.
"""
import json
import os
import threading

import auth

SETTINGS_FILE = os.path.join(os.path.dirname(auth.USERS_FILE) or ".", "settings.json")

# key: (environment variable, default seconds, minimum, maximum, label)
FIELDS = {
    "user_max_seconds": ("CODEC399_USER_MAX_SECONDS", 360.0, 10.0, 24 * 3600.0,
                         "Longest video a user may upload"),
    "auto_fast_seconds": ("CODEC399_AUTO_FAST_SECONDS", 210.0, 0.0, 24 * 3600.0,
                          "User uploads longer than this use Fast encode"),
    "user_max_batch": ("CODEC399_USER_MAX_BATCH", 8.0, 1.0, 50.0,
                       "Videos a user may upload per batch"),
}

_lock = threading.Lock()


def _defaults():
    out = {}
    for key, (env, default, lo, hi, _label) in FIELDS.items():
        try:
            out[key] = min(hi, max(lo, float(os.environ.get(env, default))))
        except ValueError:
            out[key] = default
    return out


def load():
    values = _defaults()
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (FileNotFoundError, ValueError):
        return values
    for key in FIELDS:
        if isinstance(saved.get(key), (int, float)):
            values[key] = float(saved[key])
    return values


def get(key):
    return load()[key]


def update(changes):
    """Validate and save the given {key: seconds} values. Returns the full settings afterwards."""
    with _lock:
        values = load()
        for key, raw in changes.items():
            if key not in FIELDS:
                raise ValueError(f"Unknown setting {key}.")
            _env, _default, lo, hi, label = FIELDS[key]
            try:
                val = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{label}: enter a number.")
            if key == "user_max_batch":
                if val != int(val) or not (lo <= val <= hi):
                    raise ValueError(f"{label}: enter a whole number from {lo:g} to {hi:g}.")
                values[key] = float(int(val))
                continue
            if not (lo <= val <= hi):
                raise ValueError(f"{label}: must be between {lo / 60:g} and {hi / 60:g} minutes.")
            values[key] = val
        os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(values, fh, indent=2, sort_keys=True)
        os.replace(tmp, SETTINGS_FILE)
        return values

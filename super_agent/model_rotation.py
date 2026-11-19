#!/usr/bin/env python
"""Pick a model profile for the next session (user ruling 2026-10-31; ISS-32).

Two jobs, one file, one source of truth (the ``models.profiles_file`` config
value, or the checkout's ``config/model_profiles.json`` when none is set):

* default / ``--json`` / ``--peek`` / ``--log`` -- the worker rotation cursor.
  The candidate list is the ``rotation`` array in the config; the cursor lives
  in ``state/model_rotation.json`` (created on first use).
* ``--profile <role>`` -- the model/effort for a named role (``qa``, ``pm``,
  ``steward``, ...), read from the ``profiles`` map. There is no ``worker``
  key: workers always come from the rotation below (user ruling 2026-10-31), so
  ``--profile worker`` falls back to the built-in default.

Every read falls back to the built-in default below when the config is missing
or malformed, so a broken config changes no behaviour -- it only prints a WARN
on stderr. Cooldown (``state/model_cooldown.json``) is unchanged.

Usage:
    python -m super_agent.model_rotation                    # -> worker rotation slot
    python -m super_agent.model_rotation --peek             # same, without advancing
    python -m super_agent.model_rotation --json             # {"model": ..., "effort": ..., "index": N}
    python -m super_agent.model_rotation --log wk-<任务>-<日期>
    python -m super_agent.model_rotation --profile qa       # -> role profile args
    python -m super_agent.model_rotation --profile qa --json
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state", "model_rotation.json")
LEDGER = os.path.join(ROOT, "state", "model_rotation.log")
PROFILES = os.path.join(ROOT, "config", "model_profiles.json")

# 内置默认：config 缺失/坏掉时行为与本单之前逐字一致。
# 用户 2026-11-02 定：worker 只用 cdy dsp v4.1 flash（其它模型太耗积分）。
ROTATION = [
    {"model": "cb-deepseek-v4.1-flash", "effort": "high"},
]
PROVIDER = "codebuddy"
FALLBACK_PROFILE = {"model": "cb-deepseek-v4.1-flash", "effort": "high", "thinking": "high"}

# 冷却登记：{"model": "until(YYYY-MM-DDTHH:MM:SS)"}；冷却中的模型自动跳过。
# 来源＝429 报文里的「预计 ... 重置后恢复」，由 supervisor 或 PM 手工登记。
COOLDOWN = os.path.join(ROOT, "state", "model_cooldown.json")


def warn(msg: str) -> None:
    print(f"[WARN] model_profiles: {msg}", file=sys.stderr)


def _bad(profile: dict) -> bool:
    return not isinstance(profile, dict) or not all(
        isinstance(profile.get(k), str) and profile.get(k) for k in ("model", "effort"))


def load_profiles(path: str = PROFILES) -> dict:
    """Return the parsed config, or ``{}`` when it is missing/invalid.

    Validation (ISS-32): ``schema_version`` must be present, ``rotation`` must be
    a non-empty list of ``{model, effort}``, every profile must carry a string
    ``model`` and ``effort``. Anything else is a bad config: keep the built-in
    defaults and warn -- never raise, never change behaviour.
    """
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        warn(f"读不到 {path}（{exc}），回退内置默认")
        return {}
    except ValueError as exc:
        warn(f"{path} 不是合法 JSON（{exc}），回退内置默认")
        return {}
    if not isinstance(data, dict):
        warn(f"{path} 顶层不是对象，回退内置默认")
        return {}
    if "schema_version" not in data:
        warn(f"{path} 缺 schema_version，视为坏配置，回退内置默认")
        return {}
    rotation = data.get("rotation")
    if not isinstance(rotation, list) or not rotation:
        warn(f"{path} 的 rotation 不是非空列表，视为坏配置，回退内置默认")
        return {}
    for i, cand in enumerate(rotation):
        if _bad(cand):
            warn(f"{path} 的 rotation[{i}] 缺 model/effort，视为坏配置，回退内置默认")
            return {}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        warn(f"{path} 的 profiles 不是对象，视为坏配置，回退内置默认")
        return {}
    for name, prof in profiles.items():
        if _bad(prof):
            warn(f"{path} 的 profiles[{name}] 缺 model/effort，视为坏配置，回退内置默认")
            return {}
    return data


def rotation_of(cfg: dict) -> list:
    return cfg.get("rotation") or ROTATION


def profile_of(cfg: dict, role: str) -> tuple:
    """(profile dict, source) -- source is ``config`` or ``fallback``."""
    prof = (cfg.get("profiles") or {}).get(role)
    if _bad(prof):
        if role:
            warn(f"没有角色 `{role}` 的画像（或字段不全），回退内置默认")
        return dict(FALLBACK_PROFILE), "fallback"
    return dict(prof), "config"


def load_cooldown() -> dict:
    try:
        with open(COOLDOWN, encoding="utf-8") as fh:
            data = json.load(fh)
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except (OSError, ValueError):
        return {}


def cooling(model: str, cooldown: dict) -> bool:
    until = cooldown.get(model)
    if not until:
        return False
    try:
        return time.strptime(until, "%Y-%m-%dT%H:%M:%S") > time.localtime()
    except ValueError:
        return False


def load_index() -> int:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return int(json.load(fh).get("index", 0))
    except (OSError, ValueError, TypeError):
        return 0


def save_index(index: int) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"index": index, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh, ensure_ascii=False)
    os.replace(tmp, STATE)


def main(argv: list[str]) -> int:
    peek = "--peek" in argv
    as_json = "--json" in argv
    role = ""
    if "--profile" in argv:
        i = argv.index("--profile")
        role = argv[i + 1] if i + 1 < len(argv) else ""
        if not role:
            print("--profile 需要一个角色名，例：--profile qa", file=sys.stderr)
            return 2
    note = ""
    if "--log" in argv:
        i = argv.index("--log")
        if i + 1 < len(argv):
            note = argv[i + 1]

    cfg = load_profiles()
    rotation = rotation_of(cfg)

    if role:
        prof, source = profile_of(cfg, role)
        if note:
            os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
            with open(LEDGER, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{note}\t{prof['model']}\t{prof['effort']}\n")
        if as_json:
            print(json.dumps({"model": prof["model"], "effort": prof["effort"],
                              "profile": role, "source": source}, ensure_ascii=False))
        else:
            print(f"--provider {PROVIDER} --model {prof['model']} --effort {prof['effort']}")
        return 0

    index = load_index() % len(rotation)
    cooldown = load_cooldown()
    skipped = []
    for _ in range(len(rotation)):
        candidate = rotation[index]
        if not cooling(candidate["model"], cooldown):
            break
        skipped.append(candidate["model"])
        index = (index + 1) % len(rotation)
    pick = rotation[index]
    if not peek:
        save_index((index + 1) % len(rotation))
    if skipped:
        print(f"[i] 跳过冷却中模型：{', '.join(skipped)}", file=sys.stderr)
    if note:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{note}\t{pick['model']}\t{pick['effort']}\n")

    if as_json:
        print(json.dumps({**pick, "index": index}, ensure_ascii=False))
    else:
        print(f"--provider {PROVIDER} --model {pick['model']} --effort {pick['effort']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

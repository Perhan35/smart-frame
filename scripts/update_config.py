#!/usr/bin/env python3
"""
Merges new keys from config.example.yaml into config.yaml.
Existing values in config.yaml are never overwritten.
Keys that no longer exist in config.example.yaml are removed.
"""

import sys
from pathlib import Path
import yaml


def deep_merge(base: dict, updates: dict) -> tuple[dict, list[str]]:
    """
    Recursively add keys from `updates` that are missing in `base`.
    Returns the merged dict and a list of added key paths.
    """
    added = []
    for key, value in updates.items():
        if key not in base:
            base[key] = value
            added.append(key)
        elif isinstance(base[key], dict) and isinstance(value, dict):
            _, sub_added = deep_merge(base[key], value)
            added.extend(f"{key}.{k}" for k in sub_added)
    return base, added


def deep_prune(config: dict, reference: dict, prefix: str = "") -> tuple[dict, list[str]]:
    """
    Recursively remove keys from `config` that are absent in `reference`.
    Returns the pruned dict and a list of removed key paths.
    """
    removed = []
    for key in list(config.keys()):
        full_key = f"{prefix}.{key}" if prefix else key
        if key not in reference:
            del config[key]
            removed.append(full_key)
        elif isinstance(config[key], dict) and isinstance(reference[key], dict):
            _, sub_removed = deep_prune(config[key], reference[key], full_key)
            removed.extend(sub_removed)
    return config, removed


def main():
    root = Path(__file__).parent.parent
    example_path = root / "config.example.yaml"
    config_path = root / "config.yaml"

    if not example_path.exists():
        print("config.example.yaml not found, skipping config update.")
        return

    with open(example_path) as f:
        example = yaml.safe_load(f) or {}

    if not config_path.exists():
        print("config.yaml not found — copying from config.example.yaml.")
        config_path.write_text(example_path.read_text())
        return

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    merged, added_keys = deep_merge(config, example)
    pruned, removed_keys = deep_prune(merged, example)

    if not added_keys and not removed_keys:
        print("config.yaml is already up to date.")
        return

    with open(config_path, "w") as f:
        yaml.dump(pruned, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    if added_keys:
        print(f"config.yaml updated — added {len(added_keys)} new key(s):")
        for key in added_keys:
            print(f"  + {key}")
        print("Review config.yaml and fill in any placeholder values.")

    if removed_keys:
        print(f"config.yaml updated — removed {len(removed_keys)} obsolete key(s):")
        for key in removed_keys:
            print(f"  - {key}")


if __name__ == "__main__":
    sys.exit(main())

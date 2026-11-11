#!/usr/bin/env python3
"""
Merges new keys from config.example.yaml into config.yaml.
Existing values in config.yaml are never overwritten, and keys are never deleted:
keys that are not in config.example.yaml are only listed, so you can review them
(they may be optional settings you added on purpose).

New keys are inserted as text, copied with their comment from config.example.yaml,
so the comments already in config.yaml are kept.

One-time migration: MQTT topics that still have their old fixed default value
("smartframe/set_mode"...) get the new per-frame default ("smartframe/{device_id}/...").
Topics you changed yourself are never touched.
"""

import re
import sys
from pathlib import Path
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from smartframe import config_edit  # noqa: E402

# Old fixed defaults of the mqtt: topics (before one ID per frame)
LEGACY_TOPIC_DEFAULTS = {
    "topic": "smartframe/set_mode",
    "state_topic": "smartframe/mode_state",
    "status_topic": "smartframe/status",
    "available_modes_topic": "smartframe/modes_available",
    "dba_state_topic": "smartframe/audio/dba",
    "brightness_topic": "smartframe/set_brightness",
    "brightness_state_topic": "smartframe/brightness_state",
    "contrast_topic": "smartframe/set_contrast",
    "contrast_state_topic": "smartframe/contrast_state",
    "color_preset_topic": "smartframe/set_color_preset",
    "color_preset_state_topic": "smartframe/color_preset_state",
    "input_source_topic": "smartframe/set_input_source",
    "input_source_state_topic": "smartframe/input_source_state",
}

# Keys that older versions had in config.example.yaml and that nothing reads any more:
# left in config.yaml (harmless), but not listed as "unknown" after every git pull.
OBSOLETE_KEYS = frozenset({"audio.microphone"})

_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:")
_CHILD_KEY = re.compile(r"^  ([A-Za-z_][\w-]*)\s*:")


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


def find_unknown(config: dict, reference: dict, prefix: str = "") -> list[str]:
    """Key paths present in `config` but absent from `reference` (nothing is removed)."""
    unknown = []
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if key not in reference:
            if full_key not in OBSOLETE_KEYS:
                unknown.append(full_key)
        elif isinstance(value, dict) and isinstance(reference[key], dict):
            unknown.extend(find_unknown(value, reference[key], full_key))
    return unknown


def migrate_topics(config_text: str, config: dict, example: dict) -> tuple[str, list[str]]:
    """Move topics still at their old fixed default to the example's new default.
    Returns the new text and the list of migrated keys (text edits keep comments)."""
    mqtt = config.get("mqtt") or {}
    new_defaults = example.get("mqtt") or {}
    migrated = []
    for key, old in LEGACY_TOPIC_DEFAULTS.items():
        new = new_defaults.get(key)
        if mqtt.get(key) != old or not new or new == old:
            continue
        new_text = config_edit.set_value(config_text, "mqtt", key, new)
        if new_text is None:
            continue  # Unusual layout: leave it (the old topic keeps working)
        config_text = new_text
        mqtt[key] = new
        migrated.append(f"mqtt.{key}: {old} -> {new}")
    return config_text, migrated


def _sections(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Top-level key -> (first line, end line) in a 2-level YAML file."""
    starts = [(m.group(1), i) for i, line in enumerate(lines) if (m := _TOP_KEY.match(line))]
    sections = {}
    for n, (key, start) in enumerate(starts):
        end = starts[n + 1][1] if n + 1 < len(starts) else len(lines)
        sections[key] = (start, end)
    return sections


def insert_keys_as_text(config_text: str, example_text: str, added: list[str]) -> str | None:
    """Copy the example lines of each added key into config_text.

    Returns the new text, or None if the layout is not the simple 2-level one this
    handles (the caller then falls back to rewriting the file).
    """
    lines = config_text.splitlines()
    ex_lines = example_text.splitlines()
    ex_sections = _sections(ex_lines)

    for path in added:
        parts = path.split(".")
        if len(parts) == 1:
            if parts[0] not in ex_sections:
                return None
            start, end = ex_sections[parts[0]]
            block = ex_lines[start:end]
            while block and not block[-1].strip():
                block.pop()
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(block)
        elif len(parts) == 2:
            section, child = parts
            if section not in ex_sections:
                return None
            ex_start, ex_end = ex_sections[section]
            child_line = next(
                (ln for ln in ex_lines[ex_start + 1:ex_end]
                 if (m := _CHILD_KEY.match(ln)) and m.group(1) == child),
                None,
            )
            sections = _sections(lines)
            if child_line is None or section not in sections:
                return None
            start, end = sections[section]
            # Insert after the section's last indented line
            insert_at = start + 1
            for i in range(start + 1, end):
                if lines[i].startswith((" ", "\t")):
                    insert_at = i + 1
            lines.insert(insert_at, child_line)
        else:
            return None
    return "\n".join(lines) + "\n"


def main():
    root = Path(__file__).parent.parent
    example_path = root / "config.example.yaml"
    config_path = root / "config.yaml"

    if not example_path.exists():
        print("config.example.yaml not found, skipping config update.")
        return

    example_text = example_path.read_text()
    example = yaml.safe_load(example_text) or {}

    if not config_path.exists():
        print("config.yaml not found — copying from config.example.yaml.")
        config_path.write_text(example_text)
        config_path.chmod(0o600)  # Holds the MQTT password
        return

    config_text = config_path.read_text()
    config = yaml.safe_load(config_text) or {}

    config_text, migrated = migrate_topics(config_text, config, example)
    unknown_keys = find_unknown(config, example)
    merged, added_keys = deep_merge(config, example)

    if migrated and not added_keys:
        config_path.write_text(config_text)
    if migrated:
        print(f"config.yaml: {len(migrated)} MQTT topic(s) moved to the per-frame default "
              "({device_id} = this Pi's hostname, or mqtt.device_id):")
        for line in migrated:
            print(f"  ~ {line}")
        print("  Anything that publishes to the old topics directly (scripts, automations) "
              "must use the new ones.")

    if added_keys:
        new_text = insert_keys_as_text(config_text, example_text, added_keys)
        if new_text is None or yaml.safe_load(new_text) != merged:
            # Unusual layout: rewrite the whole file (comments are lost), keep a backup.
            backup = config_path.with_name("config.yaml.bak")
            backup.write_text(config_text)
            backup.chmod(0o600)
            new_text = yaml.dump(merged, default_flow_style=False, allow_unicode=True,
                                 sort_keys=False)
            print(f"Note: config.yaml was rewritten (comments lost). Backup: {backup.name}")
        config_path.write_text(new_text)

        print(f"config.yaml updated — added {len(added_keys)} new key(s):")
        for key in added_keys:
            print(f"  + {key}")
        print("Review config.yaml and fill in any placeholder values.")
    elif not migrated:
        print("config.yaml is already up to date.")

    if unknown_keys:
        print(f"config.yaml has {len(unknown_keys)} key(s) not in config.example.yaml "
              "(kept; delete them by hand if they are obsolete):")
        for key in unknown_keys:
            print(f"  ? {key}")


if __name__ == "__main__":
    sys.exit(main())

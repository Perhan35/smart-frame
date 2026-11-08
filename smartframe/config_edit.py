"""Edit one value in config.yaml as text, so its comments and layout are kept.

config.yaml is a simple 2-level file (`section:` then `  key: value  # comment`).
yaml.dump would rewrite it and drop every comment, so the scripts change only the
one line they need. Only depends on the standard library (and PyYAML for checks).
"""

import json
import re

import yaml

_TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*)\s*:")


def yaml_scalar(value):
    """Python value -> YAML text for one line: None -> null, str -> "quoted"."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)  # JSON strings are valid YAML


def set_value(text, section, key, value):
    """Return `text` with `section.key` set to `value`, or None if the key line is
    not found (or the result would not read back as `value`)."""
    lines = text.splitlines(keepends=True)
    in_section = False
    line_re = re.compile(
        r"^(?P<head>[ \t]+" + re.escape(key) + r"[ \t]*:[ \t]*)"
        r"(?P<value>(?:\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^#\r\n])*?)"
        r"(?P<tail>[ \t]*#[^\r\n]*)?(?P<eol>\r?\n?)$"
    )
    for i, line in enumerate(lines):
        if (m := _TOP_KEY.match(line)):
            in_section = m.group(1) == section
            continue
        if not in_section:
            continue
        m = line_re.match(line)
        if m:
            tail = m.group("tail") or ""
            if tail and not tail.startswith((" ", "\t")):
                tail = " " + tail
            lines[i] = f"{m.group('head')}{yaml_scalar(value)}{tail}{m.group('eol')}"
            new_text = "".join(lines)
            loaded = yaml.safe_load(new_text) or {}
            if (loaded.get(section) or {}).get(key) != value:
                return None
            return new_text
    return None

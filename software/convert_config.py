#!/usr/bin/env python3
"""Convert legacy JSON config files to YAML v2.0 format.

Usage:
    python convert_config.py <json_config_path> [yaml_output_path]

If yaml_output_path is not specified, writes to the same directory
with a .yaml extension replacing .json.
"""

import json
import os
import sys

import yaml

from fluidics.control.config import convert_legacy_config
from fluidics.files import atomic_write


def convert_json_to_yaml(json_path, yaml_path=None):
    """Convert a legacy JSON config file to YAML v2.0 format.

    Returns the path to the generated YAML file.
    """
    with open(json_path, 'r') as f:
        old = json.load(f)

    if yaml_path is None:
        yaml_path = os.path.splitext(json_path)[0] + '.yaml'

    new = convert_legacy_config(old)

    with atomic_write(yaml_path) as f:
        yaml.dump(new, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return yaml_path


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python convert_config.py <json_config_path> [yaml_output_path]")
        sys.exit(1)

    json_path = sys.argv[1]
    yaml_path = sys.argv[2] if len(sys.argv) > 2 else None

    output = convert_json_to_yaml(json_path, yaml_path)
    print(f"Converted: {json_path} -> {output}")

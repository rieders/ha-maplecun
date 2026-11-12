"""wmbus.py ohne Home Assistant laden (das Paket __init__ importiert HA)."""

import importlib.util
import sys
from pathlib import Path

COMPONENT = Path(__file__).parents[1] / "custom_components" / "maplecun"
REFERENCE = Path(__file__).parents[1] / "reference" / "WMBus.pm"

if str(COMPONENT) not in sys.path:
    sys.path.insert(0, str(COMPONENT))      # wmbus.py lädt wmbus_tables.py als Nachbarmodul


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"maplecun_{name}", COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # für dataclasses nötig
    spec.loader.exec_module(module)
    return module


wmbus = load("wmbus")
tables = load("wmbus_tables")

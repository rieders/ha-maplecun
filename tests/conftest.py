import importlib.util

import pytest

# Die HA-Tests brauchen pytest-homeassistant-custom-component; der Decoder-Test (test_wmbus.py) nicht.
HAS_HA = importlib.util.find_spec("pytest_homeassistant_custom_component") is not None

if HAS_HA:
    pytest_plugins = "pytest_homeassistant_custom_component"

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        yield

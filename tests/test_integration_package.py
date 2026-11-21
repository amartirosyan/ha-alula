"""Packaging checks for the HACS integration. These do not import Home Assistant."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pyalula

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "alula"
VENDORED = ("__init__.py", "client.py", "const.py", "models.py")


def _load_identifiers():
    path = COMPONENT / "identifiers.py"
    spec = importlib.util.spec_from_file_location("alula_identifiers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vendored_client_matches_library() -> None:
    for name in VENDORED:
        library = (ROOT / "pyalula" / name).read_bytes()
        vendored = (COMPONENT / "pyalula" / name).read_bytes()
        assert library == vendored, name


def test_manifest_drops_pypi_client() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["domain"] == "alula"
    assert manifest["version"] == "0.2.3"
    assert manifest["version"] == pyalula.__version__
    assert manifest["config_flow"] is True
    assert manifest["requirements"] == []
    assert manifest["documentation"] == "https://github.com/amartirosyan/ha-alula"
    assert (COMPONENT / "config_flow.py").is_file()
    blob = json.dumps(manifest)
    assert "pyalula" not in blob


def test_hacs_metadata() -> None:
    meta = json.loads((ROOT / "hacs.json").read_text())
    assert meta["name"] == "Alula"
    assert meta["render_readme"] is True
    assert meta["homeassistant"] == "2024.11.0"


def test_service_descriptions_and_translations() -> None:
    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    assert strings == english
    assert set(strings["services"]) == {"force_arm", "bypass_zone"}
    force = strings["services"]["force_arm"]
    assert set(force["fields"]) == {
        "entity_id",
        "mode",
        "method",
        "silent",
        "no_entry_delay",
        "variant",
    }
    assert set(force["fields"]["variant"]["selector"]["select"]["options"]) == {
        "forcearm",
        "arminglevel",
        "partition",
        "forcearm_then_arminglevel",
        "arminglevel_then_forcearm",
        "arminglevel_then_rearm",
        "arminglevel_then_zonebypass",
    }
    assert strings["options"]["step"]["init"]["data"]["night_arm_variant"]
    assert set(force["fields"]["mode"]["selector"]["select"]["options"]) == {
        "home",
        "away",
        "night",
    }
    bypass = strings["services"]["bypass_zone"]
    assert set(bypass["fields"]) == {"entity_id", "zone", "bypass"}
    services_yaml = (COMPONENT / "services.yaml").read_text()
    assert "force_arm:" in services_yaml
    assert "bypass_zone:" in services_yaml
    assert "\npin:" not in services_yaml
    assert "password:" not in services_yaml


def test_unique_ids_stay_the_same() -> None:
    identifiers = _load_identifiers()
    assert identifiers.panel_unique_id("abc") == "alula_abc_panel"
    assert identifiers.zone_unique_id("4") == "alula_zone_4"
    assert identifiers.zone_unique_id("0") == "alula_zone_0"
    assert identifiers.zone_index_from_unique_id("alula_zone_4") == 4
    assert identifiers.zone_index_from_unique_id("alula_zone_0") == 0
    assert identifiers.zone_index_from_unique_id("alula_abc_panel") is None
    assert identifiers.zone_index_from_unique_id("alula_zone_") is None
    assert identifiers.zone_index_from_unique_id(None) is None

    panel_source = (COMPONENT / "alarm_control_panel.py").read_text()
    zone_source = (COMPONENT / "binary_sensor.py").read_text()
    assert "panel_unique_id(panel.id)" in panel_source
    assert "arm_with_variant" in panel_source
    assert 'force_arm("night")' not in panel_source
    assert "silent=True" in panel_source
    assert "no_entry_delay=True" in panel_source
    assert "zone_unique_id(zone.id)" in zone_source
    assert '"bypassed": zone.is_bypassed' in zone_source

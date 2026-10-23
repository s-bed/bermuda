"""Options forms must serialize for the frontend as well as validate submissions."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.data_entry_flow import FlowManagerIndexView
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bermuda.config_flow import BermudaOptionsFlowHandler
from custom_components.bermuda.const import (
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_MAX_RADIUS,
    CONF_REF_POWER,
    CONF_SAVE_AND_CLOSE,
    CONF_SCANNERS,
    DEFAULT_ATTENUATION,
    DEFAULT_REF_POWER,
    DOMAIN,
)

from .const import MOCK_CONFIG, MOCK_OPTIONS_GLOBALS


@pytest.fixture
def options_flow(hass):
    """Use real forms with an empty scanner list; no Bluetooth hardware is needed."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, options=MOCK_OPTIONS_GLOBALS)
    entry.add_to_hass(hass)
    flow = BermudaOptionsFlowHandler(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.coordinator = SimpleNamespace(devices={}, scanner_list=[])
    return flow


def _serialize_form(hass, result):
    """Exercise the same result conversion used by the config-flow HTTP endpoints."""
    rendered = FlowManagerIndexView(hass.config_entries.options)._prepare_result_json(result)
    # Also catch invalid defaults such as NaN or infinity in a redisplayed form.
    json.dumps(rendered, allow_nan=False)
    return {field["name"]: field for field in rendered["data_schema"]}


@pytest.mark.parametrize("step", ["globalopts", "calibration1_global"])
async def test_options_forms_serialize(hass, options_flow, step):
    """Custom numeric validators must never leak into the frontend schema."""
    result = await getattr(options_flow, f"async_step_{step}")()
    assert result["type"] == FlowResultType.FORM
    fields = _serialize_form(hass, result)
    for key in (CONF_ATTENUATION, CONF_REF_POWER):
        assert fields[key]["type"] == "float"
        assert fields[key]["default"] == MOCK_OPTIONS_GLOBALS[key]


@pytest.mark.parametrize("step", ["globalopts", "calibration1_global"])
@pytest.mark.parametrize("bad", [0, -2, math.nan, math.inf, "3", True])
async def test_forms_keep_safe_stored_defaults(hass, options_flow, step, bad):
    """Opening the form must still show the values used by the distance maths."""
    options_flow.options[CONF_ATTENUATION] = bad
    options_flow.options[CONF_REF_POWER] = math.nan
    result = await getattr(options_flow, f"async_step_{step}")()
    fields = _serialize_form(hass, result)
    assert fields[CONF_ATTENUATION]["default"] == DEFAULT_ATTENUATION
    assert fields[CONF_REF_POWER]["default"] == DEFAULT_REF_POWER


def _submission(step, save):
    if step == "globalopts":
        return dict(MOCK_OPTIONS_GLOBALS)
    return {
        CONF_DEVICES: "reference_device",
        CONF_SCANNERS: "aa:bb:cc:dd:ee:ff",
        CONF_ATTENUATION: MOCK_OPTIONS_GLOBALS[CONF_ATTENUATION],
        CONF_REF_POWER: MOCK_OPTIONS_GLOBALS[CONF_REF_POWER],
        CONF_SAVE_AND_CLOSE: save,
    }


@pytest.mark.parametrize(
    ("step", "save"), [("globalopts", None), ("calibration1_global", False), ("calibration1_global", True)]
)
@pytest.mark.parametrize(
    ("field", "bad"),
    [
        (field, bad)
        for field in (CONF_ATTENUATION, CONF_REF_POWER)
        for bad in (math.nan, math.inf, -math.inf, "nan", "inf", "-inf")
    ]
    + [(CONF_ATTENUATION, 0), (CONF_ATTENUATION, -2)],
)
async def test_invalid_numbers_are_rejected_before_save_or_preview(hass, options_flow, step, save, field, bad):
    """Reject invalid submissions on every path and return a serializable error form."""
    submitted = _submission(step, save)
    submitted[field] = bad
    original_options = dict(options_flow.options)
    with (
        patch.object(options_flow, "_update_options", new_callable=AsyncMock) as update,
        patch.object(options_flow, "_get_bermuda_device_from_registry") as lookup,
        patch("custom_components.bermuda.config_flow.rssi_to_metres") as calculate,
    ):
        result = await getattr(options_flow, f"async_step_{step}")(submitted)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {field: f"invalid_{field}"}
    update.assert_not_called()
    lookup.assert_not_called()
    calculate.assert_not_called()
    assert options_flow.options == original_options
    assert options_flow._last_attenuation is None
    assert options_flow._last_ref_power is None
    fields = _serialize_form(hass, result)
    if step == "calibration1_global":
        assert fields[CONF_DEVICES]["default"] == submitted[CONF_DEVICES]
        assert fields[CONF_SCANNERS]["default"] == submitted[CONF_SCANNERS]
        assert fields[CONF_SAVE_AND_CLOSE]["default"] == save


@pytest.mark.parametrize(
    ("step", "save"), [("globalopts", None), ("calibration1_global", False), ("calibration1_global", True)]
)
@pytest.mark.parametrize(("attenuation", "ref_power"), [("0.05", "-130"), ("50", "20")])
async def test_valid_numbers_can_be_submitted_after_an_error(hass, options_flow, step, save, attenuation, ref_power):
    """Keep accepting unusual finite numbers, coerce them, and clear errors on retry."""
    submitted = _submission(step, save)
    submitted[CONF_ATTENUATION] = math.nan
    submitted[CONF_REF_POWER] = math.inf
    method = getattr(options_flow, f"async_step_{step}")
    result = await method(submitted)
    assert result["errors"] == {CONF_ATTENUATION: "invalid_attenuation", CONF_REF_POWER: "invalid_ref_power"}
    _serialize_form(hass, result)

    submitted[CONF_ATTENUATION] = attenuation
    submitted[CONF_REF_POWER] = ref_power
    device = SimpleNamespace(name="Reference device", get_scanner=lambda address: SimpleNamespace(hist_rssi=[-65]))
    with patch.object(options_flow, "_get_bermuda_device_from_registry", return_value=device):
        result = await method(submitted)
    if step == "calibration1_global" and not save:
        assert result["type"] == FlowResultType.FORM
        assert not result["errors"]
        fields = _serialize_form(hass, result)
        assert fields[CONF_ATTENUATION]["default"] == float(attenuation)
        assert fields[CONF_REF_POWER]["default"] == float(ref_power)
        assert "RSSI Actual" in result["description_placeholders"]["suffix"]
        assert options_flow.options == MOCK_OPTIONS_GLOBALS
    else:
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_ATTENUATION] == float(attenuation)
        assert result["data"][CONF_REF_POWER] == float(ref_power)


async def test_global_error_preserves_other_edits(hass, options_flow):
    submitted = dict(MOCK_OPTIONS_GLOBALS, **{CONF_MAX_RADIUS: 99.0, CONF_ATTENUATION: 0})
    result = await options_flow.async_step_globalopts(submitted)
    fields = _serialize_form(hass, result)
    assert fields[CONF_MAX_RADIUS]["default"] == 99.0
    assert fields[CONF_ATTENUATION]["default"] == 0
    assert options_flow.options == MOCK_OPTIONS_GLOBALS

"""
Тесты для services/api/schemas.py — валидаторы Pydantic.
"""

import pytest
from pydantic import ValidationError

from api.schemas import CameraCreate, ParkSettingsUpdate


def test_camera_create_strips_and_requires_nonempty_url():
    cam = CameraCreate(name="A", url="  rtsp://x  ", direction="in")
    assert cam.url == "rtsp://x"


def test_camera_create_rejects_whitespace_only_url():
    with pytest.raises(ValidationError):
        CameraCreate(name="A", url="   ", direction="in")


def test_camera_create_rejects_invalid_direction():
    with pytest.raises(ValidationError):
        CameraCreate(name="A", url="rtsp://x", direction="left")


def test_camera_create_gate_is_optional():
    cam = CameraCreate(name="A", url="rtsp://x", direction="out")
    assert cam.gate is None
    assert cam.active is True


def test_park_settings_update_rejects_zero_capacity():
    with pytest.raises(ValidationError):
        ParkSettingsUpdate(capacity=0)


def test_park_settings_update_rejects_negative_threshold():
    with pytest.raises(ValidationError):
        ParkSettingsUpdate(warning_threshold=-1)


def test_park_settings_update_allows_all_fields_unset():
    s = ParkSettingsUpdate()
    assert s.capacity is None
    assert s.warning_threshold is None
    assert s.timezone is None

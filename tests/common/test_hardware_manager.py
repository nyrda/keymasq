# ruff: noqa: F403, F405, I001
from tests.common.support import *

class TestHardwareManager:
    def test_save_and_load_hardware(self, temp_config_dir, sample_hardware_config):
        manager = HardwareManager()
        manager.save_hardware(sample_hardware_config)

        loaded = manager.get_hardware(sample_hardware_config.hardware_id)

        assert loaded is not None
        assert loaded.name == sample_hardware_config.name
        assert loaded.vendor_id == sample_hardware_config.vendor_id
        assert loaded.product_id == sample_hardware_config.product_id
        assert len(loaded.buttons) == len(sample_hardware_config.buttons)

    def test_list_hardware(self, temp_config_dir, sample_hardware_config):
        manager = HardwareManager()
        manager.save_hardware(sample_hardware_config)

        hardware_list = manager.list_hardware()

        assert len(hardware_list) == 1
        assert hardware_list[0].hardware_id == sample_hardware_config.hardware_id

    def test_get_nonexistent_hardware(self, temp_config_dir):
        manager = HardwareManager()

        result = manager.get_hardware("ffff:ffff")

        assert result is None

    def test_hardware_id_format(self, sample_hardware_config):
        assert sample_hardware_config.hardware_id == "1234:5678"



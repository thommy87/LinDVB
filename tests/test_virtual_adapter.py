"""is_virtual_adapter() recognises vidtv in the sysfs layouts of old and new kernels."""

import os

from lindvb.scan import is_virtual_adapter


def fake_sysfs(root, device, driver=None):
    dev = root / device
    dev.mkdir(parents=True)
    if driver:
        drv = root / driver
        drv.mkdir(parents=True)
        os.symlink(drv, dev / "driver")
    cls = root / "class/dvb/dvb0.frontend0"
    cls.mkdir(parents=True)
    os.symlink(dev, cls / "device")
    return root


def test_platform_device_older_kernels(tmp_path):
    root = fake_sysfs(tmp_path, "devices/platform/vidtv", "bus/platform/drivers/vidtv")
    assert is_virtual_adapter(0, 0, sysfs=root)


def test_i2c_adapter_below_the_platform_device_newer_kernels(tmp_path):
    # Linux 7.0: /sys/class/dvb/dvb0.frontend0/device -> .../platform/vidtv.0/i2c-0
    root = fake_sysfs(tmp_path, "devices/platform/vidtv.0/i2c-0")
    assert is_virtual_adapter(0, 0, sysfs=root)


def test_real_tuner(tmp_path):
    root = fake_sysfs(tmp_path, "devices/pci0000:00/0000:00:14.0/usb1/1-2",
                      "bus/usb/drivers/dvb_usb_dib0700")
    assert not is_virtual_adapter(0, 0, sysfs=root)


def test_no_adapter(tmp_path):
    assert not is_virtual_adapter(3, 0, sysfs=tmp_path)

"""Host tests for the parts of roadrunner_admin.py that no board can check.

The USB-path normalisation is the load-bearing piece: it is what correlates a
board across a BOOTSEL reboot now that the mountpoint is no longer a constant.
Get it wrong in one direction and the tool never finds the board it just
rebooted; wrong in the other and it flashes a different board at a different
port. Neither failure is visible from reading the code, and both are expensive
on hardware, so they are pinned here.
"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "roadrunner_admin", Path(__file__).with_name("roadrunner_admin.py")
)
assert _spec and _spec.loader
admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin)


CDC = "pci-0000:00:14.0-usb-0:1.2:1.0"
MSC = "pci-0000:00:14.0-usb-0:1.2:0.0-scsi-0:0:0:0"
PORT = "pci-0000:00:14.0-usb-0:1.2"


@pytest.mark.parametrize(
    "by_path, expected",
    [
        # The two faces of one board, either side of a BOOTSEL reboot.
        (CDC, PORT),
        (MSC, PORT),
        (MSC + "-part1", PORT),
        # A root-hub port with no hub in between: one component, not two.
        ("pci-0000:00:14.0-usb-0:5:1.0", "pci-0000:00:14.0-usb-0:5"),
        # Chained hubs - the port keeps growing, and its dots must survive.
        ("pci-0000:00:14.0-usb-0:1.2.3:1.0", "pci-0000:00:14.0-usb-0:1.2.3"),
        # Non-PCI controllers, and the -port0 suffix some kernels append. The
        # platform name here contains its own ".usb", which is why the search
        # for "-usb-" has to take the last one.
        ("platform-3f980000.usb-usb-0:1.2:1.0-port0", "platform-3f980000.usb-usb-0:1.2"),
        # Not a USB path at all.
        ("pci-0000:00:1f.2-ata-1", None),
        ("", None),
    ],
)
def test_usb_port_prefix(by_path, expected):
    assert admin.usb_port_prefix(by_path) == expected


def test_serial_and_block_paths_of_one_board_agree():
    """The whole mechanism rests on this: same board, same answer."""
    assert admin.usb_port_prefix(CDC) == admin.usb_port_prefix(MSC)


def test_neighbouring_ports_stay_distinct():
    """And on this: a board in the next port must not look like ours.

    Truncating one component too many would make every board on a hub
    identical, which fails silently and flashes the wrong board.
    """
    neighbour = "pci-0000:00:14.0-usb-0:1.3:0.0-scsi-0:0:0:0"
    assert admin.usb_port_prefix(CDC) != admin.usb_port_prefix(neighbour)


def test_pci_address_is_not_mistaken_for_an_interface():
    """"0000:00:14.0" ends in something shaped exactly like an interface."""
    assert admin.usb_port_prefix(CDC).startswith("pci-0000:00:14.0-usb-")


def test_wait_for_serial_port_ignores_the_outgoing_enumeration(monkeypatch):
    """A port still listed from before the reboot must not be returned.

    Right after a UF2 lands, the board resets but its serial device can linger
    for a moment. It is stable enough to pass the settle check, so without
    `expect_disconnect` the wait returns a handle that is about to vanish and
    the caller fails on open instead.
    """
    listings = [
        [("/dev/ttyACM0", "RR-OLD")],  # the outgoing enumeration
        [],  # gone
        [("/dev/ttyACM1", "RR-NEW")],  # back at the same port
    ]
    seen = []

    def fake_ports():
        seen.append(len(listings))
        return listings[0] if len(listings) == 1 else listings.pop(0)

    monkeypatch.setattr(admin, "roadrunner_ports", fake_ports)
    monkeypatch.setattr(admin, "port_topology", lambda device: PORT)

    port = admin.wait_for_serial_port(
        prefix=PORT, timeout=5, settle=0, expect_disconnect=True
    )
    assert port == "/dev/ttyACM1"
    assert seen, "the wait never polled"


def test_mount_points_unescape_octal(tmp_path, monkeypatch):
    """/proc/mounts escapes the characters that would break its own parsing."""
    proc = tmp_path / "mounts"
    proc.write_text(
        "proc /proc proc rw 0 0\n"
        "/dev/sdb1 /media/vi/my\\040volume vfat rw 0 0\n"
    )

    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        # Compared as parts, not as a string: Path("/proc/mounts") stringifies
        # with backslashes on Windows, where these tests also run.
        if self.parts[-2:] == ("proc", "mounts"):
            return real_read_text(proc)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(admin.os.path, "realpath", lambda p: p)

    mounts = admin._mount_points()
    assert mounts["/dev/sdb1"] == Path("/media/vi/my volume")
    assert "proc" not in mounts  # not a /dev/ source


def test_descend_reaches_the_by_path_layout(tmp_path):
    """mcu-updater mounts at <root>/BOOTSEL/by-path/<tag> - two levels down."""
    target = tmp_path / "BOOTSEL" / "by-path" / "pci-0000_00_14.0-usb-0_1.2"
    target.mkdir(parents=True)
    assert target in admin._descend(tmp_path)


def test_descend_stops_at_three_levels(tmp_path):
    """Bounded on purpose - a mounted volume is not something to walk."""
    too_deep = tmp_path / "a" / "b" / "c" / "d"
    too_deep.mkdir(parents=True)
    found = admin._descend(tmp_path)
    assert tmp_path / "a" / "b" / "c" in found
    assert too_deep not in found

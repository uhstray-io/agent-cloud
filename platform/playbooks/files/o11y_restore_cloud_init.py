"""Closed validators for the narrow PVE cloud-init auxiliary volume shape."""

from __future__ import annotations

import re

CLOUDINIT_AUX = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]{0,63}):vm-([1-9][0-9]{0,8})-cloudinit\Z")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


def source_auxiliary(content: object, source_vmid: int) -> bool:
    """Recognize one exact block-backed source cloud-init drive or an empty ide2."""
    if content == "none,media=cdrom":
        return True
    if not isinstance(content, str):
        return False
    fields = content.split(",")
    if len(fields) != 2 or fields[1] != "media=cdrom":
        return False
    match = CLOUDINIT_AUX.fullmatch(fields[0])
    return match is not None and match.group(2) == str(source_vmid)


def target_auxiliary(content: object, target_vmid: int, storage_id: str, expected_present: bool) -> bool:
    """Require the regenerated target's exact target-store and target-VMID volume."""
    if not expected_present:
        return content is None or content == "none,media=cdrom"
    if not isinstance(content, str):
        return False
    fields = content.split(",")
    if len(fields) != 2 or fields[1] != "media=cdrom":
        return False
    match = CLOUDINIT_AUX.fullmatch(fields[0])
    return (
        match is not None
        and match.group(1) == storage_id
        and match.group(2) == str(target_vmid)
    )


def valid_smbios_uuid(value: object) -> bool:
    return isinstance(value, str) and value.startswith("uuid=") and UUID.fullmatch(value[5:]) is not None

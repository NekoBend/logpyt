"""The uid column of ``-v uid`` (design 5.8).

logcat prints the uid as a name when the name has at most 5 characters, else as
a number (blank when the uid is unknown):

- Android 7.0 looks the uid up in the static ``android_ids`` table
  (nougat ``liblog/logprint.c``), so only short platform names appear, such as
  ``root``, ``radio``, ``media`` or ``logd``; app uids print as numbers.
- Android 8.0 and later name it through ``getpwuid`` (oreo ``logprint.c``),
  which also knows app and isolated uids (bionic ``print_app_name_from_uid``):
  ``u<user>_a<n>`` is ``user * 100000 + 10000 + n`` and ``u<user>_i<n>`` is
  ``user * 100000 + AID_ISOLATED_START + n``. Only ``u0_a0``..``u9_a9`` and
  ``u0_i0``..``u9_i9`` fit in 5 characters.

``AID_ISOLATED_START`` is 99000 up to Android 9 and 90000 from Android 10
(``android_filesystem_config.h`` on oreo-release, pie-release and
android10-release). The line does not say which release printed it, so an
isolated name is read with 90000, the value of every release since 2019; on
Android 8 and 9 such a uid comes out 9000 too low.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["AID_NAMES", "parse_uid"]

# The platform AIDs whose name has at most 5 characters: the only names logcat
# can print. Values from android_filesystem_config.h, nougat-release and main
# (AOSP, read 2026-11-04); a name means the same uid in every release.
AID_NAMES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "root": 0,
        "bin": 2,
        "sys": 3,
        "radio": 1001,
        "input": 1004,
        "audio": 1005,
        "log": 1007,
        "mount": 1009,
        "wifi": 1010,
        "adb": 1011,
        "media": 1013,
        "dhcp": 1014,
        "vpn": 1016,
        "usb": 1018,
        "drm": 1019,
        "mdnsr": 1020,
        "gps": 1021,
        "mtp": 1024,
        "nfc": 1027,
        "clat": 1029,
        "logd": 1036,
        "dbus": 1038,
        "nvram": 1050,
        "dns": 1051,
        "ese": 1060,
        "hsm": 1064,
        "lmkd": 1069,
        "llkd": 1070,
        "gsid": 1074,
        "artd": 1082,
        "uwb": 1083,
        "diced": 1085,
        "mmd": 1095,
        "shell": 2000,
        "cache": 2001,
        "diag": 2002,
        "inet": 3003,
        "uhid": 3011,
        "misc": 9998,
    }
)

_PER_USER = 100_000  # AID_USER_OFFSET
_APP_START = 10_000  # AID_APP_START
_ISOLATED_START = 90_000  # AID_ISOLATED_START since Android 10 (module docstring)
_MAX_UID = 0xFFFF_FFFF
# Longest decimal uid (2**32 - 1) and the longest user or app id a name can
# carry; anything longer is not a uid logcat printed.
_MAX_UID_DIGITS = 10
_MAX_NAME_DIGITS = 5


def _small_int(text: str, max_digits: int) -> int | None:
    """``text`` as an int when it is 1..max_digits ASCII digits, else None."""
    if not text or len(text) > max_digits or not text.isascii() or not text.isdigit():
        return None
    return int(text)


def parse_uid(token: str) -> int | None:
    """Return the uid that a ``-v uid`` column token stands for.

    Args:
        token: The column as printed, without padding.

    Returns:
        The uid, or None when the token is not a number, an app or isolated
        name, or a name of ``AID_NAMES`` (for example an OEM name). Design 5.8
        treats None as a system uid for attribution: only system names fit in
        5 characters.

    """
    number = _small_int(token, _MAX_UID_DIGITS)
    if number is not None:
        return number if number <= _MAX_UID else None
    known = AID_NAMES.get(token)
    if known is not None:
        return known
    # u<user>_a<n> and u<user>_i<n> (bionic print_app_name_from_uid).
    if not token.startswith("u"):
        return None
    user_text, sep, rest = token[1:].partition("_")
    if not sep or len(rest) < 2 or rest[0] not in "ai":
        return None
    user = _small_int(user_text, _MAX_NAME_DIGITS)
    offset = _small_int(rest[1:], _MAX_NAME_DIGITS)
    if user is None or offset is None:
        return None
    base = _APP_START if rest[0] == "a" else _ISOLATED_START
    uid = user * _PER_USER + base + offset
    return uid if uid <= _MAX_UID else None

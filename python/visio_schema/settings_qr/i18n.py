"""Translations for the settings-QR tool.

The people who run this are a client's admins, provisioning rigs in the room
where the rigs are — not necessarily English speakers, and the strings they
read here decide whether a fleet ends up encrypted. Mirrors the launcher page's
scheme (`app.js` `tr()`): one flat table per language, English as the fallback,
and a missing key renders as the key rather than blowing up mid-prompt.

Language comes from ``--lang``, else ``$VISIO_LANG``, else the POSIX locale
(``$LC_ALL``/``$LC_MESSAGES``/``$LANG``). Detection is prefix-based, so
``zh_CN.UTF-8``, ``zh-Hans`` and ``zh`` all pick Chinese.
"""
from __future__ import annotations

import os

__all__ = ["LANGUAGES", "detect_language", "set_language", "tr"]

LANGUAGES = ("en", "zh")

_lang = "en"

# key -> {lang: text}. Grouped by where it is read, not alphabetically: a
# translator wants to see a whole prompt sequence together.
_STRINGS: dict[str, dict[str, str]] = {
    # -- interactive: framing ------------------------------------------- #
    "skipHint": {
        "en": "Empty answer skips a field. Ctrl-C aborts.",
        "zh": "留空表示跳过该项。Ctrl-C 取消。",
    },
    # -- interactive: sections ------------------------------------------ #
    "askMeta": {
        "en": "Configure capture metadata (task/location/...)?",
        "zh": "配置采集信息（任务／地点等）？",
    },
    "askStorage": {
        "en": "Configure cloud upload (OSS/S3)?",
        "zh": "配置云端上传（OSS／S3）？",
    },
    "askAutoUpload": {"en": "Enable auto-upload?", "zh": "开启自动上传？"},
    "askBitrate": {"en": "Set video bitrate?", "zh": "设置视频码率？"},
    "askResolution": {"en": "Set camera resolution?", "zh": "设置相机分辨率？"},
    "askWifi": {"en": "Configure device Wi-Fi?", "zh": "配置设备 Wi-Fi？"},
    "cloudProvider": {"en": "cloud provider:", "zh": "云服务商："},
    "customEndpoint": {"en": "custom endpoint URL", "zh": "自定义 endpoint URL"},
    "choose": {"en": "choose", "zh": "请选择"},
    "notAChoice": {
        "en": "not one of the choices: {value!r}",
        "zh": "不是可选项：{value!r}",
    },
    "ambiguous": {
        "en": "{value!r} matches {hits} — be more specific",
        "zh": "{value!r} 匹配到 {hits} —— 请输入更精确的名称",
    },
    "endpointUrl": {"en": "endpoint_url (https://...)", "zh": "endpoint_url（https://...）"},
    "prefix": {"en": "prefix", "zh": "路径前缀"},
    "ssid": {"en": "ssid", "zh": "Wi-Fi 名称 (SSID)"},
    "wifiPass": {
        "en": "  passphrase (empty = open network): ",
        "zh": "  Wi-Fi 密码（留空表示开放网络）：",
    },
    "secretSuffix": {
        "en": " (empty = device keeps its stored secret)",
        "zh": "（留空表示保留设备上已存的密钥）",
    },
    # -- interactive: the recording key --------------------------------- #
    "encHeading": {"en": "Recording encryption", "zh": "录制加密"},
    "askSetKey": {
        "en": "  Set the recording key (encrypts recordings on the card)?",
        "zh": "  设置录制密钥（加密存储卡上的录制内容）？",
    },
    "encProofHint": {
        "en": ("  Replacing or clearing a key requires proving you know the "
               "current one.\n"
               "  Give its FILE, or its 16-hex FINGERPRINT to take it from "
               "this\n"
               "  computer's keyring (where visio-display keeps every key it "
               "has\n"
               "  minted). Leave blank if these rigs have never been keyed."),
        "zh": ("  更换或清除密钥需要证明你知道当前密钥。\n"
               "  可提供密钥文件，或其 16 位十六进制指纹，以便从本机钥匙串\n"
               "  中取用（visio-display 生成的每个密钥都保存在那里）。\n"
               "  若这些设备从未设置过密钥，请留空。"),
    },
    "askCurrentKey": {
        "en": "  current key file or fingerprint (blank = never keyed)",
        "zh": "  当前密钥文件或指纹（留空表示从未设置）",
    },
    "notOnKeyring": {
        "en": ("  {fp} is not on this computer's keyring — give the key file "
               "instead."),
        "zh": "  本机钥匙串中没有 {fp} —— 请改为提供密钥文件。",
    },
    "askStopEncrypting": {
        "en": "  STOP encrypting (new recordings become readable)?",
        "zh": "  停止加密（此后的录制内容将可被直接读取）？",
    },
    "askExistingKey": {
        "en": "  path to an existing key file (blank = mint a new one)",
        "zh": "  已有密钥文件路径（留空表示新建一个）",
    },
    "askKeyOut": {"en": "  write the new key to", "zh": "  新密钥保存到"},
    # -- key safety ------------------------------------------------------ #
    "keyWritten": {"en": "wrote {path} (0600)", "zh": "已写入 {path}（0600）"},
    "keyFingerprint": {"en": "fingerprint: {fp}", "zh": "密钥指纹：{fp}"},
    "keyLossWarning": {
        "en": ("LOSE THIS FILE AND EVERY RECORDING MADE UNDER IT IS "
               "PERMANENTLY UNREADABLE. There is no recovery path."),
        "zh": "一旦丢失此文件，用它加密的所有录制内容将永远无法读取，且无任何恢复途径。",
    },
    "keyArchiveNote": {
        "en": ("NOTE: recordings made under a PREVIOUS key stay readable only "
               "with that key — keep every key you have ever used."),
        "zh": "注意：使用旧密钥录制的内容只能用该旧密钥打开——请保留你用过的每一个密钥。",
    },
    "wifiUnsealed": {
        "en": ("warning: wifi.passphrase is NOT sealed — it stays readable in "
               "this code. Sealing it needs firmware support that does not "
               "exist yet; omit the wifi section if that matters."),
        "zh": ("警告：wifi.passphrase 未被加密——它在此二维码中仍可被读取。"
               "加密它需要尚未实现的固件支持；如果这一点重要，请不要填写 Wi-Fi 部分。"),
    },
    "qrWritten": {"en": "QR version {version} -> {path}",
                  "zh": "二维码版本 {version} -> {path}"},
}


def detect_language() -> str:
    """The language to use, from $VISIO_LANG or the POSIX locale."""
    for var in ("VISIO_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "").strip().lower()
        if not value:
            continue
        for known in LANGUAGES:
            if value.startswith(known):
                return known
        # A set-but-unrecognised locale is an answer: stop looking and take
        # English, rather than letting a lower-priority variable override it.
        return "en"
    return "en"


def set_language(lang: str | None) -> str:
    """Fix the language for this process; `None` means auto-detect."""
    global _lang
    _lang = lang if lang in LANGUAGES else detect_language()
    return _lang


def tr(key: str, **fmt: object) -> str:
    """The active language's text for `key`, English if untranslated.

    An unknown key renders as the key itself — the launcher page does the same.
    A half-provisioned fleet is a worse outcome than an ugly prompt, so a
    missing translation must never raise in the middle of a key change.
    """
    entry = _STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(_lang) or entry["en"]
    return text.format(**fmt) if fmt else text

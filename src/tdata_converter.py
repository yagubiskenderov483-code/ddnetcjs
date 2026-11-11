"""Convert Telethon .session (SQLite) auth_key → Telegram Desktop tdata format.

No external dependencies beyond `cryptography` (for AES).
Produces a minimal tdata directory that TDesktop can read:
  tdata/
    key_datas
    D877F783D5D3EF8Cs          — account map
    <random_filekey>s          — MTP auth data
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import struct
from pathlib import Path
from typing import List, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend


# ── AES helpers ──────────────────────────────────────────────────────────────

def _aes_ecb_encrypt(key: bytes, block: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(block) + enc.finalize()


def _aes_ige_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-256-IGE as in OpenSSL / Telegram."""
    assert len(key) == 32 and len(iv) == 32 and len(data) % 16 == 0

    c_prev = bytearray(iv[:16])
    m_prev = bytearray(iv[16:32])
    out = bytearray()

    for i in range(0, len(data), 16):
        m = data[i : i + 16]
        xor_in = bytes(a ^ b for a, b in zip(m, m_prev))
        enc = _aes_ecb_encrypt(key, xor_in)
        c = bytes(a ^ b for a, b in zip(enc, c_prev))
        out.extend(c)
        m_prev = bytearray(m)
        c_prev = bytearray(c)

    return bytes(out)


# ── tdata crypto primitives ──────────────────────────────────────────────────

def _create_local_key(passcode: bytes, salt: bytes) -> bytes:
    """PBKDF2-SHA512 → 256-byte local key (empty passcode → 1 iter)."""
    iters = 1 if not passcode else 100_000
    return hashlib.pbkdf2_hmac("sha512", passcode, salt, iters, dklen=256)


def _prepare_aes_old(key: bytes, msg_key: bytes) -> Tuple[bytes, bytes]:
    """Old-MTProto-style AES-256 key + 32-byte IV derivation (send=True, x=0)."""
    sha1a = hashlib.sha1(msg_key[:16] + key[0:32]).digest()
    sha1b = hashlib.sha1(key[32:48] + msg_key[:16] + key[48:64]).digest()
    sha1c = hashlib.sha1(key[64:80] + msg_key[:16]).digest()
    sha1d = hashlib.sha1(msg_key[:16] + key[96:128]).digest()

    aes_key = sha1a[:8] + sha1b[8:20] + sha1c[4:16]          # 32
    aes_iv  = sha1a[8:20] + sha1b[:8] + sha1c[16:20] + sha1d[:8]  # 32
    return aes_key, aes_iv


def _encrypt_local(data: bytes, key: bytes) -> bytes:
    """Encrypt `data` with tdesktop's EncryptLocal convention."""
    pad_need = (16 - len(data) % 16) % 16
    padded = data + os.urandom(pad_need) if pad_need else data

    msg_key = hashlib.sha1(padded).digest()[4:20]          # last 16 of SHA-1
    aes_key, aes_iv = _prepare_aes_old(key, msg_key)
    encrypted = _aes_ige_encrypt(padded, aes_key, aes_iv)
    return msg_key + encrypted


# ── TDF file format ──────────────────────────────────────────────────────────

_TDF_MAGIC   = b"TDF$"
_TDF_VERSION = struct.pack("<i", 4002000)      # tdesktop ~v4.2


def _qba(blob: bytes) -> bytes:
    """QDataStream QByteArray: uint32-BE length + raw bytes."""
    return struct.pack(">I", len(blob)) + blob


def _write_tdf(inner_data: bytes) -> bytes:
    """Wrap inner_data into a TDF$ file."""
    md5_src = inner_data + struct.pack("<I", len(inner_data)) + _TDF_VERSION + _TDF_MAGIC
    checksum = hashlib.md5(md5_src).digest()
    return _TDF_MAGIC + _TDF_VERSION + inner_data + checksum


def _file_part(val: int) -> str:
    """FileKey (uint64) → 16-char hex filename, LSB-first nibbles."""
    chars: List[str] = []
    for _ in range(16):
        v = val & 0x0F
        chars.append(f"{v:X}")
        val >>= 4
    return "".join(chars)


# ── public API ───────────────────────────────────────────────────────────────

def read_telethon_session(session_path: str) -> List[Tuple[int, bytes]]:
    """Read [(dc_id, auth_key_256)] from a Telethon .session SQLite file."""
    con = sqlite3.connect(session_path)
    rows = con.execute("SELECT dc_id, auth_key FROM sessions WHERE auth_key IS NOT NULL").fetchall()
    con.close()
    result: List[Tuple[int, bytes]] = []
    for dc_id, key_blob in rows:
        if key_blob and len(key_blob) == 256:
            result.append((int(dc_id), bytes(key_blob)))
    return result


def build_tdata(
    dc_keys: List[Tuple[int, bytes]],
    main_dc: int = 2,
    out_dir: str | Path = "/tmp/tdata_out",
) -> Path:
    """Assemble a minimal tdata/ directory from DC auth keys.

    Returns the path to the created ``tdata/`` folder.
    """
    out = Path(out_dir)
    tdata = out / "tdata"
    if tdata.exists():
        shutil.rmtree(tdata)
    tdata.mkdir(parents=True)

    # ── 1. key_datas ──────────────────────────────────────────────────────
    salt = os.urandom(32)
    local_key = os.urandom(256)
    passcode_key = _create_local_key(b"", salt)

    encrypted_key = _encrypt_local(local_key, passcode_key)

    # info: count=1 (int32 BE), active=0 (int32 BE)
    info_data = struct.pack(">i", 1) + struct.pack(">i", 0)
    encrypted_info = _encrypt_local(info_data, local_key)

    key_datas_inner = _qba(salt) + _qba(encrypted_key) + _qba(encrypted_info)
    (tdata / "key_datas").write_bytes(_write_tdf(key_datas_inner))

    # ── 2. MTP auth data file ─────────────────────────────────────────────
    # mainDcId (int32 BE) + count (int32 BE) + [dcId (int32 BE) + key (256)]…
    auth_payload = struct.pack(">i", main_dc) + struct.pack(">i", len(dc_keys))
    for dc_id, key_blob in dc_keys:
        auth_payload += struct.pack(">i", dc_id) + key_blob

    encrypted_auth = _encrypt_local(auth_payload, local_key)
    auth_file_key = struct.unpack("<Q", os.urandom(8))[0]
    auth_fname = _file_part(auth_file_key) + "s"

    auth_inner = _qba(encrypted_auth)
    (tdata / auth_fname).write_bytes(_write_tdf(auth_inner))

    # ── 3. account map file ───────────────────────────────────────────────
    # map entries: lskMtpAuthorization (uint32 BE = 0x04) + FileKey (uint64 BE)
    map_payload = struct.pack(">I", 0x04) + struct.pack(">Q", auth_file_key)
    encrypted_map = _encrypt_local(map_payload, local_key)

    # account folder hash: MD5("data") → first 8 bytes as uint64 LE → _file_part
    acct_hash = hashlib.md5(b"data").digest()
    acct_key = struct.unpack("<Q", acct_hash[:8])[0]
    acct_name = _file_part(acct_key)                        # D877F783D5D3EF8C

    map_inner = _qba(encrypted_map)
    (tdata / (acct_name + "s")).write_bytes(_write_tdf(map_inner))

    return tdata


def session_to_tdata(session_path: str, out_dir: str | Path = "/tmp/tdata_out") -> Path:
    """One-call: Telethon .session → tdata/ directory.  Returns path to tdata/."""
    dc_keys = read_telethon_session(session_path)
    if not dc_keys:
        raise ValueError("No auth keys found in session file")
    # pick main DC: prefer DC 2 (production), else first available
    main = 2 if any(d == 2 for d, _ in dc_keys) else dc_keys[0][0]
    return build_tdata(dc_keys, main_dc=main, out_dir=out_dir)

"""Import the static dashboard bundle (output/site) straight into the database.

The built site is self-describing:

    dse-data/manifest.json   PBKDF2 salt + per-file AES-GCM IV
    dse-data/core.bin        meta / tree / dates / opening / dse_customers
    dse-data/c/<dse>.bin     one file per DSE with its customer records

Every file is raw-deflate compressed, then AES-256-GCM encrypted with a key
derived from the dashboard password. This module reverses that, reassembles the
payload the template expects, and hands it to ``store.save_payload``.

Because the built bundle is the single source of truth for the day, this path
never touches ``Input/`` and never runs the ETL scripts.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
import zlib
from pathlib import Path
from typing import Callable, Iterable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings

AAD_PREFIX = "dgp-dashboard-v2:"
_MANIFEST = "manifest.json"
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB guard against zip bombs


class SiteBundleError(Exception):
    """Raised when the uploaded folder is not a usable dashboard bundle."""


def _b64(value: str) -> bytes:
    return base64.b64decode(value)


def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, iterations, dklen=32)


def _decrypt_file(data_dir: Path, rel: str, key: bytes, iv_b64: str) -> dict:
    path = data_dir / f"{rel}.bin"
    if not path.is_file():
        raise SiteBundleError(f"bundle is missing {rel}.bin")
    aes = AESGCM(key)
    ciphertext = path.read_bytes()
    plaintext = aes.decrypt(_b64(iv_b64), ciphertext, AAD_PREFIX.encode() + rel.encode())
    return json.loads(zlib.decompress(plaintext, -15).decode("utf-8"))


def _read_plain_file(data_dir: Path, rel: str) -> dict:
    """Read an unencrypted bundle file: raw-deflate JSON."""
    path = data_dir / f"{rel}.bin"
    if not path.is_file():
        raise SiteBundleError(f"bundle is missing {rel}.bin")
    return json.loads(zlib.decompress(path.read_bytes(), -15).decode("utf-8"))


def _candidate_passwords(plan_dates: Iterable[str]) -> list[str]:
    """Passwords to try, most likely first.

    ``build_dashboard.py`` derives ``SME#DDYYYYMM`` from the latest plan visit
    date. We seed the list with every plan date we can see, then fall back to a
    rolling window so a freshly built bundle always resolves.
    """
    seen: list[str] = []

    env_pw = os.environ.get("DASHBOARD_PASSWORD")
    if env_pw and env_pw.strip():
        seen.append(env_pw.strip())

    def add(d: dt.date) -> None:
        candidate = f"SME#{d:%d%Y%m}"
        if candidate not in seen:
            seen.append(candidate)

    parsed: list[dt.date] = []
    for value in plan_dates:
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                parsed.append(dt.datetime.strptime(text, fmt).date())
                break
            except ValueError:
                continue
    for d in sorted(parsed, reverse=True):
        add(d)

    today = dt.date.today()
    for offset in range(0, 400):
        add(today - dt.timedelta(days=offset))
    return seen


def _history_plan_dates() -> list[str]:
    """Latest plan dates recorded in History/, used to guess the password."""
    dates: list[str] = []
    history = settings.history_dir
    if not history.is_dir():
        return dates
    pattern = re.compile(r"Easy_SBF_Gap_(\d{8})", re.IGNORECASE)
    for path in history.glob("Easy_SBF_Gap_*.xlsx"):
        m = pattern.search(path.name)
        if m:
            dates.append(m.group(1))
    return dates


def decrypt_bundle(data_dir: Path, log: Callable[[str], None]) -> dict:
    """Read a bundle folder into one payload dict.

    Handles both bundle formats:

    * v3 (current) - plain raw-deflate JSON, no password.
    * v2 (legacy) - AES-256-GCM encrypted with the old SME#DDYYYYMM password,
      kept working so previously built sites still import.
    """
    manifest_path = data_dir / _MANIFEST
    if not manifest_path.is_file():
        raise SiteBundleError(
            "manifest.json not found. Zip the output/site folder (it must contain "
            "dse-data/manifest.json, dse-data/core.bin and dse-data/c/)."
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest["files"]

    read = _plain_reader(data_dir) if not _is_encrypted(manifest) else None
    if read is None:
        read = _encrypted_reader(data_dir, manifest, log)
    else:
        log(f"bundle format v{manifest.get('v')} (plain, no password)")

    core = read("core")
    core.setdefault("customers", {})

    total = 0
    for rel in files:
        if rel == "core":
            continue
        try:
            core["customers"].update(read(rel))
        except SiteBundleError:
            continue
        total += 1

    log(f"bundle format v{manifest.get('v')} "
        f"({'encrypted' if _is_encrypted(manifest) else 'plain'}): "
        f"{total} chunks merged, {len(core['customers'])} customer records")
    return core


def _is_encrypted(manifest: dict) -> bool:
    return bool(manifest.get("salt")) and bool(manifest.get("iterations"))


def _plain_reader(data_dir: Path) -> Callable[[str], dict]:
    return lambda rel: _read_plain_file(data_dir, rel)


def _encrypted_reader(data_dir: Path, manifest: dict,
                      log: Callable[[str], None]) -> Callable[[str], dict]:
    salt = _b64(manifest["salt"])
    iterations = int(manifest["iterations"])
    files = manifest["files"]
    candidates = _candidate_passwords(_history_plan_dates())
    log(f"legacy encrypted bundle: {len(files)} files, PBKDF2 iterations={iterations}")

    for index, password in enumerate(candidates, start=1):
        trial = _derive_key(password, salt, iterations)
        try:
            AESGCM(trial).decrypt(
                _b64(files["core"]["iv"]),
                (data_dir / "core.bin").read_bytes(),
                AAD_PREFIX.encode() + b"core",
            )
        except Exception:
            continue
        log("bundle decrypted (legacy password accepted)")
        return lambda rel: _decrypt_file(data_dir, rel, trial, files[rel]["iv"])

    raise SiteBundleError(
        "Could not decrypt this legacy encrypted bundle. Its password was "
        "derived from the plan date at build time; rebuild the site with the "
        "current build_dashboard.py (which no longer encrypts) and retry."
    )


def _safe_extract(zip_path: Path, dest: Path) -> Path:
    """Extract a zip, rejecting traversal, and return the dse-data folder."""
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise SiteBundleError(
            "That file is not a valid zip. Zip the whole output/site folder."
        ) from exc

    with archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise SiteBundleError(f"unsafe path in archive: {info.filename}")
            total += info.file_size
            if total > _MAX_TOTAL_BYTES:
                raise SiteBundleError("archive is too large")
            archive.extract(info, dest)

    direct = dest / "dse-data"
    if direct.is_dir():
        return direct
    for manifest in dest.rglob(_MANIFEST):
        return manifest.parent
    raise SiteBundleError(
        "No dse-data/manifest.json found. Zip the output/site folder itself "
        "(it must contain index.html and the dse-data folder)."
    )


def import_from_zip(data: bytes, log: Callable[[str], None]) -> dict:
    """Unpack an uploaded zip of output/site and return the payload."""
    tmp = Path(tempfile.mkdtemp(prefix="dgp_site_"))
    try:
        archive = tmp / "site.zip"
        archive.write_bytes(data)
        data_dir = _safe_extract(archive, tmp / "unzipped")
        return decrypt_bundle(data_dir, log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def import_from_disk(site_dir: Path, log: Callable[[str], None]) -> dict:
    """Import a bundle that is already on the server filesystem."""
    data_dir = site_dir / "dse-data"
    if not data_dir.is_dir():
        raise SiteBundleError(f"{data_dir} not found")
    return decrypt_bundle(data_dir, log)


def default_site_dir() -> Path:
    return settings.project_root / "output" / "site"
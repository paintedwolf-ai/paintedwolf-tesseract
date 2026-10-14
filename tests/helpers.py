"""Synthetic engine archives and inputs for the tooling tests."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import engine as ENGINE  # noqa: E402

TRAINED = {"eng.traineddata": b"english model", "osd.traineddata": b"orientation model"}
LICENSES = {name: ("text of " + name).encode() for name in ENGINE.LICENSE_FILES}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def elf(machine, interpreter=False):
    """A minimal 64-bit little-endian ELF image with one or two program headers."""
    kinds = [1, 3] if interpreter else [1]
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4], header[5], header[6] = 2, 1, 1
    header[18:20] = machine.to_bytes(2, "little")
    header[32:40] = (64).to_bytes(8, "little")
    header[54:56] = (56).to_bytes(2, "little")
    header[56:58] = len(kinds).to_bytes(2, "little")
    programs = b"".join(kind.to_bytes(4, "little") + bytes(52) for kind in kinds)
    return bytes(header) + programs + b"code"


def inputs():
    """The real lock and notices, with trained data and license digests for the fakes."""
    lock = copy.deepcopy(ENGINE.load_lock())
    for name, data in TRAINED.items():
        lock["sources"]["tessdata_fast"]["files"][name] = {"sha256": sha(data), "bytes": len(data)}
    inventory = ENGINE.read_json(ENGINE.INVENTORY_PATH)
    inventory["notices"] = {name: sha(data) for name, data in LICENSES.items()}
    lock_bytes = json.dumps(lock, indent=2).encode()
    return {"lock": lock, "lock_bytes": lock_bytes, "inventory": inventory, "notices": ENGINE.NOTICES_PATH.read_bytes()}


def payload(platform, given, binary=None):
    lock = given["lock"]
    version = ENGINE.engine_version(lock)
    binary = elf(ENGINE.PLATFORMS[platform]["elf_machine"]) if binary is None else binary
    provenance = {"engine_version": version, "platform": platform, "source_lock_sha256": sha(given["lock_bytes"]),
                  "binary": {"sha256": sha(binary), "bytes": len(binary)},
                  "build": {"base_image": lock["build_image"], "packages": "musl-1.2.5-r10 gcc-14.2.0-r6",
                            "source_date_epoch": lock["source_date_epoch"]}}
    files = {"ocr/VERSION": (version + "\n").encode(), "ocr/provenance.json": json.dumps(provenance).encode(),
             "ocr/source-lock.json": given["lock_bytes"], "ocr/NOTICES-tesseract.md": given["notices"],
             "ocr/bin/tesseract": binary}
    files.update({"ocr/tessdata/" + name: data for name, data in TRAINED.items()})
    files.update({"ocr/" + name: data for name, data in LICENSES.items()})
    return files


def write_archive(directory, platform, given, files=None, extra=None):
    """Write an archive named for the platform; files maps member names to bytes."""
    files = payload(platform, given) if files is None else files
    path = Path(directory) / ENGINE.archive_name(ENGINE.engine_version(given["lock"]), platform)
    with tarfile.open(path, "w:gz") as archive:
        for name in ("ocr", "ocr/bin", "ocr/tessdata", "ocr/licenses"):
            info = tarfile.TarInfo(name)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(info)
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755 if name.endswith("/tesseract") else 0o644
            archive.addfile(info, io.BytesIO(data))
        for info in extra or ():
            archive.addfile(info)
    return path

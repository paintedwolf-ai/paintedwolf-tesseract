#!/usr/bin/env python3
"""Check the engine inputs, build an engine archive, and verify one."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform as host
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
LOCK_PATH = ENGINE / "source-lock.json"
INVENTORY_PATH = ENGINE / "licensing/inventory.json"
NOTICES_PATH = ENGINE / "licensing/NOTICES-tesseract.md"
FIXTURES = ROOT / "tests/fixtures"

# Each released platform: its Docker platform and the ELF machine and host
# machine names that identify it.
PLATFORMS = {
    "linux-amd64": {"docker": "linux/amd64", "goos": "linux", "goarch": "amd64",
                    "elf_machine": 62, "host_machines": {"x86_64", "amd64"}},
    "linux-arm64": {"docker": "linux/arm64", "goos": "linux", "goarch": "arm64",
                    "elf_machine": 183, "host_machines": {"aarch64", "arm64"}},
}
SOURCES = {"tesseract", "leptonica", "tessdata_fast", "musl", "gcc_runtime"}
TRAINED_DATA = ("eng.traineddata", "osd.traineddata")
LICENSE_FILES = ("licenses/LICENSE-tesseract", "licenses/LICENSE-leptonica", "licenses/COPYRIGHT-musl",
                 "licenses/GPL-3.0", "licenses/GCC-RUNTIME-LIBRARY-EXCEPTION-3.1")
ARCHIVE_FILES = {"ocr/VERSION", "ocr/provenance.json", "ocr/source-lock.json", "ocr/NOTICES-tesseract.md",
                 "ocr/bin/tesseract", *("ocr/tessdata/" + name for name in TRAINED_DATA),
                 *("ocr/" + name for name in LICENSE_FILES)}
ARCHIVE_DIRECTORIES = {"ocr", "ocr/bin", "ocr/tessdata", "ocr/licenses"}
MAX_ARCHIVE_BYTES = 64 << 20
MAX_MEMBER_BYTES = 64 << 20
FIXTURE_WORDS = ("Painted", "Wolf", "Code", "rotated", "capture")

HEX64 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
IMAGE = re.compile(r"[a-z0-9][a-z0-9./_-]*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}")


class EngineError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise EngineError(message)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "Expected a regular file: " + str(path))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    with Path(path).open() as stream:
        return json.load(stream)


def engine_version(lock):
    return lock["upstream_version"] + "+paintedwolf." + str(lock["revision"])


def archive_name(version, platform):
    return "paintedwolf-tesseract-" + version + "-" + platform + ".tar.gz"


def _https(value):
    return isinstance(value, str) and value.startswith("https://")


def _digest(value):
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def check_lock(lock):
    require(lock.get("schema_version") == 1, "source-lock.json: unsupported schema_version")
    require(isinstance(lock.get("upstream_version"), str) and VERSION.fullmatch(lock["upstream_version"]),
            "source-lock.json: upstream_version must be MAJOR.MINOR.PATCH")
    require(type(lock.get("revision")) is int and lock["revision"] > 0, "source-lock.json: revision must be positive")
    require(lock.get("platforms") == sorted(PLATFORMS), "source-lock.json: platforms must be " + ", ".join(sorted(PLATFORMS)))
    require(isinstance(lock.get("build_image"), str) and IMAGE.fullmatch(lock["build_image"]),
            "source-lock.json: build_image must be pinned by digest")
    require(type(lock.get("source_date_epoch")) is int and lock["source_date_epoch"] > 0,
            "source-lock.json: source_date_epoch must be positive")
    sources = lock.get("sources", {})
    require(set(sources) == SOURCES, "source-lock.json: sources must be exactly " + ", ".join(sorted(SOURCES)))
    for name in ("tesseract", "leptonica"):
        source = sources[name]
        require(isinstance(source.get("version"), str) and VERSION.fullmatch(source["version"]),
                "source-lock.json: " + name + " version must be MAJOR.MINOR.PATCH")
        require(_https(source.get("url")) and source["version"] in source["url"],
                "source-lock.json: " + name + " url must be https and name its version")
        require(_digest(source.get("sha256")), "source-lock.json: " + name + " sha256 is not a digest")
    require(sources["tesseract"]["version"] == lock["upstream_version"],
            "source-lock.json: tesseract version differs from upstream_version")
    tessdata = sources["tessdata_fast"]
    require(isinstance(tessdata.get("commit"), str) and COMMIT.fullmatch(tessdata["commit"]),
            "source-lock.json: tessdata_fast commit must be a full commit")
    require(set(tessdata.get("files", {})) == set(TRAINED_DATA), "source-lock.json: tessdata_fast files must be eng and osd")
    for name, record in tessdata["files"].items():
        require(_digest(record.get("sha256")) and type(record.get("bytes")) is int and record["bytes"] > 0,
                "source-lock.json: " + name + " needs a digest and size")
    for name, keys in (("musl", ("copyright",)), ("gcc_runtime", ("gpl3", "exception"))):
        source = sources[name]
        require(isinstance(source.get("version"), str) and VERSION.fullmatch(source["version"]),
                "source-lock.json: " + name + " version must be MAJOR.MINOR.PATCH")
        for key in keys:
            require(_https(source.get(key + "_url")) and _digest(source.get(key + "_sha256")),
                    "source-lock.json: " + name + " " + key + " needs an https url and digest")
    return lock


def load_lock(path=LOCK_PATH):
    return check_lock(read_json(path))


def check_inventory(lock, inventory, notices):
    version = engine_version(lock)
    require(inventory.get("schema_version") == 1, "inventory.json: unsupported schema_version")
    require(inventory.get("artifact") == {"name": "paintedwolf-tesseract", "version": version},
            "inventory.json: artifact identity differs from the source lock")
    components = inventory.get("components", [])
    require(sorted(component.get("source") for component in components) == sorted(SOURCES),
            "inventory.json: components must cover every locked source once")
    pinned = inventory.get("notices", {})
    require(set(pinned) == set(LICENSE_FILES) and all(_digest(value) for value in pinned.values()),
            "inventory.json: notices must pin every archived license text")
    sources = lock["sources"]
    locked_texts = {"licenses/COPYRIGHT-musl": sources["musl"]["copyright_sha256"],
                    "licenses/GPL-3.0": sources["gcc_runtime"]["gpl3_sha256"],
                    "licenses/GCC-RUNTIME-LIBRARY-EXCEPTION-3.1": sources["gcc_runtime"]["exception_sha256"]}
    for path, digest in locked_texts.items():
        require(pinned[path] == digest, "inventory.json: " + path + " digest differs from the source lock")
    require(notices.startswith("# Third-party notices — Painted Wolf Tesseract " + version + "\n"),
            "NOTICES-tesseract.md: heading must name version " + version)
    for component in components:
        source = sources[component["source"]]
        expected = source["commit"] if component["source"] == "tessdata_fast" else source["version"]
        require(component.get("version") == expected,
                "inventory.json: " + component["source"] + " version differs from the source lock")
        require(component.get("notice") in pinned, "inventory.json: " + component["source"] + " names no archived notice")
        require(isinstance(component.get("license"), str) and component["license"],
                "inventory.json: " + component["source"] + " has no license")
        heading = "## " + component["name"] + " " + component["version"] + "\n"
        require(heading in notices, "NOTICES-tesseract.md: missing section " + heading.strip())
        section = notices.split(heading, 1)[1].split("\n## ", 1)[0]
        require(component["license"] in section and component["notice"] in section,
                "NOTICES-tesseract.md: " + component["name"] + " must state its license and notice file")
    return inventory


def check_inputs():
    lock = load_lock()
    check_inventory(lock, read_json(INVENTORY_PATH), NOTICES_PATH.read_text())
    for name in ("upright.pgm", "upside-down.pgm"):
        require((FIXTURES / name).read_bytes().startswith(b"P5\n"), "Fixture is not a binary PGM: " + name)
    return {"version": engine_version(lock), "source_lock_sha256": sha256_file(LOCK_PATH)}


def build_args(lock, lock_sha256):
    sources = lock["sources"]
    return {
        "BASE_IMAGE": lock["build_image"],
        "ENGINE_VERSION": engine_version(lock),
        "SOURCE_LOCK_SHA256": lock_sha256,
        "TESSERACT_VERSION": sources["tesseract"]["version"],
        "TESSERACT_URL": sources["tesseract"]["url"],
        "TESSERACT_SHA256": sources["tesseract"]["sha256"],
        "LEPTONICA_VERSION": sources["leptonica"]["version"],
        "LEPTONICA_URL": sources["leptonica"]["url"],
        "LEPTONICA_SHA256": sources["leptonica"]["sha256"],
        "TESSDATA_FAST_COMMIT": sources["tessdata_fast"]["commit"],
        "ENG_SHA256": sources["tessdata_fast"]["files"]["eng.traineddata"]["sha256"],
        "OSD_SHA256": sources["tessdata_fast"]["files"]["osd.traineddata"]["sha256"],
        "MUSL_COPYRIGHT_URL": sources["musl"]["copyright_url"],
        "MUSL_COPYRIGHT_SHA256": sources["musl"]["copyright_sha256"],
        "GPL3_URL": sources["gcc_runtime"]["gpl3_url"],
        "GPL3_SHA256": sources["gcc_runtime"]["gpl3_sha256"],
        "GCC_EXCEPTION_URL": sources["gcc_runtime"]["exception_url"],
        "GCC_EXCEPTION_SHA256": sources["gcc_runtime"]["exception_sha256"],
        "SOURCE_DATE_EPOCH": str(lock["source_date_epoch"]),
    }


def build_command(lock, lock_sha256, platform, output):
    argv = ["docker", "buildx", "build", "--platform", PLATFORMS[platform]["docker"], "--target", "artifact",
            "--output", "type=local,dest=" + str(output)]
    for key, value in build_args(lock, lock_sha256).items():
        argv += ["--build-arg", key + "=" + value]
    return argv + ["-f", str(ENGINE / "Dockerfile"), str(ENGINE)]


def build(platform, output):
    check_inputs()
    lock = load_lock()
    output = Path(output)
    require(not output.exists(), "Build output must be a new directory: " + str(output))
    subprocess.run(build_command(lock, sha256_file(LOCK_PATH), platform, output), check=True)
    archive = output / archive_name(engine_version(lock), platform)
    require({path.name for path in output.iterdir()} == {archive.name},
            "Build produced unexpected output in " + str(output))
    return archive


def read_archive(path):
    """Read an archive's files, refusing anything but plain files and directories under ocr/."""
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "Archive is not a regular file: " + str(path))
    require(path.stat().st_size <= MAX_ARCHIVE_BYTES, "Archive exceeds its size limit")
    files, directories = {}, set()
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and ".." not in name.parts and name.parts[:1] == ("ocr",),
                    "Archive member escapes ocr/: " + member.name)
            if member.isdir():
                directories.add(str(name))
                continue
            require(member.isfile(), "Archive member is not a regular file: " + member.name)
            require(member.size <= MAX_MEMBER_BYTES, "Archive member exceeds its size limit: " + member.name)
            require(str(name) not in files, "Archive repeats a member: " + member.name)
            files[str(name)] = (archive.extractfile(member).read(), member.mode)
    require(set(files) == ARCHIVE_FILES, "Archive file set differs: unexpected "
            + ", ".join(sorted(set(files) - ARCHIVE_FILES)) + "; missing " + ", ".join(sorted(ARCHIVE_FILES - set(files))))
    require(directories <= ARCHIVE_DIRECTORIES, "Archive has unexpected directories")
    return files


def elf_facts(data):
    """The ELF machine and whether the image names a program interpreter."""
    require(len(data) >= 64 and data[:4] == b"\x7fELF", "Executable is not an ELF image")
    require(data[4] == 2 and data[5] == 1, "Executable is not a 64-bit little-endian ELF image")
    machine = int.from_bytes(data[18:20], "little")
    phoff = int.from_bytes(data[32:40], "little")
    phentsize = int.from_bytes(data[54:56], "little")
    phnum = int.from_bytes(data[56:58], "little")
    require(phentsize >= 4 and phoff + phentsize * phnum <= len(data), "ELF program headers are truncated")
    kinds = {int.from_bytes(data[phoff + i * phentsize:phoff + i * phentsize + 4], "little") for i in range(phnum)}
    return machine, 3 in kinds


def load_inputs():
    """The checked-in inputs an archive is verified against."""
    return {"lock": load_lock(), "lock_bytes": LOCK_PATH.read_bytes(),
            "inventory": read_json(INVENTORY_PATH), "notices": NOTICES_PATH.read_bytes()}


def verify_archive(path, platform, inputs=None, run=True):
    """Verify an engine archive against the inputs; run it when this host can."""
    inputs = load_inputs() if inputs is None else inputs
    lock, lock_bytes, inventory, notices = inputs["lock"], inputs["lock_bytes"], inputs["inventory"], inputs["notices"]
    version = engine_version(lock)
    path = Path(path)
    require(path.name == archive_name(version, platform), "Archive name differs from " + archive_name(version, platform))
    files = read_archive(path)
    content = {name: data for name, (data, _) in files.items()}
    require(content["ocr/VERSION"] == (version + "\n").encode(), "Archive VERSION differs from " + version)
    require(content["ocr/source-lock.json"] == lock_bytes, "Archive source lock differs from engine/source-lock.json")
    require(content["ocr/NOTICES-tesseract.md"] == notices, "Archive notices differ from engine/licensing")
    for name in TRAINED_DATA:
        record = lock["sources"]["tessdata_fast"]["files"][name]
        data = content["ocr/tessdata/" + name]
        require(len(data) == record["bytes"] and sha256_bytes(data) == record["sha256"],
                "Archive " + name + " differs from the source lock")
    for name, digest in inventory["notices"].items():
        require(sha256_bytes(content["ocr/" + name]) == digest, "Archive " + name + " differs from the license inventory")
    binary = content["ocr/bin/tesseract"]
    require(files["ocr/bin/tesseract"][1] & 0o111, "Archive executable is not executable")
    machine, interpreted = elf_facts(binary)
    require(machine == PLATFORMS[platform]["elf_machine"], "Executable is built for another machine")
    require(not interpreted, "Executable is dynamically linked")
    provenance = json.loads(content["ocr/provenance.json"])
    expected = {"engine_version": version, "platform": platform, "source_lock_sha256": sha256_bytes(lock_bytes),
                "binary": {"sha256": sha256_bytes(binary), "bytes": len(binary)}}
    for key, value in expected.items():
        require(provenance.get(key) == value, "Archive provenance differs: " + key)
    build_record = provenance.get("build", {})
    require(build_record.get("base_image") == lock["build_image"]
            and build_record.get("source_date_epoch") == lock["source_date_epoch"]
            and isinstance(build_record.get("packages"), str) and build_record["packages"],
            "Archive provenance does not record the locked build")
    facts = {"platform": platform, "archive": {"name": path.name, "sha256": sha256_file(path), "bytes": path.stat().st_size},
             "binary": {"sha256": sha256_bytes(binary), "bytes": len(binary)},
             "packages": build_record["packages"], "executed": False}
    if run and runs_here(platform):
        facts["engine"] = functional_check(path, lock)
        facts["executed"] = True
    return facts


def runs_here(platform):
    return host.system() == "Linux" and host.machine().lower() in PLATFORMS[platform]["host_machines"]


def recognize(binary, tessdata, image):
    result = subprocess.run([str(binary), "stdin", "stdout", "--psm", "1", "-l", "eng",
                             "-c", "min_orientation_margin=1"],
                            input=image.read_bytes(), capture_output=True, timeout=120,
                            env={"TESSDATA_PREFIX": str(tessdata), "OMP_THREAD_LIMIT": "1", "PATH": "/usr/bin:/bin"})
    require(result.returncode == 0, "Engine failed on " + image.name + ": " + result.stderr.decode(errors="replace"))
    return result.stdout.decode(errors="replace")


def functional_check(path, lock):
    """Run the executable from a fresh extraction: report its version and read
    the upright and upside-down fixtures."""
    with tempfile.TemporaryDirectory(prefix="tesseract-verify-") as scratch:
        with tarfile.open(path, "r:gz") as archive:
            # read_archive already admitted only plain files under ocr/.
            options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            archive.extractall(scratch, **options)
        root = Path(scratch) / "ocr"
        binary = root / "bin/tesseract"
        result = subprocess.run([str(binary), "--version"], capture_output=True, timeout=60, env={"PATH": "/usr/bin:/bin"})
        report = (result.stdout + result.stderr).decode(errors="replace")
        leptonica = lock["sources"]["leptonica"]["version"]
        require(result.returncode == 0 and ("tesseract " + lock["upstream_version"]) in report
                and ("leptonica-" + leptonica) in report, "Engine version report differs: " + report.strip())
        read = {}
        for name in ("upright.pgm", "upside-down.pgm"):
            text = recognize(binary, root / "tessdata", FIXTURES / name)
            missing = [word for word in FIXTURE_WORDS if word not in text]
            require(not missing, "Engine did not read " + ", ".join(missing) + " from " + name + ": " + text.strip())
            read[name] = " ".join(text.split())
        return {"version_report": report.strip().splitlines()[:2], "fixtures": read}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("check", help="verify the inputs without building or using the network")
    build_cmd = commands.add_parser("build", help="build one platform's archive with Docker")
    build_cmd.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    build_cmd.add_argument("--output", required=True, type=Path)
    verify_cmd = commands.add_parser("verify", help="verify an archive and run it when this host can")
    verify_cmd.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    verify_cmd.add_argument("--archive", required=True, type=Path)
    verify_cmd.add_argument("--require-run", action="store_true", help="fail unless the engine ran on this host")
    verify_cmd.add_argument("--output", type=Path, help="write the verification facts as JSON")
    args = parser.parse_args()
    try:
        if args.operation == "check":
            result = check_inputs()
        elif args.operation == "build":
            result = {"archive": str(build(args.platform, args.output))}
        else:
            result = verify_archive(args.archive, args.platform)
            require(result["executed"] or not args.require_run, "The engine did not run on this host")
            if args.output:
                with args.output.open("x") as stream:
                    stream.write(json.dumps(result, sort_keys=True, indent=2) + "\n")
    except EngineError as error:
        raise SystemExit("error: " + str(error))
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

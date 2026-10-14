import copy
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from tests import helpers
from tests.helpers import ENGINE


class InputsTest(unittest.TestCase):
    def test_checked_in_inputs_pass(self):
        result = ENGINE.check_inputs()
        self.assertEqual(result["version"], "5.5.3+paintedwolf.1")

    def lock(self):
        return copy.deepcopy(ENGINE.load_lock())

    def test_lock_refuses_unpinned_build_image(self):
        lock = self.lock()
        lock["build_image"] = "alpine:3.22"
        with self.assertRaisesRegex(ENGINE.EngineError, "pinned by digest"):
            ENGINE.check_lock(lock)

    def test_lock_refuses_a_tesseract_version_other_than_upstream(self):
        lock = self.lock()
        lock["upstream_version"] = "5.5.4"
        with self.assertRaisesRegex(ENGINE.EngineError, "differs from upstream_version"):
            ENGINE.check_lock(lock)

    def test_lock_refuses_missing_and_extra_sources(self):
        lock = self.lock()
        del lock["sources"]["musl"]
        with self.assertRaisesRegex(ENGINE.EngineError, "sources must be exactly"):
            ENGINE.check_lock(lock)

    def test_lock_refuses_a_short_trained_data_digest(self):
        lock = self.lock()
        lock["sources"]["tessdata_fast"]["files"]["eng.traineddata"]["sha256"] = "abc"
        with self.assertRaisesRegex(ENGINE.EngineError, "eng.traineddata needs a digest"):
            ENGINE.check_lock(lock)

    def test_lock_refuses_an_unversioned_source_url(self):
        lock = self.lock()
        lock["sources"]["leptonica"]["url"] = "https://example.invalid/leptonica.tar.gz"
        with self.assertRaisesRegex(ENGINE.EngineError, "name its version"):
            ENGINE.check_lock(lock)

    def inventory_case(self):
        return (self.lock(), ENGINE.read_json(ENGINE.INVENTORY_PATH), ENGINE.NOTICES_PATH.read_text())

    def test_inventory_follows_the_lock_version(self):
        lock, inventory, notices = self.inventory_case()
        lock["revision"] = 2
        with self.assertRaisesRegex(ENGINE.EngineError, "artifact identity differs"):
            ENGINE.check_inventory(lock, inventory, notices)

    def test_inventory_pins_locked_license_texts(self):
        lock, inventory, notices = self.inventory_case()
        inventory["notices"]["licenses/GPL-3.0"] = "0" * 64
        with self.assertRaisesRegex(ENGINE.EngineError, "GPL-3.0 digest differs"):
            ENGINE.check_inventory(lock, inventory, notices)

    def test_inventory_component_versions_follow_the_lock(self):
        lock, inventory, notices = self.inventory_case()
        lock["sources"]["leptonica"]["version"] = "1.88.0"
        with self.assertRaisesRegex(ENGINE.EngineError, "leptonica version differs"):
            ENGINE.check_inventory(lock, inventory, notices)

    def test_notices_need_every_component_section(self):
        lock, inventory, notices = self.inventory_case()
        notices = notices.replace("## musl 1.2.5\n", "## musl\n")
        with self.assertRaisesRegex(ENGINE.EngineError, "missing section ## musl 1.2.5"):
            ENGINE.check_inventory(lock, inventory, notices)

    def test_notices_state_each_license(self):
        lock, inventory, notices = self.inventory_case()
        notices = notices.replace("MIT. Source: https://musl.libc.org/", "Source: https://musl.libc.org/")
        with self.assertRaisesRegex(ENGINE.EngineError, "musl must state its license"):
            ENGINE.check_inventory(lock, inventory, notices)

    def test_build_command_passes_every_locked_input(self):
        lock = self.lock()
        argv = ENGINE.build_command(lock, "f" * 64, "linux-arm64", Path("/out"))
        self.assertEqual(argv[:5], ["docker", "buildx", "build", "--platform", "linux/arm64"])
        passed = dict(argv[i + 1].split("=", 1) for i, value in enumerate(argv) if value == "--build-arg")
        self.assertEqual(passed["BASE_IMAGE"], lock["build_image"])
        self.assertEqual(passed["ENGINE_VERSION"], "5.5.3+paintedwolf.1")
        self.assertEqual(passed["SOURCE_LOCK_SHA256"], "f" * 64)
        self.assertEqual(passed["OSD_SHA256"], lock["sources"]["tessdata_fast"]["files"]["osd.traineddata"]["sha256"])
        dockerfile = (ENGINE.ENGINE / "Dockerfile").read_text()
        for key in passed:
            self.assertIn("ARG " + key, dockerfile)


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.directory = Path(self.scratch.name)
        self.inputs = helpers.inputs()

    def tearDown(self):
        self.scratch.cleanup()

    def verify(self, path, platform="linux-amd64"):
        return ENGINE.verify_archive(path, platform, self.inputs, run=False)

    def test_valid_archive_reports_its_identity(self):
        path = helpers.write_archive(self.directory, "linux-arm64", self.inputs)
        facts = self.verify(path, "linux-arm64")
        self.assertEqual(facts["archive"], {"name": path.name, "sha256": ENGINE.sha256_file(path), "bytes": path.stat().st_size})
        binary = helpers.payload("linux-arm64", self.inputs)["ocr/bin/tesseract"]
        self.assertEqual(facts["binary"], {"sha256": helpers.sha(binary), "bytes": len(binary)})
        self.assertFalse(facts["executed"])

    def test_refuses_an_archive_named_for_another_platform(self):
        path = helpers.write_archive(self.directory, "linux-arm64", self.inputs)
        with self.assertRaisesRegex(ENGINE.EngineError, "Archive name differs"):
            self.verify(path, "linux-amd64")

    def test_refuses_an_executable_for_another_machine(self):
        files = helpers.payload("linux-amd64", self.inputs, binary=helpers.elf(183))
        path = helpers.write_archive(self.directory, "linux-amd64", self.inputs, files)
        with self.assertRaisesRegex(ENGINE.EngineError, "another machine"):
            self.verify(path)

    def test_refuses_a_dynamically_linked_executable(self):
        files = helpers.payload("linux-amd64", self.inputs, binary=helpers.elf(62, interpreter=True))
        path = helpers.write_archive(self.directory, "linux-amd64", self.inputs, files)
        with self.assertRaisesRegex(ENGINE.EngineError, "dynamically linked"):
            self.verify(path)

    def test_refuses_altered_trained_data(self):
        files = helpers.payload("linux-amd64", self.inputs)
        files["ocr/tessdata/osd.traineddata"] += b"x"
        path = helpers.write_archive(self.directory, "linux-amd64", self.inputs, files)
        with self.assertRaisesRegex(ENGINE.EngineError, "osd.traineddata differs"):
            self.verify(path)

    def test_refuses_an_altered_license_text(self):
        files = helpers.payload("linux-amd64", self.inputs)
        files["ocr/licenses/COPYRIGHT-musl"] = b"other"
        path = helpers.write_archive(self.directory, "linux-amd64", self.inputs, files)
        with self.assertRaisesRegex(ENGINE.EngineError, "COPYRIGHT-musl differs from the license inventory"):
            self.verify(path)

    def test_refuses_provenance_that_misstates_the_binary(self):
        files = helpers.payload("linux-amd64", self.inputs)
        provenance = json.loads(files["ocr/provenance.json"])
        provenance["binary"]["bytes"] += 1
        files["ocr/provenance.json"] = json.dumps(provenance).encode()
        path = helpers.write_archive(self.directory, "linux-amd64", self.inputs, files)
        with self.assertRaisesRegex(ENGINE.EngineError, "provenance differs: binary"):
            self.verify(path)

    def test_refuses_a_different_source_lock(self):
        files = helpers.payload("linux-amd64", self.inputs)
        files["ocr/source-lock.json"] += b"\n"
        path = helpers.write_archive(self.directory, "linux-amd64", self.inputs, files)
        with self.assertRaisesRegex(ENGINE.EngineError, "source lock differs"):
            self.verify(path)

    def test_refuses_missing_and_extra_members(self):
        files = helpers.payload("linux-amd64", self.inputs)
        del files["ocr/NOTICES-tesseract.md"]
        files["ocr/extra"] = b"x"
        path = helpers.write_archive(self.directory, "linux-amd64", self.inputs, files)
        with self.assertRaisesRegex(ENGINE.EngineError, "unexpected ocr/extra; missing ocr/NOTICES-tesseract.md"):
            self.verify(path)

    def test_refuses_links_and_escaping_members(self):
        for info in (tarfile.TarInfo("ocr/bin/link"), tarfile.TarInfo("../outside")):
            with self.subTest(member=info.name):
                if info.name.endswith("link"):
                    info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                path = helpers.write_archive(self.directory, "linux-amd64", self.inputs, extra=[info])
                with self.assertRaises(ENGINE.EngineError):
                    self.verify(path)
                path.unlink()

    def test_runs_the_engine_only_on_its_own_platform(self):
        with mock.patch.object(ENGINE.host, "system", return_value="Linux"), \
                mock.patch.object(ENGINE.host, "machine", return_value="aarch64"):
            self.assertTrue(ENGINE.runs_here("linux-arm64"))
            self.assertFalse(ENGINE.runs_here("linux-amd64"))
        with mock.patch.object(ENGINE.host, "system", return_value="Darwin"), \
                mock.patch.object(ENGINE.host, "machine", return_value="arm64"):
            self.assertFalse(ENGINE.runs_here("linux-arm64"))

    def test_functional_check_requires_the_fixture_words(self):
        path = helpers.write_archive(self.directory, "linux-amd64", self.inputs)
        version = mock.Mock(returncode=0, stdout=b"tesseract 5.5.3\n leptonica-1.87.0\n", stderr=b"")
        with mock.patch.object(ENGINE.subprocess, "run", return_value=version), \
                mock.patch.object(ENGINE, "recognize", return_value="Painted Wolf Code\nrotated capture of a key\n"):
            report = ENGINE.functional_check(path, self.inputs["lock"])
        self.assertEqual(report["fixtures"]["upside-down.pgm"], "Painted Wolf Code rotated capture of a key")
        with mock.patch.object(ENGINE.subprocess, "run", return_value=version), \
                mock.patch.object(ENGINE, "recognize", return_value="9POD JOM pejuled"):
            with self.assertRaisesRegex(ENGINE.EngineError, "did not read Painted"):
                ENGINE.functional_check(path, self.inputs["lock"])

    def test_functional_check_requires_the_locked_versions(self):
        path = helpers.write_archive(self.directory, "linux-amd64", self.inputs)
        version = mock.Mock(returncode=0, stdout=b"tesseract 5.4.1\n leptonica-1.87.0\n", stderr=b"")
        with mock.patch.object(ENGINE.subprocess, "run", return_value=version):
            with self.assertRaisesRegex(ENGINE.EngineError, "version report differs"):
                ENGINE.functional_check(path, self.inputs["lock"])


if __name__ == "__main__":
    unittest.main()

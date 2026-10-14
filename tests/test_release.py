import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import helpers
from tests.helpers import ENGINE

import release as RELEASE  # noqa: E402

SHA = "a" * 40
VERSION = "5.5.3+paintedwolf.1"


def environment(ref="refs/tags/v" + VERSION, **overrides):
    values = {"GITHUB_SHA": SHA, "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": RELEASE.REPOSITORY,
              "GITHUB_REPOSITORY_ID": RELEASE.REPOSITORY_ID, "GITHUB_REPOSITORY_OWNER_ID": RELEASE.OWNER_ID,
              "GITHUB_REF": ref, "GITHUB_WORKFLOW_REF": RELEASE.REPOSITORY + "/" + RELEASE.WORKFLOW + "@" + ref,
              "GITHUB_WORKFLOW_SHA": SHA, "GITHUB_EVENT_NAME": "workflow_dispatch",
              "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_RUN_ID": "17", "GITHUB_RUN_ATTEMPT": "1",
              "GITHUB_JOB": "build", "ImageOS": "ubuntu24", "ImageVersion": "20261012.1",
              "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64"}
    if ref.startswith("refs/tags/"):
        values.update({"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": ref.removeprefix("refs/tags/")})
    else:
        values.update({"GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": ref.removeprefix("refs/heads/")})
    values.update(overrides)
    return values


TOOLS = {"docker": "28.0.4", "buildx": "v0.22.0", "build_image": "alpine", "driver_python": "Python 3.13.7"}


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.lock = ENGINE.load_lock()

    def test_release_runs_only_at_the_version_tag(self):
        identity = RELEASE.workflow_identity(environment(), self.lock, release=True)
        self.assertEqual((identity["version"], identity["ref"]), (VERSION, "refs/tags/v" + VERSION))
        branch = environment("refs/heads/initial-producer")
        with self.assertRaisesRegex(ENGINE.EngineError, "GITHUB_REF_TYPE"):
            RELEASE.workflow_identity(branch, self.lock, release=True)

    def test_qualification_accepts_any_branch(self):
        identity = RELEASE.workflow_identity(environment("refs/heads/initial-producer"), self.lock, release=False)
        self.assertEqual(identity["ref"], "refs/heads/initial-producer")

    def test_qualification_runs_for_pull_requests_but_release_does_not(self):
        pull = environment("refs/pull/3/merge", GITHUB_EVENT_NAME="pull_request")
        self.assertEqual(RELEASE.workflow_identity(pull, self.lock, release=False)["ref"], "refs/pull/3/merge")
        tagged = environment(GITHUB_EVENT_NAME="pull_request")
        with self.assertRaisesRegex(ENGINE.EngineError, "GITHUB_EVENT_NAME"):
            RELEASE.workflow_identity(tagged, self.lock, release=True)

    def test_refuses_another_repository_or_a_self_hosted_runner(self):
        for key, value in (("GITHUB_REPOSITORY_ID", "1"), ("RUNNER_ENVIRONMENT", "self-hosted"),
                           ("GITHUB_EVENT_NAME", "push"), ("GITHUB_WORKFLOW_SHA", "b" * 40)):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ENGINE.EngineError, key):
                    RELEASE.workflow_identity(environment(**{key: value}), self.lock, release=False)


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.directory = Path(self.scratch.name)
        self.inputs = helpers.inputs()
        lock = self.inputs["lock"]
        self.identity = {**RELEASE.workflow_identity(environment(), lock, release=True),
                         "source_lock_sha256": helpers.sha(self.inputs["lock_bytes"])}
        patches = [mock.patch.object(ENGINE, "load_inputs", return_value=self.inputs),
                   mock.patch.object(ENGINE, "load_lock", return_value=lock)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.scratch.cleanup()

    def platform_output(self, platform):
        directory = self.directory / ("in-" + platform)
        directory.mkdir()
        archive = helpers.write_archive(directory, platform, self.inputs)
        facts = ENGINE.verify_archive(archive, platform, self.inputs, run=False)
        facts.update(executed=True, engine={"fixtures": {"upright.pgm": "Painted Wolf Code"}})
        arch = "ARM64" if platform.endswith("arm64") else "X64"
        record = RELEASE.build_evidence(facts, self.identity, environment(RUNNER_ARCH=arch), TOOLS)
        (directory / RELEASE.evidence_name(platform)).write_text(json.dumps(record))
        return directory

    def test_build_evidence_requires_its_own_runner_and_a_run_engine(self):
        archive = helpers.write_archive(self.directory, "linux-arm64", self.inputs)
        facts = ENGINE.verify_archive(archive, "linux-arm64", self.inputs, run=False)
        with self.assertRaisesRegex(ENGINE.EngineError, "hosted runner"):
            RELEASE.build_evidence({**facts, "executed": True, "engine": {}}, self.identity, environment(), TOOLS)
        with self.assertRaisesRegex(ENGINE.EngineError, "engine to have run"):
            RELEASE.build_evidence(facts, self.identity, environment(RUNNER_ARCH="ARM64"), TOOLS)

    def test_assembles_descriptor_and_checksums_from_both_platforms(self):
        inputs = [self.platform_output(platform) for platform in sorted(ENGINE.PLATFORMS)]
        output = RELEASE.assemble(inputs, self.directory / "release", self.identity)
        descriptor = json.loads((output / "release.json").read_text())["tesseract"]
        self.assertEqual(descriptor["source_commit"], SHA)
        self.assertEqual([item["platform"] for item in descriptor["artifacts"]], ["linux-amd64", "linux-arm64"])
        for item in descriptor["artifacts"]:
            archive = output / Path(item["url"]).name
            self.assertEqual(item["url"], "https://github.com/" + RELEASE.REPOSITORY + "/releases/download/v"
                             + VERSION + "/" + archive.name)
            self.assertEqual((item["sha256"], item["bytes"]), (ENGINE.sha256_file(archive), archive.stat().st_size))
            self.assertIn(item["sha256"] + "  " + archive.name, (output / "SHA256SUMS").read_text())
        (output / "provenance.sigstore.json").write_text("{}")
        names = [path.name for path in RELEASE.release_files(output, self.identity)]
        self.assertEqual(names[-1], "provenance.sigstore.json")

    def test_assembly_refuses_an_archive_that_differs_from_its_evidence(self):
        inputs = [self.platform_output(platform) for platform in sorted(ENGINE.PLATFORMS)]
        archive = next(inputs[1].glob("*.tar.gz"))
        with open(archive, "ab") as stream:
            stream.write(b"\0")
        with self.assertRaisesRegex(ENGINE.EngineError, "differs from its build evidence"):
            RELEASE.assemble(inputs, self.directory / "release", self.identity)

    def test_release_files_refuse_extra_assets(self):
        inputs = [self.platform_output(platform) for platform in sorted(ENGINE.PLATFORMS)]
        output = RELEASE.assemble(inputs, self.directory / "release", self.identity)
        (output / "provenance.sigstore.json").write_text("{}")
        (output / "notes.txt").write_text("x")
        with self.assertRaisesRegex(ENGINE.EngineError, "exactly the archives"):
            RELEASE.release_files(output, self.identity)

    def test_uploaded_assets_must_match_exactly(self):
        path = self.directory / "SHA256SUMS"
        path.write_text("sums\n")
        good = {"draft": True, "tag_name": "v" + VERSION,
                "assets": [{"name": path.name, "state": "uploaded", "size": path.stat().st_size,
                            "digest": "sha256:" + ENGINE.sha256_file(path)}]}
        RELEASE.validate_uploaded_assets(good, [path], self.identity)
        good["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ENGINE.EngineError, "Uploaded asset differs"):
            RELEASE.validate_uploaded_assets(good, [path], self.identity)

    def test_attestation_must_name_this_run_and_subject(self):
        subject = self.directory / "release.json"
        subject.write_text("{}")
        uri = "https://github.com/" + self.identity["workflow_ref"]
        certificate = {"subjectAlternativeName": uri, "buildSignerURI": uri,
                       "issuer": "https://token.actions.githubusercontent.com", "buildSignerDigest": SHA,
                       "sourceRepositoryDigest": SHA, "sourceRepositoryRef": self.identity["ref"],
                       "sourceRepositoryURI": "https://github.com/" + RELEASE.REPOSITORY,
                       "sourceRepositoryIdentifier": RELEASE.REPOSITORY_ID,
                       "sourceRepositoryOwnerIdentifier": RELEASE.OWNER_ID, "runnerEnvironment": "github-hosted",
                       "buildTrigger": "workflow_dispatch",
                       "runInvocationURI": "https://github.com/" + RELEASE.REPOSITORY + "/actions/runs/17/attempts/1"}
        result = {"verificationResult": {"signature": {"certificate": certificate}, "verifiedTimestamps": [{}],
                                         "statement": {"predicateType": "https://slsa.dev/provenance/v1",
                                                       "subject": [{"digest": {"sha256": ENGINE.sha256_file(subject)}}]}}}
        RELEASE.check_attestation([result], self.identity, subject)
        certificate["runInvocationURI"] += "0"
        with self.assertRaisesRegex(ENGINE.EngineError, "No verified attestation"):
            RELEASE.check_attestation([result], self.identity, subject)


if __name__ == "__main__":
    unittest.main()

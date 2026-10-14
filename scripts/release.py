#!/usr/bin/env python3
"""Bind engine archives to the hosted workflow run, assemble a release, and
create a complete draft without overwriting a version."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import engine as ENGINE

REPOSITORY = "paintedwolf-ai/paintedwolf-tesseract"
REPOSITORY_ID = "1419109526"
OWNER_ID = "289211278"
WORKFLOW = ".github/workflows/release.yml"
BUILD_JOB = "build"
# The hosted runner each platform builds and runs on.
RUNNERS = {"linux-amd64": ("Linux", "X64"), "linux-arm64": ("Linux", "ARM64")}
require = ENGINE.require


def evidence_name(platform):
    return "build-evidence-" + platform + ".json"


def command(argv, root=ENGINE.ROOT):
    return subprocess.check_output(argv, cwd=root, text=True).strip()


def workflow_identity(environment, lock, release):
    """The run's identity. A release is dispatched only at the version tag; a
    qualification may run on any branch or pull request of this repository."""
    version = ENGINE.engine_version(lock)
    sha = environment.get("GITHUB_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None, "Invalid workflow source commit")
    ref = environment.get("GITHUB_REF", "")
    expected = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_REPOSITORY_ID": REPOSITORY_ID, "GITHUB_REPOSITORY_OWNER_ID": OWNER_ID,
                "GITHUB_WORKFLOW_REF": REPOSITORY + "/" + WORKFLOW + "@" + ref,
                "GITHUB_WORKFLOW_SHA": sha, "RUNNER_ENVIRONMENT": "github-hosted"}
    events = ("workflow_dispatch",) if release else ("workflow_dispatch", "pull_request")
    require(environment.get("GITHUB_EVENT_NAME") in events, "Unexpected workflow context: GITHUB_EVENT_NAME")
    if release:
        tag = "v" + version
        expected.update({"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": tag, "GITHUB_REF": "refs/tags/" + tag})
    for key, value in expected.items():
        require(environment.get(key) == value, "Unexpected workflow context: " + key)
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        require(re.fullmatch(r"[1-9][0-9]*", environment.get(key, "")) is not None, "Invalid run identity: " + key)
    return {"repository": REPOSITORY, "repository_id": REPOSITORY_ID, "owner_id": OWNER_ID, "commit": sha,
            "ref": ref, "version": version, "workflow_ref": expected["GITHUB_WORKFLOW_REF"], "workflow_sha": sha,
            "run_id": environment["GITHUB_RUN_ID"], "run_attempt": environment["GITHUB_RUN_ATTEMPT"]}


def checked_identity(release, root=ENGINE.ROOT, environment=None):
    environment = os.environ if environment is None else environment
    lock_path = root / "engine/source-lock.json"
    identity = workflow_identity(environment, ENGINE.check_lock(ENGINE.read_json(lock_path)), release)
    require(command(["git", "rev-parse", "HEAD"], root) == identity["commit"], "Checkout differs from workflow source")
    require(not command(["git", "status", "--porcelain", "--untracked-files=no"], root),
            "Checkout has modified tracked inputs")
    if release:
        require(command(["git", "rev-parse", identity["ref"] + "^{commit}"], root) == identity["commit"],
                "Release tag differs from workflow source")
        subprocess.run(["git", "merge-base", "--is-ancestor", identity["commit"], "refs/remotes/origin/main"],
                       cwd=root, check=True)
    return {**identity, "source_lock_sha256": ENGINE.sha256_file(lock_path)}


def require_unpublished(identity):
    pages = json.loads(command(["gh", "api", "--paginate", "--slurp", "repos/" + REPOSITORY + "/releases?per_page=100"]))
    tag = "v" + identity["version"]
    require(not any(release["tag_name"] == tag for page in pages for release in page),
            "This version already has a release or draft; existing assets will not be replaced")


def build_evidence(facts, identity, environment, tools):
    platform = facts["platform"]
    require(environment.get("GITHUB_JOB") == BUILD_JOB, "Build evidence must originate in the build job")
    for key in ("ImageOS", "ImageVersion", "RUNNER_OS", "RUNNER_ARCH"):
        require(bool(environment.get(key)), "Missing hosted image identity: " + key)
    require((environment["RUNNER_OS"], environment["RUNNER_ARCH"]) == RUNNERS[platform],
            "Build runner differs from the " + platform + " hosted runner")
    require(facts.get("executed") is True, "Build evidence requires the engine to have run on its own runner")
    require(tools and all(isinstance(value, str) and value.strip() for value in tools.values()),
            "Build tool versions are incomplete")
    return {"schema_version": 1, "build": identity, "job": BUILD_JOB, "platform": platform, "reused_artifact": False,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "runner": {key: environment[key] for key in ("ImageOS", "ImageVersion", "RUNNER_OS", "RUNNER_ARCH")},
            "tools": tools, "archive": facts["archive"], "binary": facts["binary"],
            "packages": facts["packages"], "engine": facts["engine"]}


def check_evidence(evidence, identity, platform, archive=None):
    require(evidence.get("schema_version") == 1 and evidence.get("build") == identity,
            "Build evidence differs from this source and workflow run")
    require(evidence.get("job") == BUILD_JOB and evidence.get("platform") == platform
            and evidence.get("reused_artifact") is False, "Release requires a fresh build")
    runner = evidence.get("runner", {})
    require((runner.get("RUNNER_OS"), runner.get("RUNNER_ARCH")) == RUNNERS[platform],
            "Build runner differs from the " + platform + " hosted runner")
    tools = evidence.get("tools")
    require(all(isinstance(runner.get(key), str) and runner[key] for key in ("ImageOS", "ImageVersion"))
            and isinstance(tools, dict) and tools and all(isinstance(value, str) and value.strip() for value in tools.values()),
            "Build environment evidence is incomplete")
    recorded = evidence.get("archive", {})
    require(recorded.get("name") == ENGINE.archive_name(identity["version"], platform)
            and ENGINE.HEX64.fullmatch(str(recorded.get("sha256"))) and type(recorded.get("bytes")) is int,
            "Build evidence archive identity is incomplete")
    if archive is not None:
        require(recorded == {"name": archive.name, "sha256": ENGINE.sha256_file(archive), "bytes": archive.stat().st_size},
                "Archive differs from its build evidence")


def tool_versions(lock):
    return {"docker": command(["docker", "version", "--format", "{{.Server.Version}}"]),
            "buildx": command(["docker", "buildx", "version"]),
            "build_image": lock["build_image"],
            "driver_python": command(["python3", "--version"])}


def release_url(version, name):
    return "https://github.com/" + REPOSITORY + "/releases/download/v" + version + "/" + name


def descriptor(identity, lock, evidence):
    artifacts = []
    for platform in sorted(ENGINE.PLATFORMS):
        record = evidence[platform]
        spec = ENGINE.PLATFORMS[platform]
        artifacts.append({"platform": platform, "goos": spec["goos"], "goarch": spec["goarch"],
                          "url": release_url(identity["version"], record["archive"]["name"]),
                          "sha256": record["archive"]["sha256"], "bytes": record["archive"]["bytes"],
                          "binary_sha256": record["binary"]["sha256"], "binary_bytes": record["binary"]["bytes"]})
    return {"tesseract": {"version": identity["version"], "upstream_version": lock["upstream_version"],
                          "revision": lock["revision"], "source_commit": identity["commit"],
                          "source_lock_sha256": identity["source_lock_sha256"], "license": "Apache-2.0",
                          "trained_data": lock["sources"]["tessdata_fast"]["files"], "artifacts": artifacts}}


def assemble(inputs, output, identity):
    """Combine verified per-platform outputs into one release directory."""
    lock = ENGINE.load_lock()
    require(not output.exists(), "Release output must be a new directory")
    evidence, archives = {}, {}
    for platform in sorted(ENGINE.PLATFORMS):
        name = ENGINE.archive_name(identity["version"], platform)
        found = [directory / name for directory in inputs if (directory / name).is_file()]
        require(len(found) == 1, "Expected one " + name + " among the inputs")
        archive = found[0]
        record = ENGINE.read_json(archive.parent / evidence_name(platform))
        check_evidence(record, identity, platform, archive)
        ENGINE.verify_archive(archive, platform, run=False)
        evidence[platform], archives[platform] = record, archive
    output.mkdir(parents=True)
    sums = []
    for platform in sorted(archives):
        archive = archives[platform]
        (output / archive.name).write_bytes(archive.read_bytes())
        (output / evidence_name(platform)).write_text(json.dumps(evidence[platform], sort_keys=True, indent=2) + "\n")
        sums.append(evidence[platform]["archive"]["sha256"] + "  " + archive.name)
    (output / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    (output / "release.json").write_text(json.dumps(descriptor(identity, lock, evidence), sort_keys=True, indent=2) + "\n")
    return output


def check_attestation(results, identity, subject):
    uri = "https://github.com/" + identity["workflow_ref"]
    expected = {"subjectAlternativeName": uri, "buildSignerURI": uri,
                "issuer": "https://token.actions.githubusercontent.com",
                "buildSignerDigest": identity["commit"], "sourceRepositoryDigest": identity["commit"],
                "sourceRepositoryRef": identity["ref"], "sourceRepositoryURI": "https://github.com/" + REPOSITORY,
                "sourceRepositoryIdentifier": REPOSITORY_ID, "sourceRepositoryOwnerIdentifier": OWNER_ID,
                "runnerEnvironment": "github-hosted", "buildTrigger": "workflow_dispatch",
                "runInvocationURI": "https://github.com/" + REPOSITORY + "/actions/runs/"
                + identity["run_id"] + "/attempts/" + identity["run_attempt"]}
    for result in results:
        verified = result.get("verificationResult", {})
        certificate = verified.get("signature", {}).get("certificate", {})
        statement = verified.get("statement", {})
        if (all(certificate.get(key) == value for key, value in expected.items())
                and verified.get("verifiedTimestamps")
                and statement.get("predicateType") == "https://slsa.dev/provenance/v1"
                and any(item.get("digest", {}).get("sha256") == ENGINE.sha256_file(subject)
                        for item in statement.get("subject", []))):
            return
    raise ENGINE.EngineError("No verified attestation matches the exact hosted release identity")


def verify_attestation(subject, bundle, identity):
    output = command(["gh", "attestation", "verify", str(subject), "--bundle", str(bundle), "--repo", REPOSITORY,
                      "--signer-workflow", REPOSITORY + "/" + WORKFLOW, "--source-ref", identity["ref"],
                      "--source-digest", identity["commit"], "--signer-digest", identity["commit"],
                      "--deny-self-hosted-runners", "--format", "json"])
    check_attestation(json.loads(output), identity, subject)


def release_files(directory, identity):
    lock = ENGINE.load_lock()
    record = ENGINE.read_json(directory / "release.json")["tesseract"]
    for key, expected in (("version", identity["version"]), ("source_commit", identity["commit"]),
                          ("source_lock_sha256", identity["source_lock_sha256"]),
                          ("upstream_version", lock["upstream_version"]), ("revision", lock["revision"])):
        require(record.get(key) == expected, "Release descriptor identity differs: " + key)
    archives, evidence = [], []
    for platform in sorted(ENGINE.PLATFORMS):
        archive = directory / ENGINE.archive_name(identity["version"], platform)
        check_evidence(ENGINE.read_json(directory / evidence_name(platform)), identity, platform, archive)
        archives.append(archive)
        evidence.append(directory / evidence_name(platform))
    expected = descriptor(identity, lock, {path.name[len("build-evidence-"):-len(".json")]: ENGINE.read_json(path)
                                           for path in evidence})
    require(ENGINE.read_json(directory / "release.json") == expected, "Release descriptor differs from build evidence")
    files = [*archives, directory / "SHA256SUMS", directory / "release.json", *evidence,
             directory / "provenance.sigstore.json"]
    require({path.name for path in directory.iterdir()} == {path.name for path in files},
            "Release directory must hold exactly the archives, checksums, descriptor, build evidence, and attestation")
    for path in files:
        ENGINE.sha256_file(path)
    return files


def validate_uploaded_assets(release, files, identity):
    require(release.get("draft") is True and release.get("tag_name") == "v" + identity["version"],
            "Uploaded release is not the expected draft")
    assets = release.get("assets", [])
    expected = {path.name: (path.stat().st_size, "sha256:" + ENGINE.sha256_file(path)) for path in files}
    require(len(assets) == len(expected) and {asset["name"] for asset in assets} == set(expected),
            "Draft release does not contain the complete exact asset set")
    for asset in assets:
        require(asset.get("state") == "uploaded" and (asset.get("size"), asset.get("digest")) == expected[asset["name"]],
                "Uploaded asset differs: " + asset["name"])


def draft(directory, identity):
    files = release_files(directory, identity)
    for subject in files[:-1]:
        verify_attestation(subject, files[-1], identity)
    require_unpublished(identity)
    tag = "v" + identity["version"]
    with tempfile.TemporaryDirectory(prefix="tesseract-draft-") as scratch:
        notes = Path(scratch) / "notes.md"
        notes.write_text("Static Tesseract engines for Linux amd64 and arm64.\n\nSource commit: " + identity["commit"]
                         + "\nBuild run: https://github.com/" + REPOSITORY + "/actions/runs/" + identity["run_id"]
                         + "/attempts/" + identity["run_attempt"]
                         + "\n\nArchives, checksums, descriptor, and build evidence carry GitHub-hosted provenance"
                         + " attestations. Confirm that immutable releases are enabled before publishing this draft.\n")
        subprocess.run(["gh", "release", "create", tag, "--repo", REPOSITORY, "--verify-tag", "--draft",
                        "--target", identity["commit"], "--title", tag, "--notes-file", str(notes),
                        *[str(path) for path in files]], check=True)
    pages = json.loads(command(["gh", "api", "--paginate", "--slurp", "repos/" + REPOSITORY + "/releases?per_page=100"]))
    matches = [release for page in pages for release in page if release["tag_name"] == tag]
    require(len(matches) == 1, "Cannot locate the unique uploaded draft")
    validate_uploaded_assets(matches[0], files, identity)
    return matches[0]["html_url"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("qualify", "release"), required=True,
                        help="qualify accepts any branch; release requires the unpublished version tag")
    commands = parser.add_subparsers(dest="step", required=True)
    commands.add_parser("preflight")
    evidence_cmd = commands.add_parser("build-evidence")
    evidence_cmd.add_argument("--facts", required=True, type=Path)
    evidence_cmd.add_argument("--output", required=True, type=Path)
    assemble_cmd = commands.add_parser("assemble")
    assemble_cmd.add_argument("--output", required=True, type=Path)
    assemble_cmd.add_argument("inputs", nargs="+", type=Path)
    draft_cmd = commands.add_parser("draft")
    draft_cmd.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        identity = checked_identity(args.operation == "release")
        if args.step == "preflight":
            if args.operation == "release":
                require_unpublished(identity)
            result = identity
        elif args.step == "build-evidence":
            facts = ENGINE.read_json(args.facts)
            record = build_evidence(facts, identity, os.environ, tool_versions(ENGINE.load_lock()))
            with args.output.open("x") as stream:
                stream.write(json.dumps(record, sort_keys=True, indent=2) + "\n")
            result = {"evidence": str(args.output)}
        elif args.step == "assemble":
            result = {"release": str(assemble([path.resolve() for path in args.inputs], args.output.resolve(), identity))}
        else:
            require(args.operation == "release", "Only a release creates a draft")
            result = {"draft": draft(args.directory.resolve(), identity)}
    except ENGINE.EngineError as error:
        raise SystemExit("error: " + str(error))
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

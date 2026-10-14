# Painted Wolf Tesseract

Static builds of the [Tesseract](https://github.com/tesseract-ocr/tesseract) OCR
engine for Linux amd64 and arm64, released for Painted Wolf Code. This repository
owns the build recipe, its pinned inputs, the license inventory and notices, and
the release workflow. It builds independently of the Painted Wolf Code
repository, which pins a published release and verifies it before use.

## What a release contains

Each platform archive, `paintedwolf-tesseract-<version>-linux-<arch>.tar.gz`,
holds one `ocr/` tree:

| Path | Content |
|---|---|
| `bin/tesseract` | Tesseract with Leptonica, linked statically against musl and the GCC runtime libraries |
| `tessdata/eng.traineddata`, `tessdata/osd.traineddata` | English and orientation models from `tessdata_fast` |
| `licenses/` | The license text of every component |
| `NOTICES-tesseract.md` | Third-party notices |
| `source-lock.json` | The exact inputs the archive was built from |
| `provenance.json` | Versions, source digests, the Alpine package set, and the executable digest |
| `VERSION` | The release version |

Leptonica is built without any image codec, so the engine reads only PNM
rasters. Its consumer decodes images itself and hands the engine an 8-bit PGM
on stdin, so untrusted image bytes never reach a C parser.

The version is `<tesseract version>+paintedwolf.<revision>`, taken from
[`engine/source-lock.json`](engine/source-lock.json). The release tag is `v`
followed by the version.

## Build and test

Install Go 1.26.6 or newer (to bootstrap the pinned Task runner), Python 3.9 or
newer, and Docker with Buildx. Building the other architecture locally needs QEMU
emulation registered with Docker.

```sh
./task check     # source lock, license inventory, notices, fixtures; no network
./task test      # tooling tests
./task build -- --platform linux-amd64 --output /absolute/path/to/new-directory
./task verify -- --platform linux-amd64 --archive /absolute/path/to/archive.tar.gz
```

`build` passes every input from the source lock to
[`engine/Dockerfile`](engine/Dockerfile), which builds inside the digest-pinned
Alpine image and checks every download against its pinned digest. The archive is
written with fixed ownership and timestamps. The Alpine toolchain packages are not
pinned by the base image digest; `provenance.json` records the exact package set,
so two builds on different days can differ in their compiled bytes.

`verify` checks an archive before anyone trusts it:
- The exact file set, with no links and no member outside `ocr/`.
- Every trained-data and license digest, against the lock and the inventory.
- The source lock and notices, byte for byte.
- The executable: the right ELF machine and no program interpreter.
- `provenance.json`, against the actual executable.

On a Linux host of the archive's own architecture it also extracts the archive,
checks the version report, and reads the upright and upside-down text fixtures in
[`tests/fixtures/`](tests/fixtures/).

## Changing an input

Edit `engine/source-lock.json`, raising `revision` for any change that alters the
archive, and update [`engine/licensing/`](engine/licensing/) to match: the
inventory's artifact version, component versions and license digests, and the
notices' headings. `./task check` refuses a lock, inventory, or notices that
disagree. Open a pull request: the `Release` workflow qualifies it on both
architectures.

## Qualification

The `Release` workflow's `qualify` operation builds both platforms on fresh
GitHub-hosted runners (`ubuntu-24.04` and `ubuntu-24.04-arm`), verifies each
archive and runs it on its own architecture, records build evidence, and
assembles the release files as a workflow artifact. It runs automatically on pull
requests that change the build, and can be dispatched on any branch:

```sh
gh workflow run release.yml --ref <branch> -f operation=qualify
```

It never creates a tag, a release, or an attestation.

## Release

1. Merge the reviewed source lock to `main`.
2. Tag that commit with the version and push only the tag:
   ```sh
   git tag "v$VERSION" <main-commit> && git push origin "v$VERSION"
   ```
3. Dispatch the release at the tag:
   ```sh
   gh workflow run release.yml --ref "v$VERSION" -f operation=release
   ```

The release operation runs the same builds and checks, after first refusing to
continue unless:
- The tag names the checked-out commit.
- That commit is reachable from `main`.
- No release or draft exists for the version.

The `release` job then attests the archives, `SHA256SUMS`, `release.json` and
both build evidence files with GitHub artifact attestations. It runs in the
protected `release` environment and is the only job with write permissions. It
verifies each attestation against this exact run and creates a complete draft
with exactly these assets:

- both archives
- `SHA256SUMS`
- `release.json`, the descriptor that consumers pin: version, source commit,
  source lock digest, trained-data digests, and each archive's URL, digest, size
  and executable digest
- `build-evidence-linux-amd64.json` and `build-evidence-linux-arm64.json`: runner
  image, tool versions, Alpine package set, and the engine's fixture reads
- `provenance.sigstore.json`, the attestation bundle

Review the draft and publish it. Immutable releases must be enabled before
publishing:

```sh
gh api repos/paintedwolf-ai/paintedwolf-tesseract/immutable-releases --jq '.enabled'
```

A failed run can leave a draft. The workflow refuses an existing release or
draft and never overwrites assets. Delete an unpublished failed draft before
retrying, and rerun all jobs. Published releases and their tags stay unchanged;
a fix ships as a new revision.

This is build provenance from GitHub-hosted runners, not a claim of
byte-for-byte reproducibility.

## GitHub controls

- **Environments:** in Settings → Environments, create `release` with **Required
  reviewers** set to the release operator. Disable administrator bypass and
  restrict it to tags matching `v*`.
- **Branch ruleset:** activate a `main` ruleset requiring pull requests and the
  `check` and `DCO-owned` statuses from GitHub Actions, and blocking deletion and force pushes.
- **Tag rulesets:** restrict updates and deletions of `v*` tags with no bypass,
  and restrict their creation to the release operator.
- **Releases:** keep immutable releases enabled.

## Licensing

The build tooling in this repository is licensed under the [Apache License
2.0](LICENSE). Each component in a release keeps its own license; the
inventory in [`engine/licensing/inventory.json`](engine/licensing/inventory.json)
and the [notices](engine/licensing/NOTICES-tesseract.md) list them:

| Component | License |
|---|---|
| Tesseract | Apache-2.0 |
| Leptonica | BSD-2-Clause |
| `tessdata_fast` models | Apache-2.0 |
| musl | MIT |
| GCC runtime libraries | GPL-3.0-or-later with the GCC Runtime Library Exception |

Every archive carries the license texts and notices beside the executable.

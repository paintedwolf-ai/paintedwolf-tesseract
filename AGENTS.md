# Painted Wolf Tesseract agent policy

Use `./task` from the repository root. Run `./task check` and `./task test`
before handing off any change; both are offline and fast. Engine builds are
expensive: prefer the `Release` workflow's `qualify` operation (it runs on pull
requests that change the build) over local builds, and coordinate before
starting one on a shared host.

`engine/source-lock.json` binds every build input by digest. Change an input
only together with a new `revision`, the license inventory, and the notices;
`./task check` enforces their agreement. Never relabel an existing archive or
edit a published release; a fix ships as a new revision.

Keep the repository independent of Painted Wolf Code. It owns the build recipe,
inputs, notices, and release artifacts; the consumer owns its pin and how it
runs the engine. Linux amd64 and arm64 are the released platforms; do not add
assets for platforms the workflow does not build and run.

Commit with a DCO sign-off (`git commit -s`) as
`Chris Beckman <chris@paintedwolf.ai>`. Work on a branch and open a pull request;
`main` requires the `check` status. Never create or push tags, dispatch a
release, or publish a draft unless the maintainer asks.

Never remove another agent's work, stash the shared tree, or stop a process you
did not start. Do not add compatibility shims for internal pre-v1 structures.
Use sentence case in prose and messages.

#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TASK_VERSION=v3.51.1
export GOTOOLCHAIN=go1.26.6
TASK_BIN="${ROOT}/.bin/task"
mkdir -p "${ROOT}/.bin"
if [[ ! -x "${TASK_BIN}" ]] || [[ "$("${TASK_BIN}" --version)" != "${TASK_VERSION#v}" ]]; then
  GOBIN="${ROOT}/.bin" go install "github.com/go-task/task/v3/cmd/task@${TASK_VERSION}"
fi
exec "${TASK_BIN}" -d "${ROOT}" "$@"

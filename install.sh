#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "$0")" && pwd)
python_bin=${PYTHON:-python3}
install_dir=${XDG_BIN_HOME:-"$HOME/.local/bin"}

"$python_bin" -m venv "$repo_dir/.venv"
"$repo_dir/.venv/bin/python" -m pip install --upgrade pip
"$repo_dir/.venv/bin/python" -m pip install --editable "$repo_dir"
mkdir -p "$install_dir"
ln -sfn "$repo_dir/.venv/bin/ctfdash" "$install_dir/ctfdash"

echo "Installed ctfdash at $install_dir/ctfdash"
if [[ ":${PATH}:" != *":${install_dir}:"* ]]; then
  echo "Add $install_dir to PATH to invoke ctfdash from any directory."
fi


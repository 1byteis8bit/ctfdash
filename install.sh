#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "$0")" && pwd)
python_bin=${PYTHON:-python3}
install_dir=${XDG_BIN_HOME:-"$HOME/.local/bin"}

register_path() {
  local shell_file=$1
  local escaped_dir path_line
  escaped_dir=${install_dir//\'/\'\\\'\'}
  path_line="export PATH='$escaped_dir':\"\$PATH\""
  mkdir -p "$(dirname "$shell_file")"
  touch "$shell_file"
  if ! grep -Fqx "$path_line" "$shell_file"; then
    printf '\n# Added by ctfdash install.sh\n%s\n' "$path_line" >>"$shell_file"
  fi
}

"$python_bin" -m venv "$repo_dir/.venv"
"$repo_dir/.venv/bin/python" -m pip install --upgrade pip
"$repo_dir/.venv/bin/python" -m pip install --editable "$repo_dir"
mkdir -p "$install_dir"
ln -sfn "$repo_dir/.venv/bin/ctfdash" "$install_dir/ctfdash"

# Persist the command path for future login and interactive shells. An
# executed child script cannot alter its parent shell, so the export command
# is also printed below when the current shell has not loaded it yet.
register_path "$HOME/.profile"
case "${SHELL:-}" in
  */bash) register_path "$HOME/.bashrc" ;;
  */zsh) register_path "$HOME/.zshrc" ;;
esac

echo "Installed ctfdash at $install_dir/ctfdash"
if [[ ":${PATH}:" != *":${install_dir}:"* ]]; then
  escaped_dir=${install_dir//\'/\'\\\'\'}
  echo "PATH was saved for future shells. Activate it in this shell with:"
  echo "  export PATH='$escaped_dir':\"\$PATH\""
fi


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

link_command() {
  local target_dir=$1
  local target=$target_dir/ctfdash
  mkdir -p "$target_dir"
  if [[ -e "$target" || -L "$target" ]]; then
    if [[ "$(readlink -f "$target" 2>/dev/null || true)" == "$(readlink -f "$command_source")" ]]; then
      immediate_target=$target
      return 0
    fi
    return 1
  fi
  ln -s "$command_source" "$target"
  immediate_target=$target
}

"$python_bin" -m venv "$repo_dir/.venv"
"$repo_dir/.venv/bin/python" -m pip install --upgrade pip
"$repo_dir/.venv/bin/python" -m pip install --editable "$repo_dir"
mkdir -p "$install_dir"
ln -sfn "$repo_dir/.venv/bin/ctfdash" "$install_dir/ctfdash"
command_source="$repo_dir/.venv/bin/ctfdash"
immediate_target=""

# Persist the command path for future login and interactive shells. An
# executed child script cannot alter its parent shell, so the export command
# is also printed below when the current shell has not loaded it yet.
register_path "$HOME/.profile"
case "${SHELL:-}" in
  */bash) register_path "$HOME/.bashrc" ;;
  */zsh) register_path "$HOME/.zshrc" ;;
esac

echo "Installed ctfdash at $install_dir/ctfdash"

# Make the command available to the shell that launched this script without
# requiring `source`, `export`, or `hash -r`. A child process cannot edit its
# parent's environment, so place a second link in an already-active PATH
# directory. Prefer a user-writable directory; use /usr/local/bin via sudo only
# when no such directory exists.
if [[ ":${PATH}:" == *":${install_dir}:"* ]]; then
  immediate_target="$install_dir/ctfdash"
else
  IFS=: read -r -a path_entries <<<"${PATH}"
  for path_entry in "${path_entries[@]}"; do
    [[ -n "$path_entry" && "$path_entry" == "$HOME/"* && -d "$path_entry" && -w "$path_entry" ]] || continue
    if link_command "$path_entry"; then
      break
    fi
  done
fi

if [[ -z "$immediate_target" && ":${PATH}:" == *":/usr/local/bin:"* ]]; then
  system_target=/usr/local/bin/ctfdash
  if [[ -e "$system_target" || -L "$system_target" ]]; then
    if [[ "$(readlink -f "$system_target" 2>/dev/null || true)" == "$(readlink -f "$command_source")" ]]; then
      immediate_target=$system_target
    else
      echo "Refusing to replace unrelated $system_target" >&2
      exit 1
    fi
  elif [[ -w /usr/local/bin ]]; then
    ln -s "$command_source" "$system_target"
    immediate_target=$system_target
  elif command -v sudo >/dev/null 2>&1; then
    echo "No user-writable directory is active in PATH; installing the command in /usr/local/bin."
    sudo ln -s "$command_source" "$system_target"
    immediate_target=$system_target
  fi
fi

if [[ -z "$immediate_target" ]]; then
  echo "Could not install ctfdash into an active PATH directory." >&2
  echo "Grant access to /usr/local/bin or add $install_dir to PATH, then rerun the installer." >&2
  exit 1
fi

hash -r 2>/dev/null || true
echo "Ready now: $immediate_target"
"$immediate_target" --help >/dev/null


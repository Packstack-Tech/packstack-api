# Environment for running the API tests locally (bash or zsh):
#
#     source testenv.sh && python -m pytest tests/ -q
#
# The tests hit a real Postgres (the unique indexes are partial/functional
# and don't exist on SQLite). This exports the dev DB coordinates from
# .local.env -- the app has no dotenv loader; docker compose normally
# injects them -- and puts app/ on the path so `import main` resolves.
#
# .local.env is read the way docker compose reads it (KEY=VALUE per line,
# no shell quoting), NOT sourced: some values contain spaces.
#
# Make sure every migration in migrations/ has been applied to that DB
# first; a missing column fails at the first insert, not at import.
#
# Tests create rows under throwaway users (random emails) and never clean
# up. To keep them out of the dev DB, create an empty database and point
# POSTGRES_DB at it after sourcing -- the app runs create_all on startup:
#
#     POSTGRES_DB=packstack_test python -m pytest tests/ -q

# Resolve this file's directory under bash or zsh.
if [ -n "${BASH_SOURCE:-}" ]; then
  _testenv_self="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
  _testenv_self="${(%):-%x}"
else
  _testenv_self="$0"
fi
_api_dir="$(cd "$(dirname "$_testenv_self")" && pwd)"

if [ -f "$_api_dir/.local.env" ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    _line="${_line%$'\r'}"                       # tolerate CRLF
    case "$_line" in
      ''|'#'*) continue ;;                        # blank / comment
      *=*) ;;
      *) continue ;;                              # not KEY=VALUE
    esac
    _key="${_line%%=*}"
    _val="${_line#*=}"
    case "$_key" in
      [A-Za-z_]*) ;;
      *) continue ;;
    esac
    # Strip one layer of matching quotes, as compose does.
    case "$_val" in
      \"*\") _val="${_val#\"}"; _val="${_val%\"}" ;;
      \'*\') _val="${_val#\'}"; _val="${_val%\'}" ;;
    esac
    export "$_key=$_val"
  done < "$_api_dir/.local.env"
else
  echo "testenv.sh: $_api_dir/.local.env not found" >&2
fi

export PYTHONPATH="$_api_dir/app:$_api_dir${PYTHONPATH:+:$PYTHONPATH}"
export DEVELOPMENT=1
export JWT_SECRET="${JWT_SECRET:-test-secret}"
export JWT_ALGORITHM="${JWT_ALGORITHM:-HS256}"
export APP_HOST="${APP_HOST:-http://testserver}"

unset _testenv_self _api_dir _line _key _val

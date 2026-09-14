#!/usr/bin/env bash
#
# Verify the restricted Sullivan sync account from the side that actually uses
# it. Run ON FREDDY, inside the worker container, where the private key is
# mounted:
#
#     docker exec shelfmark-worker verify-sullivan-sync
#
# Four properties are asserted. Note the shape of them: ONE must succeed and
# THREE must fail. That asymmetry is the whole reason this script exists — an
# account that is broken outright (wrong login shell, missing rrsync, revoked
# key) still fails all three of the checks that are supposed to fail, so a
# hand-run session reads as "everything refused, looks locked down" when in
# fact nothing works at all. Only check 1 tells those two states apart.
#
# Exits 0 only if all four hold.
set -uo pipefail

HOST="${SULLIVAN_SSH_HOST:-}"
USER_NAME="${SULLIVAN_SSH_USER:-shelfmark-sync}"
IDENTITY="${SULLIVAN_SSH_IDENTITY_FILE:-/run/secrets/id_ed25519}"

if [ -z "$HOST" ]; then
    echo "SULLIVAN_SSH_HOST is not set" >&2
    exit 2
fi
if [ ! -r "$IDENTITY" ]; then
    echo "identity file $IDENTITY is missing or unreadable" >&2
    exit 2
fi

KNOWN_HOSTS="$(mktemp)"
PROBE="$(mktemp)"
trap 'rm -f "$KNOWN_HOSTS" "$PROBE"' EXIT
echo "shelfmark write probe" > "$PROBE"

SSH_OPTS=(
    ssh -i "$IDENTITY"
    -o StrictHostKeyChecking=accept-new
    -o "UserKnownHostsFile=$KNOWN_HOSTS"
    -o ConnectTimeout=10
    -o BatchMode=yes
)
SSH_E="${SSH_OPTS[*]}"
TARGET="$USER_NAME@$HOST"

failures=0

# Every status below is derived from a captured exit code, never from the tail
# of a pipeline. `if rsync ... | head; then` tests head, which succeeds
# whatever rsync did, and will happily call a dead account healthy.
pass() { printf '  PASS  %s\n' "$1"; }
fail() {
    printf '  FAIL  %s\n' "$1"
    [ -n "${2:-}" ] && printf '        %s\n' "$(printf '%s' "$2" | head -3 | tr '\n' ' ')"
    failures=$((failures + 1))
}

echo "Verifying $TARGET"
echo

# 1. The one that must SUCCEED. If this fails, nothing below is meaningful.
if out=$(rsync -e "$SSH_E" --list-only "$TARGET:/" 2>&1); then
    pass "read of the download category succeeds"
else
    fail "read of the download category succeeds" "rsync: $out"
    case "$out" in
        *"not available"*|*"protocol version mismatch"*)
            cat <<EOF

        The account's login shell is refusing the forced command. An SSH
        forced command runs as \$SHELL -c '<command>', so a shell of
        /usr/sbin/nologin blocks rrsync before it starts. On Sullivan:

            sudo usermod --shell /bin/sh $USER_NAME

        Nothing is given up by that: the forced command in authorized_keys
        replaces whatever the client asks for, so interactive use stays
        unreachable. The shell was never what prevented it.
EOF
            ;;
    esac
fi

# 2. Interactive use must be refused. Assert on the OUTPUT as well as the exit
#    code — a shell that ran prints uid=..., which is unambiguous.
out=$("${SSH_OPTS[@]}" "$TARGET" id 2>&1)
rc=$?
if [[ "$out" == *uid=* ]]; then
    fail "interactive command is refused" "a shell ran: $out"
elif [ "$rc" -eq 0 ]; then
    fail "interactive command is refused" "exit 0 with output: $out"
else
    pass "interactive command is refused"
fi

# 3. Write must be refused — and confirmed absent afterwards, because a
#    non-zero exit on its own does not prove nothing landed.
probe_name="shelfmark-write-probe-$$.txt"
if out=$(rsync -e "$SSH_E" "$PROBE" "$TARGET:/$probe_name" 2>&1); then
    fail "write to Sullivan is refused" "rsync accepted the upload"
else
    landed=$(rsync -e "$SSH_E" --list-only "$TARGET:/$probe_name" 2>&1)
    if [ $? -eq 0 ] && [[ "$landed" == *"$probe_name"* ]]; then
        fail "write to Sullivan is refused" "rsync failed but $probe_name is on Sullivan"
    else
        pass "write to Sullivan is refused"
    fi
fi

# 4. Reading outside the rrsync root must be refused.
if out=$(rsync -e "$SSH_E" --list-only "$TARGET:/../../etc/passwd" 2>&1); then
    fail "read outside the category is refused" "listed a path outside the root: $out"
else
    pass "read outside the category is refused"
fi

echo
if [ "$failures" -eq 0 ]; then
    echo "All four checks hold."
    exit 0
fi
echo "$failures of 4 checks failed."
exit 1

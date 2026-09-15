#!/bin/sh
# State collector for ansible-rehearse. Runs inside the rehearsal container as root.
# Output: sectioned text, parsed by snapshot.py on the host. File records are
# NUL-delimited (filenames cannot contain NUL, so records cannot be forged by
# crafted filenames); other sections are line-oriented.
# Requires GNU findutils/coreutils (Debian/Ubuntu/RHEL-family images).
set -u

WATCH_DIRS="${REHEARSE_WATCH_DIRS:-/etc /usr/local /opt /srv /root /home /var/spool/cron /var/www}"
HASH_MAX_BYTES="${REHEARSE_HASH_MAX_BYTES:-4194304}"
SELF_DIR="/opt/ansible-rehearse"

# The nonce makes section markers unforgeable: a crafted filename cannot inject
# a marker line because it cannot predict the per-snapshot random value.
NONCE="${REHEARSE_NONCE:-0}"

begin() { printf '###REHEARSE[%s]:BEGIN %s###\n' "$NONCE" "$1"; }
end()   { printf '###REHEARSE[%s]:END###\n' "$NONCE"; }

# Only our known subdirectories are hidden from the diff; anything else a
# playbook drops under $SELF_DIR still shows up.
list_files() {
    d="$1"; shift
    find "$d" -xdev \
        \( -path "$SELF_DIR/venv" -o -path "$SELF_DIR/project" \
           -o -path "$SELF_DIR/callbacks" -o -path "$SELF_DIR/tmp" \
           -o -path "$SELF_DIR/ansible-home" -o -name __pycache__ \) -prune -o \
        "$@" 2>/dev/null
}

begin meta
printf 'format=2\n'
end

if command -v dpkg-query >/dev/null 2>&1; then
    begin packages.dpkg
    # ii = installed, hi = installed and held (dpkg_selections: hold)
    dpkg-query -W -f '${db:Status-Abbrev}\t${Package}\t${Version}\n' 2>/dev/null \
        | grep -E '^[hi]i' | cut -f2,3
    end
elif command -v rpm >/dev/null 2>&1; then
    begin packages.rpm
    rpm -qa --qf '%{NAME}\t%{VERSION}-%{RELEASE}\n' 2>/dev/null | sort
    end
fi

begin files
for d in $WATCH_DIRS; do
    [ -d "$d" ] || continue
    list_files "$d" \( -type f -o -type d \) -printf '%y|%#m|%u|%g|%s|%T@|%p\0'
    list_files "$d" -type l -printf 'l|%#m|%u|%g|%s|%T@|%p -> %l\0'
done
printf '\n'
end

begin hashes
for d in $WATCH_DIRS; do
    [ -d "$d" ] || continue
    list_files "$d" -type f -size -"$HASH_MAX_BYTES"c -print0 \
        | xargs -0 -r md5sum 2>/dev/null
done
end

if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    begin services.unitfiles
    systemctl list-unit-files --type=service --no-pager --no-legend --plain 2>/dev/null
    end
    begin services.running
    systemctl list-units --type=service --state=running --no-pager --no-legend --plain 2>/dev/null
    end
else
    begin services.unavailable
    end
fi

# `/proc/net` stores IPv4 addresses as little-endian hex and IPv6 addresses as
# four little-endian 32-bit words.  Keep this fallback in POSIX sh so minimal
# rehearsal images do not need iproute2 just to collect listening ports.
_hex_value() {
    printf '%d' "0x$1"
}

_ipv4_from_proc() {
    value="$1"
    first="${value%??????}"
    rest="${value#??}"
    second="${rest%????}"
    rest="${rest#??}"
    third="${rest%??}"
    fourth="${rest#??}"
    printf '%d.%d.%d.%d' \
        "$(_hex_value "$fourth")" "$(_hex_value "$third")" \
        "$(_hex_value "$second")" "$(_hex_value "$first")"
}

_reverse_proc_word() {
    word="$1"
    first="${word%??????}"
    rest="${word#??}"
    second="${rest%????}"
    rest="${rest#??}"
    third="${rest%??}"
    fourth="${rest#??}"
    printf '%s%s%s%s' "$fourth" "$third" "$second" "$first"
}

_ipv6_from_proc() {
    value="$1"
    word1="$(printf '%s' "$value" | cut -c 1-8)"
    word2="$(printf '%s' "$value" | cut -c 9-16)"
    word3="$(printf '%s' "$value" | cut -c 17-24)"
    word4="$(printf '%s' "$value" | cut -c 25-32)"
    reversed1="$(_reverse_proc_word "$word1")"
    reversed2="$(_reverse_proc_word "$word2")"
    reversed3="$(_reverse_proc_word "$word3")"
    reversed4="$(_reverse_proc_word "$word4")"
    printf '[%s:%s:%s:%s:%s:%s:%s:%s]' \
        "${reversed1%????}" "${reversed1#????}" \
        "${reversed2%????}" "${reversed2#????}" \
        "${reversed3%????}" "${reversed3#????}" \
        "${reversed4%????}" "${reversed4#????}"
}

_emit_proc_net_file() {
    proto="$1"
    path="$2"
    [ -r "$path" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] || continue
        set -- $line
        [ "$#" -ge 4 ] || continue
        [ "$1" = sl ] && continue
        local_field="$2"
        address="${local_field%:*}"
        port_hex="${local_field#*:}"
        state="$4"
        case "$proto" in
            tcp|tcp6)
                [ "$state" = 0A ] || continue
                status=LISTEN
                ;;
            udp|udp6)
                status=UNCONN
                ;;
            *)
                continue
                ;;
        esac
        port="$(_hex_value "$port_hex")"
        case "$proto" in
            tcp6|udp6)
                address="$(_ipv6_from_proc "$address")"
                remote='[::]:*'
                ;;
            *)
                address="$(_ipv4_from_proc "$address")"
                remote='0.0.0.0:*'
                ;;
        esac
        printf '%s %s 0 0 %s:%s %s\n' "$proto" "$status" "$address" "$port" "$remote"
    done < "$path"
}

_emit_proc_net_ports() {
    root="$1"
    _emit_proc_net_file tcp "$root/tcp"
    _emit_proc_net_file tcp6 "$root/tcp6"
    _emit_proc_net_file udp "$root/udp"
    _emit_proc_net_file udp6 "$root/udp6"
}

if command -v ss >/dev/null 2>&1; then
    begin ports
    ss -tulnpH 2>/dev/null || ss -tulnH 2>/dev/null
    end
else
    begin ports
    _emit_proc_net_ports "${REHEARSE_PROC_NET_ROOT:-/proc/net}"
    end
fi

begin users
cat /etc/passwd 2>/dev/null
end

begin groups
cat /etc/group 2>/dev/null
end

printf '###REHEARSE[%s]:DONE###\n' "$NONCE"

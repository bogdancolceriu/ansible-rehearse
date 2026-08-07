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

begin() { printf '###REHEARSE:BEGIN %s###\n' "$1"; }
end()   { printf '###REHEARSE:END###\n'; }

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

if command -v ss >/dev/null 2>&1; then
    begin ports
    ss -tulnpH 2>/dev/null || ss -tulnH 2>/dev/null
    end
fi

begin users
cat /etc/passwd 2>/dev/null
end

begin groups
cat /etc/group 2>/dev/null
end

printf '###REHEARSE:DONE###\n'

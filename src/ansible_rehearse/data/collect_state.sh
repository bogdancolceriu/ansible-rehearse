#!/bin/sh
# State collector for ansible-rehearse. Runs inside the rehearsal container as root.
# Output: sectioned plain text, parsed by snapshot.py on the host.
# Requires GNU findutils/coreutils (Debian/Ubuntu/RHEL-family images).
set -u

WATCH_DIRS="${REHEARSE_WATCH_DIRS:-/etc /usr/local /opt /srv /root /home /var/spool/cron /var/www}"
HASH_MAX_BYTES="${REHEARSE_HASH_MAX_BYTES:-4194304}"
# Our own workspace (venv, project copy) must never show up in the diff.
SELF_DIR="/opt/ansible-rehearse"

begin() { printf '###REHEARSE:BEGIN %s###\n' "$1"; }
end()   { printf '###REHEARSE:END###\n'; }

begin meta
printf 'format=1\n'
end

if command -v dpkg-query >/dev/null 2>&1; then
    begin packages.dpkg
    dpkg-query -W -f '${db:Status-Abbrev}\t${Package}\t${Version}\n' 2>/dev/null \
        | grep '^ii' | cut -f2,3
    end
elif command -v rpm >/dev/null 2>&1; then
    begin packages.rpm
    rpm -qa --qf '%{NAME}\t%{VERSION}-%{RELEASE}\n' 2>/dev/null | sort
    end
fi

begin files
for d in $WATCH_DIRS; do
    [ -d "$d" ] || continue
    find "$d" -xdev \( -path "$SELF_DIR" -o -name __pycache__ \) -prune -o \
        \( -type f -o -type d \) -printf '%y|%#m|%u|%g|%s|%p\n' 2>/dev/null
    find "$d" -xdev -path "$SELF_DIR" -prune -o \
        -type l -printf 'l|%#m|%u|%g|%s|%p -> %l\n' 2>/dev/null
done
end

begin hashes
for d in $WATCH_DIRS; do
    [ -d "$d" ] || continue
    find "$d" -xdev -path "$SELF_DIR" -prune -o \
        -type f -size -"$HASH_MAX_BYTES"c -print0 2>/dev/null \
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

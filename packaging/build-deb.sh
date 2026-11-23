#!/bin/sh
# Builds the nipovpn-dashboard Debian package.
#
#   packaging/build-deb.sh VERSION [OUTPUT_DIR] [PACKAGE_FILE_NAME]
#
# The package is architecture independent (Architecture: all). Needs dpkg-deb.
set -eu

VERSION="${1:?usage: build-deb.sh VERSION [OUTPUT_DIR] [PACKAGE_FILE_NAME]}"
OUT="${2:-build}"
FILE="${3:-nipovpn-dashboard_${VERSION}_all.deb}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO/src"
PKG="$REPO/packaging"
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT

LIB="$ROOT/usr/lib/nipovpn-dashboard"
install -d "$LIB/static" "$ROOT/usr/bin" "$ROOT/usr/lib/systemd/system" \
    "$ROOT/etc/nipovpn-dashboard" "$ROOT/DEBIAN" "$ROOT/usr/share/doc/nipovpn-dashboard"

install -m 644 "$SRC"/agentconfig.py "$SRC"/monitor.py "$SRC"/qr.py \
    "$SRC"/serverconfig.py "$SRC"/settings.py "$LIB/"
install -m 755 "$SRC/nipovpn-dashboard.py" "$LIB/"
install -m 644 "$SRC"/static/* "$LIB/static/"
ln -s ../lib/nipovpn-dashboard/nipovpn-dashboard.py "$ROOT/usr/bin/nipovpn-dashboard"
install -m 644 "$PKG/nipovpn-dashboard.service" "$ROOT/usr/lib/systemd/system/"
chmod 700 "$ROOT/etc/nipovpn-dashboard"
install -m 600 "$REPO/config.yaml" "$ROOT/etc/nipovpn-dashboard/config.yaml"
install -m 644 "$REPO/README.md" "$ROOT/usr/share/doc/nipovpn-dashboard/README.md"
cat > "$ROOT/usr/share/doc/nipovpn-dashboard/copyright" <<'COPYRIGHT'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: nipovpn-dashboard
Source: https://github.com/MortezaBashsiz/nipovpn-dashboard

Files: *
Copyright: Morteza Bashsiz and the NipoVPN contributors
License: GPL-3
 On Debian systems, the full text of the GNU General Public License
 version 3 can be found in /usr/share/common-licenses/GPL-3.
COPYRIGHT
chmod 644 "$ROOT/usr/share/doc/nipovpn-dashboard/copyright"

sed "s/@VERSION@/$VERSION/" "$PKG/debian/control" > "$ROOT/DEBIAN/control"
printf 'Installed-Size: %s\n' "$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)" >> "$ROOT/DEBIAN/control"
install -m 644 "$PKG/debian/conffiles" "$ROOT/DEBIAN/"
install -m 755 "$PKG/debian/postinst" "$PKG/debian/prerm" "$PKG/debian/postrm" "$ROOT/DEBIAN/"

mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$ROOT" "$OUT/$FILE"
echo "$OUT/$FILE"

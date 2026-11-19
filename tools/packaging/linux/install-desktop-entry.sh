#!/bin/sh
# Adds ReverseHire to the applications menu of the current user (no administrator
# rights). Run it from the unpacked ReverseHire folder; run it again after moving
# the folder. To remove the entry: install-desktop-entry.sh --remove
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
if [ "${1:-}" = "--remove" ]; then
    rm -f "$APPS/reversehire.desktop" "$ICONS/reversehire.png"
    echo "ReverseHire removed from the applications menu."
    exit 0
fi
mkdir -p "$APPS" "$ICONS"
cp "$HERE/reversehire.png" "$ICONS/reversehire.png"
cat > "$APPS/reversehire.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ReverseHire
GenericName=CV-based company finder
Comment=Starts from a CV and finds the companies that fit the profile within a radius
Exec="$HERE/reversehire"
Icon=reversehire
Terminal=false
Categories=Office;
StartupWMClass=reversehire
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1 || true
echo "ReverseHire added to the applications menu ($APPS/reversehire.desktop)."

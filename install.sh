#!/usr/bin/env bash
# Installs Lightshot for Linux into ~/.local (no root needed).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.local/share/lightshot-linux"
BIN="$HOME/.local/bin"
mkdir -p "$DIR" "$BIN" "$HOME/.local/share/applications" "$HOME/.config/autostart"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

cp "$SRC/lightshot.py" "$DIR/lightshot.py"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet PyQt5 pynput

cat > "$BIN/lightshot" <<EOF
#!/usr/bin/env bash
exec "$DIR/venv/bin/python" "$DIR/lightshot.py" "\$@"
EOF
chmod +x "$BIN/lightshot"

cat > "$HOME/.local/share/applications/lightshot.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Lightshot
Comment=Capture and annotate screenshots
Exec=$BIN/lightshot
Icon=camera-photo
Categories=Utility;Graphics;
EOF

cat > "$HOME/.config/autostart/lightshot.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Lightshot
Exec=$BIN/lightshot
X-GNOME-Autostart-enabled=true
EOF

echo
echo "Installed. Start it with:  lightshot   (make sure ~/.local/bin is in your PATH)"
echo "Trigger a capture with:    lightshot --capture"
echo
echo "Optional helpers for Wayland capture: grim (Sway/Hyprland), gnome-screenshot (GNOME), spectacle (KDE)."

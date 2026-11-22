#!/bin/sh
# Builds "PlayBill Display.app" (a menu bar app) into /Applications (override with DEST=...).
# The app runs this folder's code via uv, so rebuild only if you move this folder.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
UV=$(command -v uv || echo "$HOME/.local/bin/uv")
[ -x "$UV" ] || { echo "uv not found; install it from https://docs.astral.sh/uv/"; exit 1; }
DEST=${DEST:-/Applications}
APP="$DEST/PlayBill Display.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>PlayBill Display</string>
  <key>CFBundleDisplayName</key><string>PlayBill Display</string>
  <key>CFBundleIdentifier</key><string>com.playbilldisplay.app</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/launcher" <<LAUNCH
#!/bin/sh
export PLAYBILL_APP_BUNDLE="\$(cd "\$(dirname "\$0")/../.." && pwd)"
LOG="\$HOME/Library/Logs/PlayBillDisplay.log"
echo "--- \$(date) starting" >> "\$LOG"
cd "$HERE"
exec "$UV" run --quiet --script "$HERE/menubar.py" "\$@" >> "\$LOG" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/launcher"

# Icon: gold marquee card with a serif "P".
ICONSET=$(mktemp -d)/AppIcon.iconset
mkdir -p "$ICONSET"
"$UV" run --quiet --with pillow python - "$ICONSET" <<'PY'
import sys
from PIL import Image, ImageDraw, ImageFont
out = sys.argv[1]
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([90, 90, S - 90, S - 90], radius=190, fill=(242, 194, 0, 255), outline=(17, 17, 17, 255), width=28)
d.rectangle([90, 690, S - 90, 740], fill=(17, 17, 17, 255))
for path in ("/System/Library/Fonts/Supplemental/Georgia Bold.ttf", "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"):
    try:
        font = ImageFont.truetype(path, 520); break
    except OSError:
        font = ImageFont.load_default()
d.text((S / 2, 400), "P", font=font, fill=(17, 17, 17, 255), anchor="mm")
for n in (16, 32, 128, 256, 512):
    img.resize((n, n), Image.LANCZOS).save(f"{out}/icon_{n}x{n}.png")
    img.resize((n * 2, n * 2), Image.LANCZOS).save(f"{out}/icon_{n}x{n}@2x.png")
PY
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"

echo "Built: $APP"

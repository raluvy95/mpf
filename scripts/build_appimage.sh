#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "AppImage builds are supported on Linux only." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
APPIMAGETOOL="${APPIMAGETOOL:-appimagetool}"
VERSION="${MPF_VERSION:-0.1.0}"
BUILD_DIR="${BUILD_DIR:-${ROOT_DIR}/build/appimage}"
APP_DIR="${BUILD_DIR}/MPF.AppDir"
OUTPUT="${OUTPUT:-${ROOT_DIR}/dist/mpf-${VERSION}-x86_64.AppImage}"

rm -rf "${BUILD_DIR}"
mkdir -p "${APP_DIR}/usr/share/applications"
mkdir -p "${APP_DIR}/usr/share/icons/hicolor/scalable/apps"
mkdir -p "$(dirname "${OUTPUT}")"

"${PYTHON_BIN}" -m PyInstaller \
    --noconfirm \
    --clean \
    --onedir \
    --name mpf \
    --distpath "${BUILD_DIR}/pyinstaller-dist" \
    --workpath "${BUILD_DIR}/pyinstaller-work" \
    --specpath "${BUILD_DIR}" \
    --hidden-import mpf_core.cache \
    --hidden-import mpf_core.config \
    --hidden-import mpf_core.fetcher \
    --hidden-import mpf_core.fuzzy \
    --hidden-import mpf_core.models \
    --hidden-import mpf_core.paths \
    --hidden-import mpf_core.player \
    --hidden-import mpf_core.previewer \
    --hidden-import mpf_core.visualizer \
    "${ROOT_DIR}/mpf.py"

cp -a "${BUILD_DIR}/pyinstaller-dist/mpf/." "${APP_DIR}/usr/bin/"

cat > "${APP_DIR}/AppRun" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
exec "${HERE}/usr/bin/mpf" "$@"
EOF
chmod +x "${APP_DIR}/AppRun"

cat > "${APP_DIR}/usr/share/applications/mpf.desktop" <<EOF
[Desktop Entry]
Name=MPF
Comment=Minimal YouTube Playlist terminal player
Exec=mpf
Icon=mpf
Terminal=true
Type=Application
Categories=AudioVideo;Audio;Player;
EOF
cp "${APP_DIR}/usr/share/applications/mpf.desktop" "${APP_DIR}/mpf.desktop"

cat > "${APP_DIR}/usr/share/icons/hicolor/scalable/apps/mpf.svg" <<'EOF'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="24" fill="#111827"/>
  <path d="M37 30h18l9 22 9-22h18v68H73V63l-9 21-9-21v35H37z" fill="#67e8f9"/>
</svg>
EOF
cp "${APP_DIR}/usr/share/icons/hicolor/scalable/apps/mpf.svg" "${APP_DIR}/mpf.svg"

if ! command -v "${APPIMAGETOOL}" >/dev/null 2>&1 && [[ ! -x "${APPIMAGETOOL}" ]]; then
    echo "appimagetool is required; set APPIMAGETOOL to its path." >&2
    exit 1
fi

"${APPIMAGETOOL}" "${APP_DIR}" "${OUTPUT}"
echo "Created ${OUTPUT}"

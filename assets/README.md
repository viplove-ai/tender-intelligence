# Build resources

electron-builder reads platform icons from this directory (see `build.directories.buildResources`
in `package.json`):

- `icon.icns` — macOS `.dmg` icon (1024x1024 recommended)
- `icon.ico` — Windows `.exe` icon (256x256 recommended)

Without these, electron-builder falls back to its default Electron icon. Add real icons here
before a production release.

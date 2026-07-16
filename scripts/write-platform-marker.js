#!/usr/bin/env node
// Bakes which OS this specific desktop build targets into a plain text file bundled
// alongside app.py (see stlite.desktop.files in package.json). Each installer (.dmg vs
// .exe) is already built by a separate, platform-specific CI job — there's no need (and,
// as it turns out, no way: stlite's internal websocket bridge doesn't carry a real browser
// User-Agent) to detect the OS at runtime inside the desktop build. Runs automatically
// before `npm run dump` via npm's "pre" script convention.
const fs = require('fs');
const path = require('path');

const PLATFORM_LABELS = { darwin: 'macOS', win32: 'Windows', linux: 'Linux' };
const label = PLATFORM_LABELS[process.platform] || 'unknown';

const outPath = path.join(__dirname, '..', 'desktop_platform.txt');
fs.writeFileSync(outPath, label + '\n');
console.log(`Wrote ${outPath}: ${label}`);

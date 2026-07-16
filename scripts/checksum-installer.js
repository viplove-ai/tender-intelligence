#!/usr/bin/env node
// Computes a SHA256 checksum file for each built installer in dist/, so the desktop
// app's "Download & verify installer" flow (app.py's download_and_verify_installer) has
// something to check the fetched bytes against. Uses Node's crypto module instead of
// shasum/certutil so the same script works unmodified on the macOS and Windows CI runners.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const distDir = path.join(__dirname, '..', 'dist');
const installers = fs.readdirSync(distDir).filter(f => f.endsWith('.dmg') || f.endsWith('.exe'));

if (installers.length === 0) {
  console.error('No .dmg or .exe found in dist/ — run npm run app:dist first.');
  process.exit(1);
}

for (const file of installers) {
  const filePath = path.join(distDir, file);
  const hash = crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex');
  const checksumPath = filePath + '.sha256';
  fs.writeFileSync(checksumPath, `${hash}  ${file}\n`);
  console.log(`${file}: ${hash}`);
}

// Playwright against the preinstalled Chrome for Testing (channel "chrome" = /opt/google/chrome/chrome).
module.exports = {
  testDir: '.',
  outputDir: (process.env.E2E_OUT || '/tmp/e2e') + '/playwright',
  reporter: 'list',
  use: { channel: 'chrome', headless: true },
};

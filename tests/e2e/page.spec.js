const { test, expect } = require('@playwright/test');

test('page served from the nested container renders in Chrome', async ({ page }) => {
  await page.goto(process.env.E2E_URL || 'http://127.0.0.1:18080/');
  await expect(page.locator('#status')).toHaveText('served from a nested container');
  await page.screenshot({ path: (process.env.E2E_OUT || '/tmp/e2e') + '/nested-web.png' });
});

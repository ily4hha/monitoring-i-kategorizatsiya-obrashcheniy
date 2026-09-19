// Run with Playwright's page after opening a temporary server seeded with 21 rows.
// Fixture columns: Тема, _customer, *custom, Код; no underscores in values.
async (page) => {
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#page-label').textContent === '1–20 из 21');
  await page.locator('body').click({ position: { x: 1, y: 1 } });
  await page.keyboard.press('Tab');
  const focus = await page.evaluate(() => {
    const file = document.querySelector('#dataset-file');
    const style = getComputedStyle(document.querySelector('.upload-button'));
    return { id: document.activeElement.id, visible: file.matches(':focus-visible'), width: style.outlineWidth, style: style.outlineStyle };
  });
  assert(focus.id === 'dataset-file' && focus.visible && focus.width === '3px' && focus.style === 'solid', `File focus is invisible: ${JSON.stringify(focus)}`);
  await page.screenshot({ path: '/private/tmp/postcode-browser-focus.png' });
  await page.getByRole('tab', { name: 'История', exact: true }).click();

  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let requests = 0;
  const routePattern = '**/api/datasets/*/records?**';
  await page.route(routePattern, async route => {
    requests++;
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  await page.getByRole('button', { name: 'Далее', exact: true }).dblclick({ delay: 5 });
  assert(await page.locator('#next-page').isDisabled(), 'Next is not disabled while loading');
  release();
  await page.waitForFunction(() => document.querySelector('#page-label').textContent === '21–21 из 21');
  assert(requests === 1, `Double click sent ${requests} requests`);
  assert(await page.locator('tbody tr').count() === 1, 'Wrong last page row count');
  await page.unroute(routePattern);
  await page.screenshot({ path: '/private/tmp/postcode-browser-pagination.png' });

  await page.getByRole('button', { name: 'Назад', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#page-label').textContent === '1–20 из 21');
  await page.locator('.record-link').first().click();
  await page.locator('#record-dialog').waitFor({ state: 'visible' });
  const fields = await page.locator('#record-details dt').allTextContents();
  assert(fields.includes('Customer') && fields.includes('Custom'), 'User columns are hidden');
  assert(!fields.some(field => ['_record_id', '_sheet', '_source_row'].includes(field)), 'Metadata is visible');
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click();

  const responsePromise = page.waitForResponse(response => response.url().includes('/records?') && /[?&]q=_(&|$)/.test(response.url()));
  await page.locator('#record-search').fill('_');
  const response = await responsePromise;
  const result = await response.json();
  assert(result.total === 0, 'Underscore matched service keys');
  await page.waitForFunction(() => document.querySelector('#page-label').textContent === '0–0 из 0');
  assert(await page.locator('#table-wrap').innerText() === 'По вашему запросу ничего не найдено.', 'Missing empty search state');
  await page.locator('#record-search').fill('');
  await page.waitForFunction(() => document.querySelector('#page-label').textContent === '1–20 из 21');
  assert(errors.length === 0, `Browser errors: ${errors.join('; ')}`);
  return { focus, doubleClickRequests: requests, lastPage: '21–21 из 21', underscoreTotal: result.total, visibleUserFields: fields, pageErrors: errors };
}

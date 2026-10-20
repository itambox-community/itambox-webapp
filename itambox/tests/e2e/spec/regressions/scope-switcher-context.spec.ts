import { test, expect } from '../../fixtures/test';
import { createOwnedAsset } from '../../fixtures/factories/assets';
import { getJsonRows } from '../../helpers/api';
import * as path from 'path';

const aggregateStorageState = path.resolve(__dirname, '../../.auth/aggregate.json');
const TENANT_A_SLUG = 'helix-rnd';
const TENANT_B_SLUG = process.env.E2E_AGGREGATE_SECOND_TENANT_SLUG || 'helix-mfg';

type AccessibleTenant = {
  id: string;
  name: string;
  slug: string;
};

function accessibleTenant(rows: Record<string, unknown>[], slug: string): AccessibleTenant {
  const matches = rows.filter((row) => row.slug === slug);
  if (matches.length !== 1) {
    throw new Error(`Aggregate E2E principal must expose exactly one accessible tenant ${slug}.`);
  }
  const rawId = matches[0].id;
  if (typeof rawId !== 'string' && typeof rawId !== 'number') {
    throw new Error(`Accessible tenant ${slug} has no usable ID.`);
  }
  const rawName = matches[0].name;
  if (typeof rawName !== 'string' || !rawName.trim()) {
    throw new Error(`Accessible tenant ${slug} has no usable name.`);
  }
  return { id: String(rawId), name: rawName, slug };
}

test.describe('scope switcher context preservation (issue #499)', { tag: ['@pr', '@aggregate'] }, () => {
  test.use({ storageState: aggregateStorageState });

  test('keeps the list filter across scope switches and lands detail switches on the list', async ({
    page,
    playwright,
    api,
    cleanup,
    runId,
  }) => {
    // Prove the aggregate principal's scope through the real authenticated
    // projection before creating owned records for the target tenants.
    const aggregatePage = await page.goto('/assets/assets/?switch_all_accessible=1', { waitUntil: 'domcontentloaded' });
    expect(aggregatePage?.status(), 'aggregate asset list response').toBe(200);
    await expect(page.locator('.workspace-switcher-name')).toHaveText('All Tenants');

    const accessibleTenants = await getJsonRows(
      page.request,
      '/api/organization/tenants/?limit=100',
      'aggregate tenant visibility',
    );
    const tenantA = accessibleTenant(accessibleTenants, TENANT_A_SLUG);
    const tenantB = accessibleTenant(accessibleTenants, TENANT_B_SLUG);

    // Tenant-bound setup contexts: the existing token writes for A, a second
    // token writes for B; the browser session stays the aggregate principal.
    const secondaryToken = process.env.E2E_API_TOKEN_SECONDARY;
    if (!secondaryToken) {
      throw new Error('E2E_API_TOKEN_SECONDARY is required for secondary-tenant REST setup.');
    }
    const apiSecondary = await playwright.request.newContext({
      baseURL: process.env.E2E_BASE_URL || 'http://localhost:8000',
      extraHTTPHeaders: { Authorization: `Token ${secondaryToken}` },
    });
    cleanup.add('dispose secondary-tenant setup client', async () => {
      await apiSecondary.dispose();
    });

    const assetA1 = await createOwnedAsset(api, cleanup, tenantA.id, `${runId}-a1`, { tagScope: 'issue499' });
    const assetA2 = await createOwnedAsset(api, cleanup, tenantA.id, `${runId}-a2`, { tagScope: 'issue499' });
    const assetB1 = await createOwnedAsset(apiSecondary, cleanup, tenantB.id, `${runId}-b1`, { tagScope: 'issue499' });

    const switcher = page.locator('.itambox-topbar .nav-item.dropdown').filter({ has: page.locator('a.workspace-switcher') });
    const listRows = page.locator('#object-list-table-container table tbody tr');
    const filter = encodeURIComponent(runId);

    const listPath = `/assets/assets/?switch_all_accessible=1&q=${filter}`;
    const listResponse = await page.goto(listPath, { waitUntil: 'domcontentloaded' });
    expect(listResponse?.status(), `GET ${listPath}`).toBe(200);
    await expect(listRows).toHaveCount(3);

    // The switcher link for tenant A must carry the active filter instead of
    // replacing the whole query string (issue #499, part 2).
    await switcher.locator('a.workspace-switcher').click();
    const tenantEntry = switcher.locator(`.dropdown-menu a[href*="switch_tenant=${tenantA.id}"]`).first();
    await expect(tenantEntry).toBeVisible();
    await expect(tenantEntry).toHaveAttribute('href', new RegExp(`[?&]q=${filter}`));

    await tenantEntry.click();
    await expect(page).toHaveURL(
      (url) => url.pathname === '/assets/assets/' && url.searchParams.get('switch_tenant') === tenantA.id,
    );
    expect(new URL(page.url()).searchParams.get('q'), 'filter survives the scope switch').toBe(runId);
    await expect(listRows).toHaveCount(2);
    await expect(listRows.filter({ has: page.getByText(assetB1.assetTag, { exact: true }) })).toHaveCount(0);

    // Switching back to the accessible-tenant scope keeps the filter too.
    await switcher.locator('a.workspace-switcher').click();
    const allTenantsEntry = switcher.locator('.dropdown-menu a[href*="switch_all_accessible=1"]').first();
    await expect(allTenantsEntry).toBeVisible();
    await allTenantsEntry.click();
    await expect(page).toHaveURL(
      (url) => url.pathname === '/assets/assets/' && url.searchParams.get('switch_all_accessible') === '1',
    );
    expect(new URL(page.url()).searchParams.get('q'), 'filter survives the switch back').toBe(runId);
    await expect(listRows).toHaveCount(3);

    // Switching scope from an object page must land on the model list in the
    // new scope instead of answering the same detail URL with a bare 404
    // (issue #499, part 3). The href assertion pins the landing target, the
    // response status pins the absence of the 404.
    const detailPath = `/assets/assets/${assetA1.id}/`;
    const detailResponse = await page.goto(detailPath, { waitUntil: 'domcontentloaded' });
    expect(detailResponse?.status(), `GET ${detailPath}`).toBe(200);

    await switcher.locator('a.workspace-switcher').click();
    const detailTenantEntry = switcher.locator(`.dropdown-menu a[href*="switch_tenant=${tenantB.id}"]`).first();
    await expect(detailTenantEntry).toBeVisible();
    await expect(detailTenantEntry).toHaveAttribute('href', /\/assets\/assets\/\?switch_tenant=/);

    const landingResponse = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === '/assets/assets/' &&
        new URL(response.url()).searchParams.get('switch_tenant') === tenantB.id,
    );
    await detailTenantEntry.click();
    expect((await landingResponse).status(), 'detail-page switch landing response').toBe(200);
    await expect(page).toHaveURL(
      (url) => url.pathname === '/assets/assets/' && url.searchParams.get('switch_tenant') === tenantB.id,
    );
    await expect(switcher.locator(`a[href*="switch_tenant=${tenantB.id}"].active`).first()).toHaveCount(1);
  });

  test('shows the filter-reset notice once and does not replay it on reload', async ({ page }) => {
    const aggregatePage = await page.goto('/assets/assets/?switch_all_accessible=1', { waitUntil: 'domcontentloaded' });
    expect(aggregatePage?.status(), 'aggregate asset list response').toBe(200);

    const accessibleTenants = await getJsonRows(
      page.request,
      '/api/organization/tenants/?limit=100',
      'aggregate tenant visibility',
    );
    const tenantA = accessibleTenant(accessibleTenants, TENANT_A_SLUG);

    // A tenant filter is meaningful in the all-accessible scope and is reset by
    // a switch to a concrete tenant scope; that reset is surfaced as a notice.
    const filteredPath = `/assets/assets/?switch_all_accessible=1&tenant=${tenantA.id}`;
    const filteredResponse = await page.goto(filteredPath, { waitUntil: 'domcontentloaded' });
    expect(filteredResponse?.status(), `GET ${filteredPath}`).toBe(200);
    await expect(page.locator('#django-messages')).not.toContainText(/tenant filter was reset/);

    const switcher = page.locator('.itambox-topbar .nav-item.dropdown').filter({ has: page.locator('a.workspace-switcher') });
    await switcher.locator('a.workspace-switcher').click();
    const tenantEntry = switcher.locator(`.dropdown-menu a[href*="switch_tenant=${tenantA.id}"]`).first();
    await expect(tenantEntry).toBeVisible();
    await expect(tenantEntry).toHaveAttribute('href', /[?&]scope_notice=filters/);

    await tenantEntry.click();
    await expect(page).toHaveURL(
      (url) => url.pathname === '/assets/assets/' && url.searchParams.get('switch_tenant') === tenantA.id,
    );
    await expect(page.locator('#django-messages')).toContainText(/tenant filter was reset/);

    // Reloading the switch URL must not replay the message: the scope is
    // already resolved, so no tenant filter was reset by this request.
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.locator('#django-messages')).not.toContainText(/tenant filter was reset/);
  });
});

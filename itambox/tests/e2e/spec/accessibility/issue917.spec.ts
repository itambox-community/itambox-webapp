import { test, expect, type Locator, type Page } from '@playwright/test';

/**
 * Issue #917 — the empty "---------" entry must be the first item of every
 * Tom Select dropdown while the search query is empty.
 *
 * Two paths render those selects:
 *   - remote: the document only holds the empty option and `load` fills the
 *     list from an endpoint (asset filter: Asset Type, Location, Manufacturer)
 *   - server: Django renders every option into the document (asset filter:
 *     Status, Asset Role; the asset edit form)
 * Both must show the empty entry first; typing filters as before and clearing
 * the query brings the empty entry back to the top.
 */

const EMPTY_LABEL = '---------';

/** The generated Tom Select wrapper that replaces a `<select data-tom-select>`. */
function tomWrapper(select: Locator): Locator {
  return select.locator('xpath=following-sibling::div[contains(@class,"ts-wrapper")][1]');
}

/** Option labels of an open Tom Select dropdown, in rendered order. */
async function dropdownLabels(wrapper: Locator): Promise<string[]> {
  const options = wrapper.locator('.ts-dropdown [data-selectable]');
  await expect(options.first(), 'Tom Select dropdown must offer options').toBeVisible();
  return (await options.allTextContents()).map((label) => label.trim());
}

async function openDropdown(wrapper: Locator): Promise<void> {
  await wrapper.locator('.ts-control').click();
  await expect(wrapper.locator('.ts-dropdown')).toBeVisible();
}

/**
 * Close an open dropdown again: an open list overlays the field below it and
 * would swallow the click that opens the next select.
 */
async function closeDropdown(page: Page, wrapper: Locator): Promise<void> {
  await page.keyboard.press('Escape');
  await expect(wrapper.locator('.ts-dropdown')).toBeHidden();
}

/** Open a select and wait until its remote options have been rendered. */
async function openLoadedDropdown(wrapper: Locator): Promise<void> {
  await openDropdown(wrapper);
  await expect
    .poll(async () => (await dropdownLabels(wrapper)).length, {
      message: 'remote options must load into the dropdown',
      timeout: 10000,
    })
    .toBeGreaterThan(1);
}

async function selectField(page: Page, name: string): Promise<Locator> {
  const select = page.locator(`select[data-tom-select][name="${name}"]`);
  await expect(select, `select ${name} must be rendered once`).toHaveCount(1);
  await select.scrollIntoViewIfNeeded();
  const wrapper = tomWrapper(select);
  await expect(wrapper, `select ${name} must be enhanced by Tom Select`).toHaveCount(1);
  return wrapper;
}

test.describe('Issue #917 empty entry first in Tom Select dropdowns', () => {
  test('asset filter keeps the empty entry first for remote and server rendered selects', async ({ page }) => {
    await page.goto('/assets/assets/');
    const filterColumn = page.locator('#object-list-filter-col');
    if (await filterColumn.evaluate((el) => el.classList.contains('d-none'))) {
      await page.locator('#toggle-filters-btn').click();
    }
    const filterForm = page.locator('#filters-sidebar-content form.filter-form-sidebar');
    await expect(filterForm).toBeVisible();

    // Remote-loaded selects: the document carries only the empty entry and the
    // endpoint fills the rest, which is what put a real option first before.
    for (const name of ['asset_type', 'location', 'manufacturer']) {
      const wrapper = await selectField(page, name);
      await openLoadedDropdown(wrapper);
      const labels = await dropdownLabels(wrapper);
      expect(labels[0], `${name}: empty entry must be the first dropdown item`).toBe(EMPTY_LABEL);
      await closeDropdown(page, wrapper);
    }

    // Server-rendered selects on the same filter: the blank choice comes first
    // from Django and must stay first in the rendered dropdown.
    for (const name of ['status', 'asset_role']) {
      const wrapper = await selectField(page, name);
      await openDropdown(wrapper);
      const labels = await dropdownLabels(wrapper);
      expect(labels.length, `${name}: expected a real choice next to the empty entry`).toBeGreaterThan(1);
      expect(labels[0], `${name}: empty entry must be the first dropdown item`).toBe(EMPTY_LABEL);
      await closeDropdown(page, wrapper);
    }

    // Typing filters exactly as before; clearing brings the empty entry back.
    const assetType = await selectField(page, 'asset_type');
    await openLoadedDropdown(assetType);
    const loaded = await dropdownLabels(assetType);
    const realChoice = loaded[1];
    const query = realChoice.split(' ')[0] ?? realChoice;

    await assetType.locator('input.dropdown-input').fill(query);
    await expect
      .poll(async () => (await dropdownLabels(assetType)).includes(EMPTY_LABEL), {
        message: `"${query}" must filter the empty entry out`,
        timeout: 10000,
      })
      .toBe(false);
    const filtered = await dropdownLabels(assetType);
    expect(filtered.length, `"${query}" must keep at least one match`).toBeGreaterThan(0);
    for (const label of filtered) {
      expect(label.toLowerCase(), `"${query}" must filter every remaining option`).toContain(query.toLowerCase());
    }

    await assetType.locator('input.dropdown-input').fill('');
    await expect
      .poll(
        async () => {
          const labels = await dropdownLabels(assetType);
          return labels.length > 1 && labels[0] === EMPTY_LABEL;
        },
        { message: 'clearing the query must bring the empty entry back to the top', timeout: 10000 },
      )
      .toBe(true);
    const cleared = await dropdownLabels(assetType);
    expect(cleared[0], 'clearing the query must bring the empty entry back to the top').toBe(EMPTY_LABEL);
  });

  test('asset edit form renders the empty entry first and keeps multi-select working', async ({ page }) => {
    await page.goto('/assets/assets/');
    const firstAsset = page.locator('table tbody a[href^="/assets/assets/"]').first();
    await expect(firstAsset).toBeVisible();
    const assetHref = await firstAsset.getAttribute('href');
    if (!assetHref) throw new Error('The asset list must link at least one asset.');
    const assetId = assetHref.match(/\/assets\/assets\/(\d+)\//)?.[1];
    if (!assetId) throw new Error(`Unexpected asset link: ${assetHref}`);

    const editResponse = await page.goto(`/assets/assets/${assetId}/edit/`, { waitUntil: 'domcontentloaded' });
    expect(editResponse?.status(), 'asset edit form').toBe(200);

    // Server-rendered options: the blank choice is already the first <option>
    // in the document, and the dropdown must keep it on top.
    const assetRoleSelect = page.locator('select[data-tom-select][name="asset_role"]');
    await expect(assetRoleSelect).toHaveCount(1);
    const firstOptionValue = await assetRoleSelect.locator('option').first().getAttribute('value');
    expect(firstOptionValue, 'server-rendered select must lead with the blank option').toBe('');

    const assetRole = await selectField(page, 'asset_role');
    await openDropdown(assetRole);
    const labels = await dropdownLabels(assetRole);
    expect(labels.length).toBeGreaterThan(1);
    expect(labels[0], 'edit form: empty entry must be the first dropdown item').toBe(EMPTY_LABEL);
    await closeDropdown(page, assetRole);

    // A multi-select keeps its normal options and still accepts a selection.
    const tagsSelect = page.locator('select[data-tom-select][name="tags"]');
    await expect(tagsSelect).toHaveCount(1);
    const tags = await selectField(page, 'tags');
    await openDropdown(tags);
    const tagOptions = tags.locator('.ts-dropdown [data-selectable]');
    await expect(tagOptions.first()).toBeVisible();
    expect(await tagOptions.count()).toBeGreaterThan(0);

    await tagOptions.first().click();
    await expect(tagsSelect.locator('option:checked')).toHaveCount(1);
    await expect(tags.locator('.ts-control .item')).toHaveCount(1);
  });
});

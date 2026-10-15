/**
 * Issue #848: the @tabler/core 1.6.1 bump (#595) left the desktop vertical
 * sidebar without a scroll container (.navbar-collapse is not part of this
 * layout) and left-aligned the brand. The sidebar embeds the mobile offcanvas
 * drawer, so the height chain must be bounded at >= 992px as well; the brand
 * lockup stays centered there.
 *
 * This spec pins both behaviors against the built stylesheets, matching the
 * static-harness style of the neighbouring layout specs.
 */
import { test, expect } from '@playwright/test';
import * as path from 'path';

const repoRoot = path.resolve(__dirname, '../../../../..');
const tablerCssPath = path.join(repoRoot, 'itambox/static/dist/vendor/tabler/css/tabler.min.css');
const appCssPath = path.join(repoRoot, 'itambox/static/dist/itambox.css');

async function loadAppStyles(page: import('@playwright/test').Page): Promise<void> {
  await page.addStyleTag({ path: tablerCssPath });
  await page.addStyleTag({ path: appCssPath });
}

function navItem(index: number): string {
  return (
    '<li class="nav-item"><a class="nav-link" href="#">'
    + '<span class="nav-link-icon d-md-none d-lg-inline-block"></span>'
    + `<span class="nav-link-title">Item ${index}</span></a></li>`
  );
}

async function renderSidebar(page: import('@playwright/test').Page, itemCount: number): Promise<void> {
  await page.setContent(`
    <div class="page">
      <aside class="navbar navbar-vertical navbar-expand-lg">
        <div class="container-fluid">
          <button class="navbar-toggler" type="button" aria-label="Toggle navigation"><span class="navbar-toggler-icon"></span></button>
          <h1 class="navbar-brand">
            <a href="#" class="d-flex align-items-center">
              <svg class="navbar-brand-image" style="height:36px;width:148px" viewBox="0 0 214 52" role="img" aria-label="ITAMbox">
                <rect width="214" height="52" fill="none"></rect>
              </svg>
            </a>
          </h1>
          <div class="offcanvas-lg offcanvas-start" tabindex="-1" id="sidebar-menu" aria-label="Navigation">
            <div class="offcanvas-body p-0 d-flex flex-column">
              <div class="flex-grow-1 overflow-auto">
                <ul class="navbar-nav pt-lg-3">
                  ${Array.from({ length: itemCount }, (_, i) => navItem(i + 1)).join('\n')}
                </ul>
              </div>
            </div>
          </div>
        </div>
      </aside>
      <div class="page-wrapper"></div>
    </div>
  `);
  await loadAppStyles(page);
}

async function brandCenterDelta(page: import('@playwright/test').Page): Promise<number> {
  return page.evaluate(() => {
    const aside = document.querySelector('aside.navbar-vertical')!.getBoundingClientRect();
    const brand = document.querySelector('.navbar-brand > a')!.getBoundingClientRect();
    return Math.abs((brand.x + brand.width / 2) - (aside.x + aside.width / 2));
  });
}

test('desktop sidebar scrolls under the pinned, centered brand', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 600 });
  await renderSidebar(page, 40);

  const brand = page.locator('.navbar-brand > a');
  const scrollRegion = page.locator('#sidebar-menu > .offcanvas-body > .flex-grow-1');

  // The brand lockup stays horizontally centered in the sidebar.
  const brandBox = await brand.boundingBox();
  expect(brandBox).not.toBeNull();
  expect(await brandCenterDelta(page)).toBeLessThanOrEqual(2);

  // The navigation region is bounded by the viewport, starts below the brand,
  // keeps a usable height, and is actually scrollable.
  const metrics = await scrollRegion.evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    rectTop: element.getBoundingClientRect().top,
    rectBottom: element.getBoundingClientRect().bottom,
    overflowY: getComputedStyle(element).overflowY,
  }));
  expect(metrics.overflowY).toBe('auto');
  expect(metrics.rectTop).toBeGreaterThanOrEqual(brandBox!.y + brandBox!.height - 1);
  expect(metrics.rectBottom).toBeLessThanOrEqual(601);
  expect(metrics.clientHeight).toBeGreaterThan(200);
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);

  // Scrolling to the end reaches the last entry while the brand stays pinned.
  await scrollRegion.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  const scrollTopAfter = await scrollRegion.evaluate((element) => element.scrollTop);
  expect(scrollTopAfter).toBeGreaterThan(0);
  expect(scrollTopAfter).toBeLessThanOrEqual(metrics.scrollHeight - metrics.clientHeight + 1);

  // The last entry sits fully inside the region's visible rectangle and is the
  // topmost element there, so it is not clipped away by an ancestor.
  const lastEntryHitTest = await page.evaluate(() => {
    const region = document.querySelector('#sidebar-menu > .offcanvas-body > .flex-grow-1')!;
    const item = document.querySelector('#sidebar-menu .navbar-nav > li:last-child')!;
    const regionRect = region.getBoundingClientRect();
    const itemRect = item.getBoundingClientRect();
    const hit = document.elementFromPoint(
      itemRect.left + itemRect.width / 2,
      Math.min(itemRect.bottom - 1, regionRect.bottom - 1),
    );
    return {
      topDelta: itemRect.top - regionRect.top,
      bottomDelta: itemRect.bottom - regionRect.bottom,
      hitIsInItem: hit !== null && (item === hit || item.contains(hit)),
    };
  });
  expect(lastEntryHitTest.topDelta).toBeGreaterThanOrEqual(-1);
  expect(lastEntryHitTest.bottomDelta).toBeLessThanOrEqual(1);
  expect(lastEntryHitTest.hitIsInItem).toBe(true);

  const brandBoxAfter = await brand.boundingBox();
  expect(brandBoxAfter).not.toBeNull();
  expect(Math.abs(brandBoxAfter!.y - brandBox!.y)).toBeLessThanOrEqual(1);
});

test('short menus fill the sidebar without a forced scrollbar', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 600 });
  await renderSidebar(page, 3);

  expect(await brandCenterDelta(page)).toBeLessThanOrEqual(2);
  const metrics = await page.locator('#sidebar-menu > .offcanvas-body > .flex-grow-1').evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    rectBottom: element.getBoundingClientRect().bottom,
  }));
  expect(metrics.clientHeight).toBeGreaterThan(200);
  expect(metrics.scrollHeight).toBeLessThanOrEqual(metrics.clientHeight);
  expect(metrics.rectBottom).toBeLessThanOrEqual(601);
  expect(metrics.rectBottom).toBeGreaterThanOrEqual(599);
});

test('the desktop chain applies at 992px and leaves the mobile drawer closed below it', async ({ page }) => {
  await page.setViewportSize({ width: 992, height: 600 });
  await renderSidebar(page, 40);

  const menu = page.locator('#sidebar-menu');
  const scrollRegion = page.locator('#sidebar-menu > .offcanvas-body > .flex-grow-1');

  // At the breakpoint the desktop chain is active: bounded and scrollable.
  await expect(menu).toBeVisible();
  const metrics = await scrollRegion.evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
  }));
  expect(metrics.clientHeight).toBeGreaterThan(200);
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);

  // Below the breakpoint the sidebar is the closed mobile drawer again and the
  // desktop chain must not force it visible.
  await page.setViewportSize({ width: 991, height: 600 });
  await expect(menu).toBeHidden();
});

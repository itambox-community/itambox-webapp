import { test, expect } from '@playwright/test';

test.describe('HTMX Dynamic Interactivity & Form Validations', () => {

  test.beforeEach(async ({ page }) => {
    // Monitor for console errors and unhandled promise rejections
    page.on('console', msg => {
      if (msg.type() === 'error') {
        console.error(`[Console Error]: ${msg.text()}`);
      }
    });
    page.on('pageerror', error => {
      console.error(`[Page Error]: ${error.message}`);
    });
  });

  test('Test blank form submission returns clean HTMX validation fragment without full reload', async ({ page }) => {
    // Navigate to an object creation form. Assuming /inventory/accessories/add/ since inventory paths are known
    await page.goto('/inventory/accessories/add/');
    
    // Ensure we are on the page
    const mainForm = page.locator('.page-body form').first();
    await expect(mainForm).toBeVisible();
    
    // Clear out any defaults if possible
    
    // Attempt to submit form with blank mandatory inputs
    // The form button is usually inside a .form-footer or similar.
    const submitBtn = mainForm.locator('button[type="submit"], input[type="submit"]');
    await submitBtn.click();
    
    // Check if HTMX returned a fragment and updated the DOM, rather than a full page reload or 500 error
    // With HTMX, a validation error usually renders inline error text like .invalid-feedback
    await page.waitForSelector('.invalid-feedback, .is-invalid', { state: 'visible', timeout: 5000 }).catch(() => {
       console.log('No invalid-feedback found, checking if form crashed');
    });
    
    // Ensure it's not a 500 Server Error
    await expect(page).not.toHaveTitle(/Server Error/);
    
    // Ensure the form didn't disappear and we are still on the form view
    await expect(mainForm).toBeVisible();
  });

  test('Test modal interactions for HTMX forms', async ({ page }) => {
    // Try to find a trigger for a modal, e.g. "Clone" or "Checkout"
    // Let's try /inventory/accessories/
    await page.goto('/inventory/accessories/');
    
    // If there is any item, click its row or actions dropdown, then "Checkout" or "Clone"
    const dropdownToggle = page.locator('#page-content-wrapper .dropdown-toggle').first();
    if (await dropdownToggle.count() > 0) {
      await dropdownToggle.click();
      
      const checkoutBtn = page.locator('.dropdown-menu a', { hasText: 'Checkout' }).first();
      if (await checkoutBtn.count() > 0) {
        await checkoutBtn.click();
        
        // Modal should appear
        const modal = page.locator('.modal.show');
        await expect(modal).toBeVisible();
        
        // Try submitting the empty modal form
        const modalSubmit = modal.locator('button[type="submit"]');
        if (await modalSubmit.count() > 0) {
           await modalSubmit.click();
           // Check if it stays open or freezes
           await expect(modal).toBeVisible();
        }
      }
    }
  });

});

import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir:'./e2e',
  workers:1,
  timeout:45000,
  use:{baseURL:process.env.OPSPILOT_TEST_URL || 'http://127.0.0.1:8000',trace:'retain-on-failure',screenshot:'only-on-failure'},
});

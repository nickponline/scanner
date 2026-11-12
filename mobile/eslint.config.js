// https://docs.expo.dev/guides/using-eslint/
const { defineConfig } = require('eslint/config');
const expoConfig = require('eslint-config-expo/flat');

module.exports = defineConfig([
  expoConfig,
  { ignores: ['ios/*', 'android/*', 'node_modules/*', '.expo/*'] },
  { files: ['**/__tests__/**', '**/testing/**'], languageOptions: { globals: { jest: 'readonly', describe: 'readonly', test: 'readonly', expect: 'readonly' } } },
]);

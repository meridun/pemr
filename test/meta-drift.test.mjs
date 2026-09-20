import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  cavemanHookMatchesL1,
  extractCavemanHookRule,
  extractCavemanSection,
} from '../scripts/check-meta-drift.mjs';

const RULE = 'Terse by default. No recap unless asked.';
const l1 = `# L1\n\n## Tone\n\nx\n\n## Caveman mode\n\nTerse by default. No recap\nunless asked.\nWired as a hook.\n\n## graphify\n\ny\n`;
const settings = (rule) => ({
  hooks: {
    UserPromptSubmit: [
      {
        hooks: [
          {
            type: 'command',
            command: `echo '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"Caveman mode: ${rule}"}}'`,
          },
        ],
      },
    ],
  },
});

test('extractCavemanSection returns only the caveman section', () => {
  const section = extractCavemanSection(l1);
  assert.match(section, /Terse by default/);
  assert.doesNotMatch(section, /graphify/);
  assert.equal(extractCavemanSection('# nothing here'), null);
});

test('extractCavemanHookRule pulls the rule text after the prefix', () => {
  assert.equal(extractCavemanHookRule(settings(RULE)), RULE);
  assert.equal(extractCavemanHookRule({ hooks: {} }), null);
});

test('cavemanHookMatchesL1 is whitespace-insensitive and detects drift', () => {
  assert.equal(cavemanHookMatchesL1(l1, settings(RULE)), true);
  assert.equal(cavemanHookMatchesL1(l1, settings('Terse by default. No recap ever.')), false);
});

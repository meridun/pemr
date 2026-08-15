#!/usr/bin/env node
// PII/PHI guard for a public repo. Enforces AGENTS.md §"Privacy posture":
// fixtures are synthetic only, and no real identity reaches code, commits, or
// issue/PR text.
//
// Allowlist-first, deliberately: real names are unguessable, but the synthetic
// cast is a short fixed list. Anything claiming to be a patient identity that
// is NOT on the roster is treated as a violation. Adding a persona is then a
// visible, reviewable edit to SYNTHETIC_ROSTER rather than a silent new name.
//
//   node scripts/pii-scan.mjs [paths...]   # CI mode; exit 1 on any finding
//   import { scanText } from './pii-scan.mjs'
//
// Escape hatch for a genuine false positive: put `pii-allow` on the same line.

import fs from 'fs';
import path from 'path';
import { execFileSync } from 'child_process';

/** The only identities permitted to appear in this repository. */
export const SYNTHETIC_ROSTER = {
  slugs: [
    'jane-doe', 'john-doe', 'kid-doe', 'bob-roe', 'john-roe', 'robert-roe',
    'ann-poe', 'ann-zed', 'jane-smith', 'karen-smith', 'michael-vu',
    'alex-carter', 'dana-roe', 'stranger-sam',
  ],
  // Surnames that mark a name as a placeholder (Doe/Roe/Poe/Zed family).
  surnames: [
    'doe', 'roe', 'poe', 'zed', 'smith', 'vu', 'carter', 'stranger', 'last',
    'fields',
  ],
  dobs: [
    '1962-03-14', '1971-09-09', '1955-11-02', '1900-03-14', '1990-09-09',
    '1981-03-07', '1980-01-01', '1890-01-01', '1955-01-02', '1900-01-01',
    // Day-first trap dates: deliberately NOT anyone's DOB. They exist so the
    // digit-boundary guard in `_person_matches` is actually exercised.
    '1981-07-23', '1981-07-13',
  ],
};

/**
 * Common given names. A `given-surname` token is what a person slug looks like,
 * and "starts with a given name" discriminates far better than any attempt to
 * enumerate technical vocabulary — that set is unbounded, this one is not.
 * Names that double as technical words (mark, bill, will, may, art, page) are
 * deliberately omitted; the `--person` rule still catches those in slug position.
 */
const GIVEN_NAMES = new Set([
  'aaron', 'abigail', 'adam', 'adrian', 'aiden', 'alan', 'albert', 'alex',
  'alexander', 'alexis', 'alice', 'alicia', 'allison', 'amanda', 'amber',
  'amelia', 'amy', 'andrea', 'andrew', 'angela', 'anna', 'anne', 'anthony',
  'antonio', 'ariana', 'arthur', 'ashley', 'aubrey', 'audrey', 'austin',
  'ava', 'barbara', 'beatrice', 'benjamin', 'bernard', 'beth', 'betty',
  'beverly', 'blake', 'bradley', 'brandon', 'brenda', 'brian', 'brianna',
  'bridget', 'brittany', 'brooke', 'bruce', 'bryan', 'caleb', 'cameron',
  'camila', 'carl', 'carlos', 'carol', 'caroline', 'carolyn', 'catherine',
  'charles', 'charlotte', 'chelsea', 'cheryl', 'chloe', 'christian',
  'christina', 'christine', 'christopher', 'claire', 'clara', 'colin',
  'connor', 'courtney', 'craig', 'crystal', 'cynthia', 'daniel', 'danielle',
  'david', 'deborah', 'debra', 'declan', 'denise', 'dennis', 'derek',
  'diana', 'diane', 'dominic', 'donald', 'donna', 'dorothy', 'douglas',
  'dylan', 'edward', 'eleanor', 'elena', 'eli', 'elijah', 'elizabeth',
  'ella', 'ellen', 'emily', 'emma', 'eric', 'erica', 'erin', 'ethan',
  'eugene', 'evelyn', 'felix', 'fiona', 'frances', 'francis', 'frank',
  'gabriel', 'gabriella', 'gary', 'george', 'gerald', 'gloria', 'gordon',
  'gregory', 'hannah', 'harold', 'harper', 'harry', 'hazel', 'heather',
  'helen', 'henry', 'holly', 'howard', 'hunter', 'ian', 'irene', 'isaac',
  'isabella', 'isaiah', 'ivan', 'jack', 'jackson', 'jacob', 'jacqueline',
  'james', 'jamie', 'jane', 'janet', 'janice', 'jared', 'jasmine', 'jason',
  'jeffrey', 'jennifer', 'jeremy', 'jerry', 'jesse', 'jessica', 'joan',
  'joanne', 'joel', 'john', 'jonathan', 'jordan', 'joseph', 'joshua',
  'joyce', 'juan', 'judith', 'judy', 'julia', 'julian', 'julie', 'justin',
  'kaitlyn', 'karen', 'katherine', 'kathleen', 'kathryn', 'katie', 'kayla',
  'keith', 'kelly', 'kenneth', 'kevin', 'kimberly', 'kyle', 'larry', 'laura',
  'lauren', 'lawrence', 'leah', 'leo', 'leonard', 'leslie', 'liam', 'lillian',
  'lily', 'linda', 'lisa', 'logan', 'lois', 'lorraine', 'louis', 'lucas',
  'lucy', 'luke', 'lydia', 'madison', 'marcus', 'margaret', 'maria',
  'marie', 'marilyn', 'mario', 'marjorie', 'martha', 'martin', 'mary',
  'mason', 'matthew', 'maureen', 'megan', 'melanie', 'melissa', 'micah',
  'michael', 'michelle', 'mildred', 'molly', 'monica', 'nancy', 'naomi',
  'natalie', 'nathan', 'neil', 'nicholas', 'nicole', 'noah', 'nora',
  'norman', 'olivia', 'oscar', 'owen', 'pamela', 'patricia', 'patrick',
  'paul', 'paula', 'pauline', 'peter', 'philip', 'phillip', 'phoebe',
  'rachel', 'ralph', 'randy', 'raymond', 'rebecca', 'regina', 'renee',
  'richard', 'robert', 'roberta', 'roger', 'ronald', 'rosemary', 'roy',
  'russell', 'ruth', 'ryan', 'samantha', 'samuel', 'sandra', 'sara',
  'sarah', 'scott', 'sean', 'sebastian', 'shannon', 'sharon', 'shawn',
  'sheila', 'shirley', 'sidney', 'simon', 'sophia', 'sophie', 'stanley',
  'stephanie', 'stephen', 'steven', 'susan', 'sylvia', 'tamara', 'tara',
  'teresa', 'terrance', 'thelma', 'theodore', 'theresa', 'thomas',
  'tiffany', 'timothy', 'tina', 'todd', 'tracy', 'travis', 'tyler',
  'valerie', 'vanessa', 'veronica', 'victor', 'victoria', 'vincent',
  'virginia', 'vivian', 'walter', 'wanda', 'wayne', 'wendy', 'wesley',
  'william', 'wyatt', 'xavier', 'yolanda', 'yvonne', 'zachary', 'zoe',
]);

/** Words that mark a line as talking about a person/record. */
const PERSON_CONTEXT = /\b(patient|person|roster|medication|meds|dob|repro|owner|family|slug|ingest|misfil|archive|record|chart|encounter)/i;

const ALLOW_MARKER = /pii-allow/;

const slugSet = new Set(SYNTHETIC_ROSTER.slugs);
const surnameSet = new Set(SYNTHETIC_ROSTER.surnames);
const dobSet = new Set(SYNTHETIC_ROSTER.dobs);

/** Normalize any of the renderings `dob_candidates` emits to ISO, else null. */
function isoDate(raw) {
  let m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(raw);
  if (m) return `${m[1]}-${String(m[2]).padStart(2, '0')}-${String(m[3]).padStart(2, '0')}`;
  m = /^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$/.exec(raw);
  if (!m) return null;
  const [, a, b, y] = m;
  // Ambiguous month/day: accept either reading as allowlisted.
  const mm = String(a).padStart(2, '0');
  const dd = String(b).padStart(2, '0');
  return [`${y}-${mm}-${dd}`, `${y}-${dd}-${mm}`];
}

function dobAllowed(raw) {
  const iso = isoDate(raw);
  if (!iso) return false;
  return Array.isArray(iso) ? iso.some((d) => dobSet.has(d)) : dobSet.has(iso);
}

const RULES = [
  {
    id: 'person-slug',
    // `--person X`, `person add X`, `add_person(conn, "X"`
    // A literal slug only — `<slug>`/`$VAR` placeholders and prose after
    // `--person` (as in "omit --person for everyone") are not identities.
    re: /(?:--person\s+|person add\s+|add_person\(\s*conn\s*,\s*["'])(?![<${])([a-z][a-z0-9-]{2,40})/g,
    check: (m) => {
      const slug = m[1];
      if (slugSet.has(slug)) return null;
      // Single-token slugs (`jane`, `zoe`, `typo`) are conventional test stubs and
      // carry no surname, so they cannot identify anyone. Only `first-last` shapes
      // assert a full identity — those must land on a placeholder surname.
      const parts = slug.split('-');
      if (parts.length < 2) return null;
      const surname = parts[parts.length - 1];
      if (surnameSet.has(surname)) return null;
      return `person slug '${slug}' is not on the synthetic roster`;
    },
  },
  {
    id: 'bare-slug',
    // A `given-surname` token in prose, with no `--person` in front of it. This
    // is how the real leak actually travelled: "Real repro (charlotte-dickinson,
    // medication omeprazole)". Context-gated so ordinary hyphenated compounds
    // (`fan-out`, `byte-identical`) stay quiet.
    re: /\b([a-z]{3,15})-([a-z]{3,15})\b/g,
    check: (m, line, inPersonDoc) => {
      const [full, given, surname] = m;
      if (slugSet.has(full)) return null;
      if (surnameSet.has(surname)) return null;
      if (!GIVEN_NAMES.has(given)) return null;
      // A path or filename (`exports/jane-journal.md`) is not a person reference.
      const before = line[m.index - 1];
      const after = line[m.index + full.length];
      if (before === '/' || before === '.' || before === '_' || after === '.' || after === '/') return null;
      return `'${full}' reads as a real person slug (surname '${surname}' is not a roster placeholder)`;
    },
  },
  {
    id: 'full-name',
    // `Michael Dickinson` in prose — the same identity as the slug, but spelled
    // out. Gated on person context so ordinary Capitalised Pairs stay quiet.
    // Match the whole capitalised run, so a middle name (`Robert Alan Roe`)
    // cannot hide the placeholder surname that ends it.
    re: /\b([A-Z][a-z]{2,14}(?:\s+[A-Z][a-z]{2,14}){1,3})\b/g,
    check: (m, line, inPersonDoc) => {
      const tokens = m[1].split(/\s+/);
      const given = tokens[0].toLowerCase();
      if (!GIVEN_NAMES.has(given)) return null;
      if (tokens.some((t) => surnameSet.has(t.toLowerCase()))) return null;
      if (!inPersonDoc) return null;
      const surname = tokens[tokens.length - 1];
      return `'${m[1]}' reads as a real person's name (surname '${surname}' is not a roster placeholder)`;
    },
  },
  {
    id: 'patient-name',
    // `Patient: SURNAME, FIRST` / `Patient Name: SURNAME, FIRST`
    re: /Patient(?:\s+Name)?\s*:\s*([A-Za-z'-]{2,20})\s*[,^]\s*([A-Za-z'-]{2,20})/g,
    check: (m) => (surnameSet.has(m[1].toLowerCase())
      ? null
      : `patient name '${m[1]}, ${m[2]}' is not a roster placeholder`),
  },
  {
    id: 'dob',
    re: /\b(?:DOB|D\.O\.B\.?|date of birth|birth ?date)\b\s*[:=]?\s*(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})/gi,
    check: (m) => (dobAllowed(m[1]) ? null : `DOB '${m[1]}' is not an allowlisted synthetic date`),
  },
  {
    id: 'ssn',
    re: /\b\d{3}-\d{2}-\d{4}\b/g,
    check: (m) => `looks like a US SSN: ${m[0]}`,
  },
  {
    id: 'mrn',
    re: /\bMRN\s*[:=#]\s*([0-9]{4,12})\b/gi,
    check: (m) => `MRN with a concrete value: ${m[0]}`,
  },
  {
    id: 'card',
    re: /\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6011)[ -]?\d{4}[ -]?\d{4}[ -]?\d{2,4}\b/g,
    check: (m) => `looks like a payment card number: ${m[0]}`,
  },
  {
    id: 'nhs',
    // Requires the label: a bare 10-digit run passes the checksum often enough
    // that GitHub comment IDs trip it, which would train people to ignore this.
    re: /\bNHS(?:\s*(?:number|no\.?))?\s*[:=#]?\s*(\d{3})[ -]?(\d{3})[ -]?(\d{4})\b/gi,
    check: (m) => {
      const d = (m[1] + m[2] + m[3]);
      let t = 0;
      for (let i = 0; i < 9; i++) t += Number(d[i]) * (10 - i);
      let c = 11 - (t % 11);
      if (c === 11) c = 0;
      if (c === 10 || c !== Number(d[9])) return null;
      return `passes the NHS-number checksum: ${m[0]}`;
    },
  },
];

/**
 * @param {string} text
 * @param {string} [label] file path or description, echoed in findings
 * @returns {{rule:string, line:number, message:string, excerpt:string}[]}
 */
export function scanText(text, label = '<text>') {
  const findings = [];
  if (!text) return findings;
  const lines = String(text).split(/\r?\n/);
  // Person context is judged over the whole document, not the single line: a
  // write-up names the patient in one sentence and the repro three lines down.
  const inPersonDoc = PERSON_CONTEXT.test(text);
  lines.forEach((line, i) => {
    if (ALLOW_MARKER.test(line)) return;
    // A literal `\n` in quoted source text glues to the next word (`...^\nDOB:`)
    // and defeats \b. Blank the escape to two spaces — same length, so match
    // offsets still line up with the original for the adjacency checks.
    const scanLine = line.replace(/\\[nrt]/g, '  ');
    for (const rule of RULES) {
      rule.re.lastIndex = 0;
      let m;
      while ((m = rule.re.exec(scanLine)) !== null) {
        const message = rule.check(m, scanLine, inPersonDoc);
        if (message) {
          findings.push({
            rule: rule.id,
            label,
            line: i + 1,
            message,
            excerpt: line.trim().slice(0, 160),
          });
        }
      }
    }
  });
  return findings;
}

/** Throws if `text` carries a patient identity. Used to gate outbound writes. */
export function assertClean(text, what = 'text') {
  const findings = scanText(text, what);
  if (findings.length === 0) return;
  const detail = findings.map((f) => `  [${f.rule}] line ${f.line}: ${f.message}`).join('\n');
  throw new Error(
    `refusing to publish ${what}: it contains a patient identity.\n${detail}\n` +
    'See AGENTS.md §"Privacy posture". Substitute a roster persona, or add `pii-allow` ' +
    'to the line if this is genuinely synthetic.',
  );
}

const SKIP_DIRS = new Set(['.git', 'node_modules', 'sources', 'exports', 'inbox', 'backups', 'data', 'vdm-diag', '.venv', 'pemr.egg-info']);
const TEXT_EXT = new Set(['.py', '.mjs', '.js', '.ts', '.md', '.json', '.toml', '.yml', '.yaml', '.txt', '.csv', '.sql', '.cfg', '.ini']);
// This file and its test necessarily contain the shapes they describe.
const SKIP_FILES = new Set(['scripts/pii-scan.mjs', 'test/pii-scan.test.mjs']);

function trackedFiles() {
  try {
    return execFileSync('git', ['ls-files'], { encoding: 'utf8' }).split(/\r?\n/).filter(Boolean);
  } catch {
    return [];
  }
}

function main(argv) {
  const explicit = argv.slice(2).filter((a) => !a.startsWith('-'));
  const files = (explicit.length ? explicit : trackedFiles()).filter((f) => {
    const norm = f.split(path.sep).join('/');
    if (SKIP_FILES.has(norm)) return false;
    if (norm.split('/').some((seg) => SKIP_DIRS.has(seg))) return false;
    return explicit.length ? true : TEXT_EXT.has(path.extname(f));
  });

  let all = [];
  for (const f of files) {
    let text;
    try {
      text = fs.readFileSync(f, 'utf8');
    } catch {
      continue;
    }
    all = all.concat(scanText(text, f));
  }

  if (all.length === 0) {
    console.log(`pii-scan: clean (${files.length} files)`);
    return 0;
  }
  console.error(`pii-scan: ${all.length} finding(s)\n`);
  for (const f of all) {
    console.error(`${f.label}:${f.line}  [${f.rule}] ${f.message}`);
    console.error(`    ${f.excerpt}`);
  }
  console.error('\nAGENTS.md §"Privacy posture": fixtures are synthetic only.');
  console.error('Add the persona to SYNTHETIC_ROSTER in scripts/pii-scan.mjs, or mark the line `pii-allow`.');
  return 1;
}

if (import.meta.url === `file://${process.argv[1]}` || process.argv[1]?.endsWith('pii-scan.mjs')) {
  process.exit(main(process.argv));
}

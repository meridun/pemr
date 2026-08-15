// Tests for scripts/pii-scan.mjs — the privacy guard behind AGENTS.md
// §"Privacy posture". Run with `npm test` (node --test).
//
// The positive cases below are the *shapes* that actually leaked into this
// public repo's issue tracker and test fixtures, reduced to minimal form. The
// negative cases are the shapes that tripped earlier drafts of the scanner —
// a guard nobody trusts is a guard people switch off, so false positives are
// treated as bugs with the same weight as misses.

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { scanText, assertClean, SYNTHETIC_ROSTER } from '../scripts/pii-scan.mjs';
import { guardOutboundBody } from '../scripts/sdlc.mjs';

const rules = (text) => scanText(text).map((f) => f.rule);

describe('pii-scan: catches real identities', () => {
  it('flags a person slug that is not on the roster', () => {
    assert.ok(rules('pemr ingest scan.pdf --person melanie-ashworth').includes('person-slug'));
  });

  it('flags a bare given-surname slug in prose — how the leak actually travelled', () => {
    const t = 'Real repro (melanie-ashworth, medication omeprazole): four stored rows.';
    assert.ok(rules(t).includes('bare-slug'));
  });

  it('flags a full name spelled out in prose', () => {
    const t = 'The patient record was filed under Gregory Ashworth by mistake.';
    assert.ok(rules(t).includes('full-name'));
  });

  it('flags a DOB that is not an allowlisted synthetic date', () => {
    assert.ok(rules('Patient: DOE, JOHN Q   DOB: 1968-02-11').includes('dob'));
  });

  it('sees through a literal \\n escape gluing the label to the previous token', () => {
    // `...^\nDOB:` reads as `nDOB` and defeats a naive word boundary.
    const t = 'PatientName = "STRANGER^SAM^\\nDOB: 1968-02-11"';
    assert.ok(rules(t).includes('dob'));
  });

  it('flags a patient name whose surname is not a placeholder', () => {
    assert.ok(rules('Patient: ASHWORTH, GREGORY J').includes('patient-name'));
  });

  it('flags SSNs, concrete MRNs and card numbers outright', () => {
    assert.ok(rules('SSN 123-45-6789').includes('ssn'));
    assert.ok(rules('MRN: 88213').includes('mrn'));
    assert.ok(rules('card 4111 1111 1111 1111').includes('card'));
  });
});

describe('pii-scan: stays quiet on synthetic and technical text', () => {
  it('accepts every roster persona', () => {
    for (const slug of SYNTHETIC_ROSTER.slugs) {
      assert.deepEqual(scanText(`--person ${slug}`), [], `roster slug ${slug} should pass`);
    }
  });

  it('accepts roster DOBs in any rendering dob_candidates emits', () => {
    for (const t of ['DOB: 1962-03-14', 'DOB 03/14/1962', 'DOB: 3/14/1962', 'DOB 03-14-1962']) {
      assert.deepEqual(scanText(t), [], `${t} should pass`);
    }
  });

  it('ignores ordinary hyphenated compounds even in person context', () => {
    const t = 'The patient record is byte-identical: fan-out, read-only, pre-existing, row-scoped.';
    assert.deepEqual(scanText(t), []);
  });

  it('ignores single-token test stubs, which identify nobody', () => {
    assert.deepEqual(scanText('persons.add_person(conn, "zoe", "Zoe")'), []);
    assert.deepEqual(scanText('pemr query meds --person jane --active'), []);
  });

  it('ignores placeholders and prose after --person', () => {
    assert.deepEqual(scanText('pemr document list [--person <slug>]'), []);
    assert.deepEqual(scanText('omit --person for everyone'), []);
  });

  it('ignores a path that merely contains a name', () => {
    assert.deepEqual(scanText('pemr render journal --person jane > exports/jane-journal.md'), []);
  });

  it('does not let a middle name hide the placeholder surname', () => {
    assert.deepEqual(scanText('BOB = Person(slug="bob-roe", full_name="Robert Alan Roe")'), []);
  });

  it('does not treat a 10-digit comment id as an NHS number', () => {
    assert.deepEqual(scanText('see issues/131#issuecomment-5300310204 for the audit'), []);
  });

  it('honours the pii-allow escape hatch', () => {
    assert.deepEqual(scanText('DOB: 1968-02-11  # pii-allow'), []);
  });
});

describe('assertClean', () => {
  it('throws with the offending rule named', () => {
    assert.throws(() => assertClean('Patient: DOE, JOHN Q  DOB: 1968-02-11', 'a comment'), /dob/);
  });

  it('passes clean text through silently', () => {
    assert.doesNotThrow(() => assertClean('sdlc:claim dispatch-20260815-0407 design'));
  });
});

describe('guardOutboundBody: the dispatcher cannot republish an identity', () => {
  it('refuses an issue comment carrying a real identity', () => {
    assert.throws(
      () => guardOutboundBody(['issue', 'comment', '61', '--body', 'repro: melanie-ashworth meds']),
      /patient identity/,
    );
  });

  it('allows an ordinary dispatcher status comment', () => {
    assert.doesNotThrow(
      () => guardOutboundBody(['issue', 'comment', '138', '--body', 'sdlc:claim run-1 verify']),
    );
  });

  it('ignores read-only gh invocations', () => {
    assert.doesNotThrow(
      () => guardOutboundBody(['issue', 'view', '61', '--json', 'labels']),
    );
  });
});

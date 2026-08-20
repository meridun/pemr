-- 013_person_unit_pref: one canonical **display** unit per person per measurement key
-- (issue #136).
--
-- The reporting case: a household whose documents state weight in `kg` for one person
-- and `lb` for another - and sometimes both for the *same* person, because two clinics
-- print two unit systems. Reading a summary then means converting in your head, and a
-- trend chart that interleaves 77.6 and 171.0 is a wrong chart.
--
-- The obvious fix - normalise the unit at `commit-extraction` time and migrate the
-- corpus once - was considered and **rejected** (the decision is on the issue). A
-- genuine unit-of-measure difference is not a spelling mistake: rewriting a stored
-- `kg` row into `lb` mutates a document-sourced fact, and no person's internally
-- consistent unit system is more correct than another's. So canonicalisation moves
-- entirely to **display** time and storage never mutates.
--
-- This table is therefore the first **display-only** lever in the schema, and the
-- distinction is load-bearing:
--
--   * Nothing **resolves through** it. `dedup`, `rekey`, `commit-extraction`, the
--     conflict resolver, `verify`'s identity checks and the render curation overlay all
--     never read it. `unit` is a non-key field for both `lab_result` and `observation`
--     (`dedup.KEY_FIELDS`), which is what makes a display-only lever possible without
--     touching identity at all.
--   * Only two read paths honour it: `render summary` and `query.trends`. Every
--     conversion they perform is disclosed on the line it changes, and the stored row
--     is untouched - a render stays a pure function of DB state, exactly as the 008
--     curation overlay is (Architecture.md 6).
--
-- `key` is a `dedup.key_token` (dictionary-normalised, qualifier-preserving) so a
-- preference set as `--key "A1c"` finds rows stored as `HbA1c` - the same token the
-- render's latest-vitals fold and `trends`'s series matching already group on. A later
-- `dictionary.toml` edit plus `rekey` can rename a family out from under a preference;
-- the failure is benign (the row simply renders in its stored unit) and needs no
-- retirement machinery, unlike 010's row-scoped verdicts.
--
-- `unit` is a `pemr.units` canonical unit id (`lb`, `kg`, `degF`, `cm`, ...), validated
-- in Python against that registry - fixed physics, deliberately NOT a `data/dictionary.toml`
-- section, which is user-grown medical *vocabulary* and an identity lever.
--
-- `ON DELETE CASCADE`, and `person_unit_pref` is deliberately **absent** from
-- `persons._CHILD_TABLES`: a display preference is not medical history, so it must never
-- be the reason a childless roster typo cannot be hard-removed.
--
-- Purely additive: CREATE TABLE only, no rebuild, safe under foreign_keys=ON. Readers
-- guard with `units.has_pref_table` (the `curation.has_table` / `records.has_edit_table`
-- precedent) so a restored pre-013 snapshot reports "no preferences" rather than raising
-- `no such table`.

CREATE TABLE person_unit_pref (
  person_id INTEGER NOT NULL REFERENCES person(person_id) ON DELETE CASCADE,
  key       TEXT NOT NULL,   -- dedup.key_token of the measurement key / analyte
  unit      TEXT NOT NULL,   -- a pemr.units canonical unit id; validated in Python
  set_at    TEXT NOT NULL,   -- ISO8601 UTC seconds
  PRIMARY KEY (person_id, key)
);

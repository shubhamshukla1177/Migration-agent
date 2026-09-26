-- 0007 v2: normalised MBI next to the value each feed sent, indexed for joins.
-- Source rows are left untouched. The 37 beneficiaries who arrived from both the
-- CCLF and EHR feeds need a member-merge (claims re-pointed, one row kept) as its
-- own reviewed migration before a UNIQUE constraint can go on mbi_normalized.
ALTER TABLE patients ADD COLUMN mbi_normalized char(11);

UPDATE patients SET mbi_normalized = upper(replace(mbi, '-', ''));

ALTER TABLE patients ALTER COLUMN mbi_normalized SET NOT NULL;

CREATE INDEX patients_mbi_normalized_idx ON patients (mbi_normalized);

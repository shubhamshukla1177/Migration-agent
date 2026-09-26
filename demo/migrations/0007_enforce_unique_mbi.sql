-- 0007: one row per Medicare beneficiary.
-- Normalise MBIs to CMS format (11 characters, upper case, no dashes) and add a
-- unique index so attribution never double-counts a member.
UPDATE patients
   SET mbi = upper(replace(mbi, '-', ''))
 WHERE mbi <> upper(replace(mbi, '-', ''));

CREATE UNIQUE INDEX patients_mbi_key ON patients (mbi);

-- 0008: tighten diagnoses.icd10_code.
-- The codes we bill most (I10, E11.9, N18.30, I25.10) fit in 6 characters; cap the
-- column so malformed feed values can't bloat the index.
ALTER TABLE diagnoses
    ALTER COLUMN icd10_code TYPE varchar(6) USING icd10_code::varchar(6);

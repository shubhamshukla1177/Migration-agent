#!/usr/bin/env python3
"""Seed a synthetic Medicare ACO claims database as your demo "prod", plus the gate's two roles.

  python demo/seed.py --admin-url postgresql://owner:...@host/appdb \
                      --reader-password ... --migrator-password ...

Everything is generated (deterministically): names, MBIs, NPIs, claims. No real PHI.

Roles created:
  app_owner      NOLOGIN, owns the tables
  gate_reader    LOGIN, SELECT only, default_transaction_read_only  -> READER_URL
  gate_migrator  LOGIN, member of app_owner (can run DDL/DML)        -> MIGRATOR_URL
"""
from __future__ import annotations

import argparse
import datetime as dt
import random

import psycopg
from psycopg import sql

SEED = 20260926
N_PATIENTS = 5000
N_DUPLICATES = 37          # beneficiaries who arrive from both the CCLF and the EHR feed
N_CLAIMS = 15000

MBI_ALPHA = "ACDEFGHJKMNPQRTUVWXY"          # CMS excludes S, L, O, I, B, Z
MBI_ALNUM = MBI_ALPHA + "0123456789"

FIRST = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda", "David", "Elizabeth",
         "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen",
         "Christopher", "Lisa", "Daniel", "Nancy", "Matthew", "Betty", "Anthony", "Margaret", "Mark", "Sandra",
         "Donald", "Ashley", "Steven", "Dorothy", "Paul", "Kimberly", "Andrew", "Emily", "Joshua", "Donna",
         "Kenneth", "Michelle", "Kevin", "Carol", "Brian", "Amanda", "George", "Melissa", "Edward", "Deborah",
         "Ronald", "Stephanie", "Timothy", "Rebecca", "Jason", "Sharon", "Jeffrey", "Laura", "Ryan", "Cynthia"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
        "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
        "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
        "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts"]

# ICD-10-CM codes stored WITH the dot. Short ones fit in 6 chars; long ones don't.
DX_SHORT = [("I10", 14), ("E11.9", 10), ("E78.5", 9), ("Z79.4", 5), ("N18.30", 5), ("I25.10", 5),
            ("J44.9", 5), ("I50.9", 4), ("E66.9", 4), ("F32.9", 3), ("M17.11", 3), ("G47.33", 3),
            ("K21.9", 3), ("E03.9", 3), ("I48.91", 3), ("R53.83", 2), ("N40.0", 2), ("D64.9", 2)]
DX_LONG = [("E11.649", 3), ("S72.001A", 2), ("C50.911", 2), ("J45.909", 2), ("E11.319", 1)]
P_LONG = 0.1035
DX_PER_CLAIM = [(1, 20), (2, 30), (3, 25), (4, 15), (5, 10)]
CLAIM_TYPES = [("professional", 70), ("outpatient", 20), ("inpatient", 6), ("snf", 4)]

DDL = """
DROP TABLE IF EXISTS diagnoses, claims, patients CASCADE;

CREATE TABLE patients (
    id              serial PRIMARY KEY,
    mbi             varchar(13) NOT NULL,          -- as received: CCLF sends 1EG4TE5MK72, EHR sends 1eg4-te5-mk72
    first_name      text NOT NULL,
    last_name       text NOT NULL,
    birth_date      date NOT NULL,
    source_feed     text NOT NULL CHECK (source_feed IN ('CCLF', 'EHR')),
    attributed_npi  char(10) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX patients_mbi_idx ON patients (mbi);

CREATE TABLE claims (
    id            serial PRIMARY KEY,
    patient_id    integer NOT NULL REFERENCES patients(id),
    claim_no      varchar(16) NOT NULL UNIQUE,
    rendering_npi char(10) NOT NULL,
    claim_type    text NOT NULL,
    service_date  date NOT NULL,
    paid_amount   numeric(10,2) NOT NULL
);
CREATE INDEX claims_patient_id_idx ON claims (patient_id);

CREATE TABLE diagnoses (
    id          serial PRIMARY KEY,
    claim_id    integer NOT NULL REFERENCES claims(id),
    seq         smallint NOT NULL,
    icd10_code  varchar(8) NOT NULL
);
CREATE INDEX diagnoses_claim_id_idx ON diagnoses (claim_id);
CREATE INDEX diagnoses_icd10_code_idx ON diagnoses (icd10_code);
"""


def weighted(rng, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def make_mbi(rng):
    d = lambda: str(rng.randint(0, 9))  # noqa: E731
    a = lambda: rng.choice(MBI_ALPHA)   # noqa: E731
    an = lambda: rng.choice(MBI_ALNUM)  # noqa: E731
    return str(rng.randint(1, 9)) + a() + an() + d() + a() + an() + d() + a() + a() + d() + d()


def ehr_format(mbi):
    return f"{mbi[:4]}-{mbi[4:7]}-{mbi[7:]}".lower()


def make_npi(rng):
    # 10 digits with a valid Luhn check digit over the 80840 prefix (CMS NPI rule)
    base = "1" + "".join(str(rng.randint(0, 9)) for _ in range(8))
    digits = [int(x) for x in "80840" + base]
    total = 0
    for i, v in enumerate(reversed(digits)):
        if i % 2 == 0:
            v *= 2
            v = v - 9 if v > 9 else v
        total += v
    return base + str((10 - total % 10) % 10)


def generate():
    rng = random.Random(SEED)
    npis = [make_npi(rng) for _ in range(60)]
    mbis = {"1EG4TE5MK72"}
    while len(mbis) < N_PATIENTS - N_DUPLICATES:
        mbis.add(make_mbi(rng))
    mbis = sorted(mbis)
    rng.shuffle(mbis)

    people = []
    for m in mbis:
        people.append({"mbi": m, "first": rng.choice(FIRST), "last": rng.choice(LAST),
                       "dob": dt.date(1935, 1, 1) + dt.timedelta(days=rng.randint(0, 365 * 25)),
                       "npi": rng.choice(npis)})
    rows = []
    for p in people:  # most arrive via CCLF, some via EHR only (dashed, lowercase)
        feed = "EHR" if rng.random() < 0.18 else "CCLF"
        rows.append((p["mbi"] if feed == "CCLF" else ehr_format(p["mbi"]), p["first"], p["last"], p["dob"],
                     feed, p["npi"]))
    # the problem: 37 CCLF beneficiaries also arrive from the EHR feed as separate rows
    cclf_idx = [i for i, r in enumerate(rows) if r[4] == "CCLF" and r[0] != "1EG4TE5MK72"]
    dup_idx = [next(i for i, r in enumerate(rows) if r[0] == "1EG4TE5MK72")] + rng.sample(cclf_idx, N_DUPLICATES - 1)
    for i in dup_idx:
        mbi, first, last, dob, _, npi = rows[i]
        rows.append((ehr_format(mbi), first.upper(), last.upper(), dob, "EHR", rng.choice(npis)))
    rng.shuffle(rows)

    claims, dx = [], []
    per_patient = [1] * N_PATIENTS
    for _ in range(N_CLAIMS - N_PATIENTS):
        per_patient[rng.randrange(N_PATIENTS)] += 1
    claim_id = 0
    for pid, k in enumerate(per_patient, 1):
        for _ in range(k):
            claim_id += 1
            ctype = weighted(rng, CLAIM_TYPES)
            amount = {"professional": (40, 400), "outpatient": (150, 2500),
                      "inpatient": (4000, 42000), "snf": (2000, 18000)}[ctype]
            claims.append((pid, f"CLM{claim_id:010d}", rng.choice(npis), ctype,
                           dt.date(2025, 1, 1) + dt.timedelta(days=rng.randint(0, 364)),
                           round(rng.uniform(*amount), 2)))
            for seq in range(1, weighted(rng, DX_PER_CLAIM) + 1):
                code = weighted(rng, DX_LONG) if rng.random() < P_LONG else weighted(rng, DX_SHORT)
                dx.append((claim_id, seq, code))
    return rows, claims, dx


def ensure_role(cur, name, login, password=None):
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    verb = "ALTER" if cur.fetchone() else "CREATE"
    q = sql.SQL("{} ROLE {} " + ("LOGIN PASSWORD {}" if login else "NOLOGIN")).format(
        sql.SQL(verb), sql.Identifier(name), *([sql.Literal(password)] if login else []))
    cur.execute(q)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-url", required=True)
    ap.add_argument("--reader-password", required=True)
    ap.add_argument("--migrator-password", required=True)
    args = ap.parse_args()

    patients, claims, dx = generate()
    with psycopg.connect(args.admin_url) as conn:
        cur = conn.cursor()
        db = cur.execute("SELECT current_database()").fetchone()[0]
        ensure_role(cur, "app_owner", login=False)
        ensure_role(cur, "gate_reader", login=True, password=args.reader_password)
        ensure_role(cur, "gate_migrator", login=True, password=args.migrator_password)
        cur.execute("SELECT pg_has_role(current_user, 'app_owner', 'USAGE')")
        if not cur.fetchone()[0]:
            cur.execute("GRANT app_owner TO CURRENT_USER")
        cur.execute("GRANT app_owner TO gate_migrator")
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO gate_reader, gate_migrator").format(sql.Identifier(db)))
        cur.execute("GRANT USAGE, CREATE ON SCHEMA public TO app_owner")
        cur.execute("GRANT USAGE ON SCHEMA public TO gate_reader")
        cur.execute("ALTER ROLE gate_reader SET default_transaction_read_only = on")

        cur.execute(DDL)
        with cur.copy("COPY patients (mbi, first_name, last_name, birth_date, source_feed, attributed_npi) "
                      "FROM STDIN") as cp:
            for r in patients:
                cp.write_row(r)
        with cur.copy("COPY claims (patient_id, claim_no, rendering_npi, claim_type, service_date, paid_amount) "
                      "FROM STDIN") as cp:
            for r in claims:
                cp.write_row(r)
        with cur.copy("COPY diagnoses (claim_id, seq, icd10_code) FROM STDIN") as cp:
            for r in dx:
                cp.write_row(r)
        for t in ("patients", "claims", "diagnoses"):
            cur.execute(sql.SQL("ALTER TABLE {} OWNER TO app_owner").format(sql.Identifier(t)))
        cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO gate_reader")
        cur.execute("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO gate_reader")
        cur.execute("ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public GRANT SELECT ON TABLES TO gate_reader")
        cur.execute("ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public "
                    "GRANT SELECT ON SEQUENCES TO gate_reader")
        cur.execute("ANALYZE patients, claims, diagnoses")

        n_p, n_c, n_d = (cur.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                         for t in ("patients", "claims", "diagnoses"))
        n_dup = cur.execute("SELECT count(*) FROM (SELECT upper(replace(mbi, '-', '')) FROM patients "
                            "GROUP BY 1 HAVING count(*) > 1) d").fetchone()[0]
        n_long = cur.execute("SELECT count(*) FROM diagnoses WHERE length(icd10_code) > 6").fetchone()[0]
        examples = [r[0] for r in cur.execute(
            "SELECT DISTINCT icd10_code FROM diagnoses WHERE length(icd10_code) > 6 ORDER BY 1").fetchall()]

    print(f"patients={n_p} claims={n_c} diagnoses={n_d}")
    print(f"  duplicate beneficiaries across feeds: {n_dup}")
    print(f"  dx codes longer than 6 chars ({', '.join(examples)}): {n_long}")
    print("roles: gate_reader (read-only), gate_migrator (member of app_owner)")


if __name__ == "__main__":
    main()

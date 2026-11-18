-- ===========================================================================
-- MedSafe app objects for PostgreSQL. The app runs this file every time it starts.
-- Everything here is safe to run again (IF NOT EXISTS / OR REPLACE).
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Search-as-you-type: trigram indexes make  name ILIKE '%text%'  fast, and
-- similarity() ranks the closest matches first.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS ix_drug_name_trgm       ON Drug              USING gin (GenericName gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_ingredient_name_trgm ON ActiveIngredients USING gin (IngredientName gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_disease_name_trgm    ON Disease           USING gin (Name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_patient_name_trgm    ON Patient           USING gin (Name gin_trgm_ops);

-- Partial indexes: only the rows the safety check and the review queue read.
CREATE INDEX IF NOT EXISTS ix_rx_approved_dates ON Prescription (DiagnosisID, PrescriptionStart, PrescriptionEnd)
    WHERE ReviewStatus = 'Approved';
CREATE INDEX IF NOT EXISTS ix_rx_pending ON Prescription (PrescriptionID) WHERE ReviewStatus = 'Pending';
CREATE INDEX IF NOT EXISTS ix_diagnosis_doctor ON Diagnosis (DoctorID);

-- Patient records A-Z: B-tree (PostgreSQL's B-tree is a B+ tree: sorted keys, linked
-- leaf pages) on (first letter, name). "Names starting with L" is one equality lookup
-- on the first column, and the matching leaf entries are already in name order, so
-- ORDER BY Name needs no sort; with LIMIT the scan stops early. Measured on 200k
-- synthetic patients: first 50 for a letter 29 ms -> 0.3 ms; letter counts 118 -> 43 ms.
-- The trigram index above serves the search box (text anywhere in the name) instead.
CREATE INDEX IF NOT EXISTS ix_patient_name_letter ON Patient ((upper(left(Name, 1))), Name);

-- ---------------------------------------------------------------------------
-- Who wrote each prescription. The prescriber is a fact about the prescription itself
-- (PrescriptionID -> PrescriberID): a doctor can prescribe under a diagnosis another doctor
-- made, so Diagnosis.DoctorID can't stand in for it. Existing rows are filled from the
-- diagnosing doctor, which is who wrote them in the demo data.
-- ---------------------------------------------------------------------------
ALTER TABLE Prescription ADD COLUMN IF NOT EXISTS PrescriberID INT REFERENCES Doctor(DoctorID);
UPDATE Prescription r SET PrescriberID = g.DoctorID
FROM Diagnosis g WHERE g.DiagnosisID = r.DiagnosisID AND r.PrescriberID IS NULL;
ALTER TABLE Prescription ALTER COLUMN PrescriberID SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_prescription_prescriber ON Prescription (PrescriberID);

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------
-- Every prescription with everything a screen needs to show it. DoctorID and doctor_name
-- are the PRESCRIBER (who may differ from the doctor who made the diagnosis).
CREATE OR REPLACE VIEW v_prescription_detail AS
SELECT r.PrescriptionID, r.DiagnosisID, r.PrescriptionTemplateID, r.PrescriptionStart, r.PrescriptionEnd,
       r.PharmacistID, r.ReviewStatus, r.ReviewedAt, r.ReviewNotes,
       g.NRIC, p.Name AS patient_name, p.DOB, p.Sex,
       r.PrescriberID AS DoctorID, doc.Name AS doctor_name, doc.Hospital,
       g.DiseaseID, ds.Name AS disease,
       d.DrugID, d.GenericName,
       t.dose_amount, t.dose_unit, t.frequency, t.route, t.dosage_form,
       ph.Name AS pharmacist_name
FROM Prescription r
JOIN Diagnosis g            ON g.DiagnosisID = r.DiagnosisID
JOIN Patient p              ON p.NRIC = g.NRIC
JOIN Doctor doc             ON doc.DoctorID = r.PrescriberID
JOIN Disease ds             ON ds.DiseaseID = g.DiseaseID
JOIN PrescriptionTemplate t ON t.PrescriptionTemplateID = r.PrescriptionTemplateID
JOIN Drug d                 ON d.DrugID = t.DrugID
LEFT JOIN Pharmacist ph     ON ph.PharmacistID = r.PharmacistID;

-- A medication is ACTIVE when approved and today is between its start and end
-- (a NULL end means ongoing).
CREATE OR REPLACE VIEW v_active_medications AS
SELECT * FROM v_prescription_detail
WHERE ReviewStatus = 'Approved'
  AND PrescriptionStart <= CURRENT_DATE
  AND (PrescriptionEnd IS NULL OR PrescriptionEnd >= CURRENT_DATE);

-- The pharmacist's queue.
CREATE OR REPLACE VIEW v_pending_reviews AS
SELECT * FROM v_prescription_detail WHERE ReviewStatus = 'Pending';

-- ---------------------------------------------------------------------------
-- Integrity rules beyond CHECK constraints (trigger)
--   * a reviewed prescription must name the pharmacist and the review time
--   * returning a prescription to the doctor needs a note
--   * approving a drug the patient is SEVERELY allergic to needs a note
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_check_review() RETURNS trigger AS $$
BEGIN
    IF NEW.ReviewStatus <> 'Pending' THEN
        IF NEW.PharmacistID IS NULL OR NEW.ReviewedAt IS NULL THEN
            RAISE EXCEPTION 'A reviewed prescription needs a pharmacist and a review time.'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.ReviewStatus = 'Rejected' AND coalesce(btrim(NEW.ReviewNotes), '') = '' THEN
            RAISE EXCEPTION 'Returning a prescription to the doctor needs a note.'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.ReviewStatus = 'Approved' AND coalesce(btrim(NEW.ReviewNotes), '') = '' AND EXISTS (
            SELECT 1
            FROM Diagnosis g
            JOIN IsAllergicTo a          ON a.NRIC = g.NRIC AND a.Severity = 'Severe'
            JOIN PrescriptionTemplate t  ON t.PrescriptionTemplateID = NEW.PrescriptionTemplateID
            JOIN Contains c              ON c.DrugID = t.DrugID AND c.IngredientID = a.IngredientID
            WHERE g.DiagnosisID = NEW.DiagnosisID) THEN
            RAISE EXCEPTION 'The patient has a severe allergy to this drug. Add a note explaining why it is being approved.'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_check_review ON Prescription;
CREATE TRIGGER trg_check_review
    BEFORE INSERT OR UPDATE OF ReviewStatus, ReviewNotes, PharmacistID, ReviewedAt ON Prescription
    FOR EACH ROW EXECUTE FUNCTION fn_check_review();

-- ---------------------------------------------------------------------------
-- Sign-in. One login per doctor or pharmacist. The table holds only what a login
-- adds (username, password hash); the name and workplace stay in Doctor/Pharmacist,
-- and the role is whichever ID is set, so neither can disagree with its source.
-- Username -> everything else, so the table is in BCNF.
-- Passwords are stored as salted scrypt hashes (werkzeug), never as text.
-- ---------------------------------------------------------------------------
-- Re-running the setup script drops Doctor/Pharmacist with CASCADE, which removes this
-- table's foreign keys but not its rows; start it again in that case.
DO $$
BEGIN
    IF to_regclass('appuser') IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                       WHERE conrelid = to_regclass('appuser') AND contype = 'f'
                         AND confrelid = to_regclass('doctor')) THEN
            DROP TABLE appuser;
        END IF;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS AppUser (
    Username     VARCHAR(50) PRIMARY KEY,
    PasswordHash TEXT NOT NULL,
    DoctorID     INT UNIQUE REFERENCES Doctor(DoctorID) ON DELETE CASCADE,
    PharmacistID INT UNIQUE REFERENCES Pharmacist(PharmacistID) ON DELETE CASCADE,
    CONSTRAINT ck_appuser_one_person CHECK (num_nonnulls(DoctorID, PharmacistID) = 1),
    CONSTRAINT ck_appuser_username CHECK (Username = lower(Username) AND Username ~ '^[a-z0-9._-]{3,50}$')
);

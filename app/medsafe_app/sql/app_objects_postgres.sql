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

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------
-- Every prescription with everything a screen needs to show it.
CREATE OR REPLACE VIEW v_prescription_detail AS
SELECT r.PrescriptionID, r.DiagnosisID, r.PrescriptionTemplateID, r.PrescriptionStart, r.PrescriptionEnd,
       r.PharmacistID, r.ReviewStatus, r.ReviewedAt, r.ReviewNotes,
       g.NRIC, p.Name AS patient_name, p.DOB, p.Sex,
       g.DoctorID, doc.Name AS doctor_name, doc.Hospital,
       g.DiseaseID, ds.Name AS disease,
       d.DrugID, d.GenericName,
       t.dose_amount, t.dose_unit, t.frequency, t.route, t.dosage_form,
       ph.Name AS pharmacist_name
FROM Prescription r
JOIN Diagnosis g            ON g.DiagnosisID = r.DiagnosisID
JOIN Patient p              ON p.NRIC = g.NRIC
JOIN Doctor doc             ON doc.DoctorID = g.DoctorID
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

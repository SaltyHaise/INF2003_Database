-- INF2003 G61: check the medsafety setup. Every row should say PASS.
USE medsafety;

WITH expected_counts AS (
    SELECT 1 AS sort, 'Drug' AS item, 2202 AS expected
    UNION ALL SELECT 2 AS sort, 'ActiveIngredients' AS item, 2008 AS expected
    UNION ALL SELECT 3 AS sort, 'Contains' AS item, 2697 AS expected
    UNION ALL SELECT 4 AS sort, 'SideEffects' AS item, 202 AS expected
    UNION ALL SELECT 5 AS sort, 'HasSideEffects' AS item, 941 AS expected
    UNION ALL SELECT 6 AS sort, 'DrugtoDrugInteraction' AS item, 6684 AS expected
    UNION ALL SELECT 7 AS sort, 'Patient' AS item, 50 AS expected
    UNION ALL SELECT 8 AS sort, 'Doctor' AS item, 8 AS expected
    UNION ALL SELECT 9 AS sort, 'Pharmacist' AS item, 5 AS expected
    UNION ALL SELECT 10 AS sort, 'Disease' AS item, 73 AS expected
    UNION ALL SELECT 11 AS sort, 'Diagnosis' AS item, 120 AS expected
    UNION ALL SELECT 12 AS sort, 'PrescriptionTemplate' AS item, 38 AS expected
    UNION ALL SELECT 13 AS sort, 'Prescription' AS item, 182 AS expected
    UNION ALL SELECT 14 AS sort, 'IsAllergicTo' AS item, 8 AS expected
    UNION ALL SELECT 15 AS sort, 'ToAddress' AS item, 481 AS expected
),
actual_counts AS (
    SELECT 'Drug' AS item, COUNT(*) AS actual FROM Drug
    UNION ALL SELECT 'ActiveIngredients' AS item, COUNT(*) AS actual FROM ActiveIngredients
    UNION ALL SELECT 'Contains' AS item, COUNT(*) AS actual FROM Contains
    UNION ALL SELECT 'SideEffects' AS item, COUNT(*) AS actual FROM SideEffects
    UNION ALL SELECT 'HasSideEffects' AS item, COUNT(*) AS actual FROM HasSideEffects
    UNION ALL SELECT 'DrugtoDrugInteraction' AS item, COUNT(*) AS actual FROM DrugtoDrugInteraction
    UNION ALL SELECT 'Patient' AS item, COUNT(*) AS actual FROM Patient
    UNION ALL SELECT 'Doctor' AS item, COUNT(*) AS actual FROM Doctor
    UNION ALL SELECT 'Pharmacist' AS item, COUNT(*) AS actual FROM Pharmacist
    UNION ALL SELECT 'Disease' AS item, COUNT(*) AS actual FROM Disease
    UNION ALL SELECT 'Diagnosis' AS item, COUNT(*) AS actual FROM Diagnosis
    UNION ALL SELECT 'PrescriptionTemplate' AS item, COUNT(*) AS actual FROM PrescriptionTemplate
    UNION ALL SELECT 'Prescription' AS item, COUNT(*) AS actual FROM Prescription
    UNION ALL SELECT 'IsAllergicTo' AS item, COUNT(*) AS actual FROM IsAllergicTo
    UNION ALL SELECT 'ToAddress' AS item, COUNT(*) AS actual FROM ToAddress
),
scenarios AS (
    SELECT 3 AS prescription_id, 'D1 no alert: on metformin, new amlodipine' AS scenario, 0 AS expect_interaction, 0 AS expect_allergy
    UNION ALL SELECT 6 AS prescription_id, 'D2 allergy: amoxicillin allergy, new amoxicillin' AS scenario, 0 AS expect_interaction, 1 AS expect_allergy
    UNION ALL SELECT 12 AS prescription_id, 'D3 interaction: on warfarin, new ibuprofen' AS scenario, 1 AS expect_interaction, 0 AS expect_allergy
    UNION ALL SELECT 17 AS prescription_id, 'D4 multiple: on warfarin + simvastatin, new clarithromycin' AS scenario, 1 AS expect_interaction, 0 AS expect_allergy
    UNION ALL SELECT 22 AS prescription_id, 'D5 multiple: ibuprofen allergy + on aspirin and digoxin, new ibuprofen' AS scenario, 1 AS expect_interaction, 1 AS expect_allergy
    UNION ALL SELECT 27 AS prescription_id, 'D6 interaction: on clopidogrel, new omeprazole' AS scenario, 1 AS expect_interaction, 0 AS expect_allergy
    UNION ALL SELECT 31 AS prescription_id, 'D7 interaction: on nitroglycerin, new sildenafil' AS scenario, 1 AS expect_interaction, 0 AS expect_allergy
    UNION ALL SELECT 36 AS prescription_id, 'D8 interaction: on spironolactone, new lisinopril' AS scenario, 1 AS expect_interaction, 0 AS expect_allergy
    UNION ALL SELECT 39 AS prescription_id, 'D9 interaction: on sertraline, new tramadol' AS scenario, 1 AS expect_interaction, 0 AS expect_allergy
    UNION ALL SELECT 41 AS prescription_id, 'D10 no alert: clarithromycin ended months ago, new simvastatin' AS scenario, 0 AS expect_interaction, 0 AS expect_allergy
    UNION ALL SELECT 42 AS prescription_id, 'D11 no alert: no medications, no allergies' AS scenario, 0 AS expect_interaction, 0 AS expect_allergy
    UNION ALL SELECT 43 AS prescription_id, 'D12 allergy: carbamazepine SJS on record, new carbamazepine' AS scenario, 0 AS expect_interaction, 1 AS expect_allergy
),
scenario_results AS (
    SELECT s.prescription_id, s.scenario, s.expect_interaction, s.expect_allergy,
           (SELECT COUNT(*)
            FROM Contains pc
            JOIN Diagnosis ad             ON ad.NRIC = dg.NRIC
            JOIN Prescription a           ON a.DiagnosisID = ad.DiagnosisID AND a.ReviewStatus = 'Approved'
                                         AND a.PrescriptionStart <= CURRENT_DATE
                                         AND (a.PrescriptionEnd IS NULL OR a.PrescriptionEnd >= CURRENT_DATE)
            JOIN PrescriptionTemplate at2 ON at2.PrescriptionTemplateID = a.PrescriptionTemplateID
            JOIN Contains ac              ON ac.DrugID = at2.DrugID
            JOIN DrugtoDrugInteraction x  ON x.IngredientID_A = LEAST(pc.IngredientID, ac.IngredientID)
                                         AND x.IngredientID_B = GREATEST(pc.IngredientID, ac.IngredientID)
            WHERE pc.DrugID = t.DrugID) AS interaction_alerts,
           (SELECT COUNT(*)
            FROM Contains pc
            JOIN IsAllergicTo al ON al.NRIC = dg.NRIC AND al.IngredientID = pc.IngredientID
            WHERE pc.DrugID = t.DrugID) AS allergy_alerts
    FROM scenarios s
    JOIN Prescription rx        ON rx.PrescriptionID = s.prescription_id AND rx.ReviewStatus = 'Pending'
    JOIN Diagnosis dg           ON dg.DiagnosisID = rx.DiagnosisID
    JOIN PrescriptionTemplate t ON t.PrescriptionTemplateID = rx.PrescriptionTemplateID
)
SELECT 'counts' AS check_type, e.item, CAST(e.expected AS CHAR) AS expected,
       CAST(COALESCE(a.actual, 0) AS CHAR) AS actual,
       CASE WHEN a.actual = e.expected THEN 'PASS' ELSE 'FAIL' END AS result, e.sort
FROM expected_counts e LEFT JOIN actual_counts a ON a.item = e.item
UNION ALL
SELECT 'scenario', s.scenario,
       CONCAT('interaction ', CASE WHEN s.expect_interaction = 1 THEN 'yes' ELSE 'no' END,
              ', allergy ', CASE WHEN s.expect_allergy = 1 THEN 'yes' ELSE 'no' END),
       COALESCE(CONCAT(r.interaction_alerts, ' interaction, ', r.allergy_alerts, ' allergy'), 'prescription missing'),
       CASE WHEN r.prescription_id IS NOT NULL
             AND (r.interaction_alerts > 0) = (s.expect_interaction = 1)
             AND (r.allergy_alerts > 0) = (s.expect_allergy = 1) THEN 'PASS' ELSE 'FAIL' END,
       100 + s.prescription_id
FROM scenarios s LEFT JOIN scenario_results r ON r.prescription_id = s.prescription_id
ORDER BY sort;

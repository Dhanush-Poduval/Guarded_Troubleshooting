-- Fields carried by the official deeplink catalog that the initial schema did not model.
--
-- The catalog entry nests a validation block. Of 578 entries, 138 carry a complete one
-- (deeplink, key, resultType, condition, value) and 432 carry only deeplink and key, so
-- the detail columns are nullable and a plan emits validationDeeplink only when the data
-- supports it.
--
-- catalog_id is the publisher's own identifier (DL-0001). It is kept so a row can be
-- traced back to the source file, and is not used for matching.

ALTER TABLE deeplink_catalog
    ADD COLUMN IF NOT EXISTS catalog_id             TEXT,
    ADD COLUMN IF NOT EXISTS control_type           INTEGER,
    ADD COLUMN IF NOT EXISTS validation_deeplink    TEXT,
    ADD COLUMN IF NOT EXISTS validation_key         TEXT,
    ADD COLUMN IF NOT EXISTS validation_result_type TEXT,
    ADD COLUMN IF NOT EXISTS validation_condition   TEXT,
    ADD COLUMN IF NOT EXISTS validation_value       TEXT;

CREATE INDEX IF NOT EXISTS deeplink_catalog_catalog_id_idx ON deeplink_catalog (catalog_id);

-- Constrained to the values the contract enums permit, so an unexpected value in a future
-- catalog surfaces at load time rather than as an invalid response.
ALTER TABLE deeplink_catalog
    DROP CONSTRAINT IF EXISTS deeplink_catalog_result_type_chk;
ALTER TABLE deeplink_catalog
    ADD CONSTRAINT deeplink_catalog_result_type_chk
    CHECK (validation_result_type IS NULL
           OR validation_result_type IN ('boolean', 'integer', 'str', 'float'));

ALTER TABLE deeplink_catalog
    DROP CONSTRAINT IF EXISTS deeplink_catalog_condition_chk;
ALTER TABLE deeplink_catalog
    ADD CONSTRAINT deeplink_catalog_condition_chk
    CHECK (validation_condition IS NULL
           OR validation_condition IN ('greater', 'equal', 'less'));

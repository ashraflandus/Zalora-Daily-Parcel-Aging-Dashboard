-- Flyway migration (OceanBase / MySQL dialect). All DDL lives here, never in code.
-- One JSON document per row, grouped by collection (snapshots, slaVersions, log, config, otp, ...),
-- mirroring the document store the dashboard used as a claude.ai artifact.

CREATE TABLE docs (
    collection  VARCHAR(64)  NOT NULL,
    doc_id      VARCHAR(191) NOT NULL,
    data        LONGTEXT     NOT NULL,
    updated_us  BIGINT       NOT NULL,
    updated_by  VARCHAR(255) NULL,
    PRIMARY KEY (collection, doc_id)
) DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_docs_updated ON docs (collection, updated_us);

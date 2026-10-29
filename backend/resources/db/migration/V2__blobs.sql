-- Flyway migration (OceanBase / MySQL dialect).
-- Raw daily Active Orders files uploaded on the dashboard, gzip-compressed.

CREATE TABLE blobs (
    id            VARCHAR(32)  NOT NULL,
    content_type  VARCHAR(128) NOT NULL,
    data          LONGBLOB     NOT NULL,
    size          BIGINT       NOT NULL,
    created_us    BIGINT       NOT NULL,
    created_by    VARCHAR(255) NULL,
    PRIMARY KEY (id)
);

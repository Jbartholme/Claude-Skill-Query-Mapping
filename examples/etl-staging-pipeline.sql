-- =====================================================
-- ETL Staging Pipeline: Multi-Batch Example
-- Demonstrates temp table and table variable detection
-- =====================================================

-- BATCH 1: Extract raw events into unindexed temp table
SELECT
    EventId,
    EventCode,
    EventTimestamp,
    EventData,
    SourceSystem,
    LoadedDate
INTO #StagedEvents
FROM stage.RawEvents
WHERE LoadedDate >= CAST(GETDATE() AS DATE) - 7
  AND EventCode IS NOT NULL;

GO

-- BATCH 2: Load lookup codes into table variable (unindexed)
DECLARE @LookupCodes TABLE (
    Code VARCHAR(10),
    Description VARCHAR(100)
);

INSERT INTO @LookupCodes (Code, Description)
SELECT
    Code,
    Description
FROM dbo.EventCodes
WHERE IsActive = 1;

GO

-- BATCH 3: Join temp table and table variable, output to final destination
SELECT
    se.EventId,
    se.EventCode,
    se.EventTimestamp,
    se.EventData,
    se.SourceSystem,
    lc.Description AS EventDescription,
    se.LoadedDate,
    GETDATE() AS FinalizedDate
INTO dbo.EventFacts
FROM #StagedEvents se
INNER JOIN @LookupCodes lc ON lc.Code = se.EventCode
WHERE se.EventTimestamp IS NOT NULL;

GO

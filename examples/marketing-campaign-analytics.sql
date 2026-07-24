-- Marketing Analytics Query: Campaign Performance Comparison (Q1 vs Q2)
-- Domain: mktg schema with Campaigns, Impressions, Conversions tables
-- Purpose: Compare Q1 and Q2 performance with near-duplicate CTE logic

WITH campaign_performance_q1 AS (
    SELECT
        c.CampaignID,
        c.Name AS CampaignName,
        COUNT(DISTINCT i.ImpressionID) AS TotalImpressions,
        SUM(i.Cost) AS TotalSpend,
        AVG(i.CPC) AS AvgCostPerClick
    FROM mktg.Campaigns c
    INNER JOIN mktg.Impressions i ON i.CampaignID = c.CampaignID
    WHERE i.ImpressionDate BETWEEN '2025-01-01' AND '2025-03-31'
    GROUP BY c.CampaignID, c.Name
),
campaign_performance_q2 AS (
    SELECT
        c.CampaignID,
        c.Name AS CampaignName,
        COUNT(DISTINCT i.ImpressionID) AS TotalImpressions,
        SUM(i.Cost) AS TotalSpend,
        AVG(i.CPC) AS AvgCostPerClick,
        c.Budget AS CampaignBudget
    FROM mktg.Campaigns c
    INNER JOIN mktg.Impressions i ON i.CampaignID = c.CampaignID
    WHERE i.ImpressionDate BETWEEN '2025-04-01' AND '2025-06-30'
    GROUP BY c.CampaignID, c.Name, c.Budget
)
SELECT
    q1.CampaignID,
    q1.CampaignName,
    q1.TotalImpressions AS Q1_Impressions,
    q1.TotalSpend AS Q1_Spend,
    q2.TotalImpressions AS Q2_Impressions,
    q2.TotalSpend AS Q2_Spend,
    CAST(
        CASE
            WHEN q1.TotalSpend = 0 THEN 0
            ELSE (q2.TotalSpend - q1.TotalSpend) / q1.TotalSpend * 100
        END AS DECIMAL(10,2)
    ) AS SpendVariance_Pct,
    COUNT(DISTINCT conv.ConversionID) AS TotalConversions
FROM campaign_performance_q1 q1
FULL OUTER JOIN campaign_performance_q2 q2
    ON q1.CampaignID = q2.CampaignID
LEFT JOIN mktg.Conversions conv
    ON COALESCE(q1.CampaignID, q2.CampaignID) = conv.CampaignID
    AND conv.ConversionDate BETWEEN '2025-01-01' AND '2025-06-30'
GROUP BY
    q1.CampaignID,
    q1.CampaignName,
    q1.TotalImpressions,
    q1.TotalSpend,
    q2.CampaignID,
    q2.TotalImpressions,
    q2.TotalSpend
ORDER BY q1.CampaignID;

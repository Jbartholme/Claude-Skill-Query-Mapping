-- Poorly-written T-SQL query for financial fraud detection
-- Contains multiple anti-patterns for demonstration

SELECT *
FROM fin.Transactions t
INNER JOIN fin.Accounts a ON a.AccountID = t.AccountID
INNER JOIN fin.FraudFlags f ON 1=1
WHERE YEAR(t.TransactionDate) = 2024
  AND t.Description LIKE '%suspicious%'
  AND UPPER(a.AccountStatus) = 'FROZEN'
  AND t.Amount > 5000
  AND a.RiskScore >= 75
GROUP BY t.TransactionID, a.AccountID, f.FlagID, t.Description, t.Amount, a.RiskScore, t.TransactionDate
HAVING COUNT(*) > 1
ORDER BY t.TransactionDate DESC;

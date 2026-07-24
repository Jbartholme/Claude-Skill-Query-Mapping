-- Cross-database HR/Payroll/Benefits reporting query.
-- Deliberately references the same schema.table name ("dbo.Employees") in
-- two DIFFERENT databases to prove they're tracked as distinct sources.
WITH active_headcount AS (
    SELECT e.EmployeeID, e.Name, e.DepartmentID
    FROM HR_DB.dbo.Employees e
    WHERE e.Terminated = 0
),
current_salaries AS (
    SELECT s.EmployeeID, s.AnnualSalary, s.EffectiveDate
    FROM Payroll_DB.dbo.Salaries s
    WHERE s.EffectiveDate = (
        SELECT MAX(s2.EffectiveDate)
        FROM Payroll_DB.dbo.Salaries s2
        WHERE s2.EmployeeID = s.EmployeeID
    )
),
benefit_enrollment AS (
    SELECT b.EmployeeID, b.PlanName, b.MonthlyPremium
    FROM Benefits_DB.enroll.Enrollments b
    WHERE b.Status = 'Active'
)
SELECT
    hc.Name,
    hc.DepartmentID,
    cs.AnnualSalary,
    be.PlanName,
    be.MonthlyPremium,
    dup.Name AS DuplicateHRLookup
FROM active_headcount hc
JOIN current_salaries cs ON cs.EmployeeID = hc.EmployeeID
LEFT JOIN benefit_enrollment be ON be.EmployeeID = hc.EmployeeID
LEFT JOIN HR_DB.dbo.Employees dup ON dup.EmployeeID = hc.EmployeeID;

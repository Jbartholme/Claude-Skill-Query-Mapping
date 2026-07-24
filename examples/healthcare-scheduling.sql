-- Healthcare Clinical Scheduling Query
-- Self-join on clinic.Providers (attending to supervisor)
-- Subquery filtering clinic.Patients for no-show history
-- Returns scheduled appointments with provider and supervisor details

SELECT
    a.AppointmentID,
    a.AppointmentDate,
    a.StartTime,
    a.EndTime,
    p.PatientID,
    p.PatientName,
    p.DateOfBirth,
    att.ProviderID AS AttendingProviderID,
    att.ProviderName AS AttendingProviderName,
    sup.ProviderID AS SupervisorProviderID,
    sup.ProviderName AS SupervisorProviderName,
    d.DepartmentID,
    d.DepartmentName,
    a.Status,
    a.Notes
FROM clinic.Appointments a
    INNER JOIN clinic.Patients p ON a.PatientID = p.PatientID
    INNER JOIN clinic.Providers att ON a.ProviderID = att.ProviderID
    INNER JOIN clinic.Providers sup ON att.SupervisorID = sup.ProviderID
    INNER JOIN clinic.Departments d ON att.DepartmentID = d.DepartmentID
WHERE
    a.AppointmentDate >= CAST(GETDATE() AS DATE)
    AND a.AppointmentDate < DATEADD(DAY, 30, CAST(GETDATE() AS DATE))
    AND a.Status = 'Scheduled'
    AND att.IsActive = 1
    AND p.PatientID NOT IN (
        SELECT DISTINCT a2.PatientID
        FROM clinic.Appointments a2
        WHERE a2.Status = 'NoShow'
            AND a2.AppointmentDate >= DATEADD(YEAR, -1, CAST(GETDATE() AS DATE))
    )
ORDER BY a.AppointmentDate, a.StartTime;

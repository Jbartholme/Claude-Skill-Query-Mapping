-- Manufacturing Equipment Assembly Hierarchy with Recursive CTE
-- Domain: equipment assembly tree with maintenance log rollup
WITH EquipmentHierarchy AS (
  -- Anchor member: top-level equipment (no parent)
  SELECT
    EquipmentID,
    ParentEquipmentID,
    Name,
    0 AS HierarchyLevel
  FROM mfg.Equipment
  WHERE ParentEquipmentID IS NULL

  UNION ALL

  -- Recursive member: child equipment (sub-assemblies)
  SELECT
    e.EquipmentID,
    e.ParentEquipmentID,
    e.Name,
    h.HierarchyLevel + 1
  FROM mfg.Equipment e
  INNER JOIN EquipmentHierarchy h ON e.ParentEquipmentID = h.EquipmentID
  WHERE e.ParentEquipmentID IS NOT NULL
)
SELECT
  h.EquipmentID,
  h.Name,
  h.HierarchyLevel,
  ml.LogID,
  ml.ServiceDate,
  ml.Notes
FROM EquipmentHierarchy h
LEFT JOIN mfg.MaintenanceLog ml ON h.EquipmentID = ml.EquipmentID
ORDER BY h.HierarchyLevel, h.EquipmentID, ml.ServiceDate;

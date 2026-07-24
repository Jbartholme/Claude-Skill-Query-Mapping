-- E-commerce Order Fulfillment Query
-- Shows inventory levels by warehouse and pending shipments requiring fulfillment

WITH InventoryByWarehouse AS (
    SELECT
        i.WarehouseId,
        i.ProductId,
        w.WarehouseName,
        w.Region,
        i.QuantityOnHand,
        i.QuantityReserved,
        i.QuantityOnHand - i.QuantityReserved AS AvailableQuantity
    FROM ecom.Inventory i
    INNER JOIN ecom.Warehouses w ON i.WarehouseId = w.WarehouseId
    WHERE i.QuantityOnHand > 0
),

PendingShipments AS (
    SELECT
        s.ShipmentId,
        s.ProductId,
        s.WarehouseId,
        s.RequestedQuantity,
        s.ShipmentStatus,
        p.ProductName,
        p.UnitPrice
    FROM ecom.Shipments s
    INNER JOIN ecom.Products p ON s.ProductId = p.ProductId
    WHERE s.ShipmentStatus IN ('Pending', 'Processing')
)

SELECT
    ps.ShipmentId,
    ps.ProductName,
    ps.UnitPrice,
    ps.RequestedQuantity,
    iw.WarehouseName,
    iw.Region,
    iw.AvailableQuantity,
    CASE
        WHEN iw.AvailableQuantity >= ps.RequestedQuantity THEN 'Ready to Ship'
        WHEN iw.AvailableQuantity > 0 THEN 'Partial Stock Available'
        ELSE 'Out of Stock'
    END AS FulfillmentStatus
FROM PendingShipments ps
LEFT JOIN InventoryByWarehouse iw
    ON ps.WarehouseId = iw.WarehouseId
    AND ps.ProductId = iw.ProductId
ORDER BY ps.ShipmentId, iw.Region;

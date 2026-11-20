# Business definitions

The rules every correct answer follows. The semantic model encodes them; the correct SQL in
`questions.yaml` implements them independently, against the raw tables.

## Time

- Every metric is dated by when the order was placed (`order_purchase_timestamp`).
- Years are calendar years. Today is 2018-10-17, the last timestamp in the data.

## Orders

| Term | Rule |
|---|---|
| Orders | Every order placed, any status |
| Delivered orders | Status `delivered` |
| Canceled orders | Status `canceled` |
| Cancellation rate | Canceled orders ÷ orders |

## Money (delivered orders only)

| Term | Rule |
|---|---|
| Revenue | Sum of item prices. Excludes freight |
| Freight | Sum of freight charged on items |
| Gross merchandise value | Revenue + freight |
| Payments | Sum of payment values (includes installment interest and vouchers) |
| Average order value | Revenue ÷ delivered orders |

## Customers

| Term | Rule |
|---|---|
| Customer | A person (`customer_unique_id`), never `customer_id`, which is new on every order |
| Customers | Distinct customers with at least one order |
| Repeat customers | Customers with two or more orders, all time |
| Repeat customer rate | Repeat customers ÷ customers, all time |

## Delivery (delivered orders with a delivery date)

| Term | Rule |
|---|---|
| Late | Delivery date is after the estimated delivery date, compared as dates |
| Late delivery rate | Late orders ÷ delivered orders with a delivery date |
| Delivery days | Calendar days from purchase date to delivery date, averaged |

## Reviews

| Term | Rule |
|---|---|
| Review score | One review per order: the latest by answer time |
| Average review score | Average over orders that have a review |

## Attributes

| Term | Rule |
|---|---|
| Product category | English name; Portuguese name when no translation exists |
| State | The customer state recorded on the order |

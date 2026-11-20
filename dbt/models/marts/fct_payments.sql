select p.payment_id, p.order_id, o.purchased_at, p.payment_type, p.amount
from {{ ref('stg_payments') }} p
join {{ ref('stg_orders') }} o using (order_id)

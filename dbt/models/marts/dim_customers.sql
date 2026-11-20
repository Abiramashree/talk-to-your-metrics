select
  customer_id,
  min(purchased_at) as first_order_at,
  count(*) as orders
from {{ ref('fct_orders') }}
group by 1

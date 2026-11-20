select
  o.order_id,
  c.customer_unique_id as customer_id,
  c.customer_state as state,
  o.order_status as status,
  o.purchased_at,
  o.delivered_at is not null and o.order_status = 'delivered' as has_delivery,
  case when o.order_status = 'delivered' and o.delivered_at is not null
       then cast(o.delivered_at as date) > o.estimated_delivery_date end as is_late,
  case when o.order_status = 'delivered' and o.delivered_at is not null
       then date_diff('day', cast(o.purchased_at as date), cast(o.delivered_at as date)) end as delivery_days,
  r.review_score
from {{ ref('stg_orders') }} o
join {{ ref('stg_customers') }} c using (customer_id)
left join {{ ref('stg_reviews') }} r using (order_id)

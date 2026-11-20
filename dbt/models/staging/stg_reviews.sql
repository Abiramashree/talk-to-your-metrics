-- One review per order: the latest by answer time (547 orders have more than one).
select order_id, review_score
from {{ source('raw', 'order_reviews') }}
qualify row_number() over (partition by order_id order by review_answer_timestamp desc) = 1

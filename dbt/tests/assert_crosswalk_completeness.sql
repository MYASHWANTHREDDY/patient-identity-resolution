-- Singular test: every conformance record must have a crosswalk entry (crosswalk.record_key
-- is separately tested `unique`, so this plus that together prove "exactly one").
select p.record_key
from {{ ref('patient_normalized') }} p
left join {{ source('serving_written', 'crosswalk') }} c on c.record_key = p.record_key
where c.record_key is null

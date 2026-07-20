{{ config(materialized='table') }}

{#
  Block size, measured on the *core* side only -- a huge core block is what would flood an
  asymmetric join (every match-path record sharing that key joins against all of it); a
  large match-path-side block doesn't have the same cost shape, since the core side of the
  join is what's actually large at this project's scale. Same
  reasoning as mdm.pipeline._MATCHPATH_BLOCKING_PASSES' docstring (the DuckDB-tier version
  of this same cap).
#}

select
    blocking_pass,
    block_key,
    count(*) as record_count,
    count(*) > {{ var('max_block_size') }} as is_oversized
from {{ ref('matchpath_block_keys') }}
where side = 'core'
group by 1, 2

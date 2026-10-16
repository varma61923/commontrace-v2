# Spatial object memory

Send `frontier spatial-update REQUEST.json` with `delta` and authenticated
`context`: tenant/site are mandatory and robot is optional. Delta fields are
`object_id`, `class`, `pose` (xyz or xyz+xyzw), `frame`, `robot_id`, `timestamp`,
`confidence`; optional fields are `embedding_ref`, `relations`, `keyframe`, and
content-addressed `media`. Supported relations are `located_in`, `on`, `near`,
`seen_at`. Adapters translate Hydra/HOV-SG/ConceptGraphs/DynaMem summaries into
this interchange; no dense geometry is transferred.

Each delta is a bitemporal fact plus a signed immutable object event. Pass
`correction_of` for a correction to the same owned object. `where_was` resolves
event time; `believed_at(robot,time)` restricts the record-time horizon so later
corrections cannot rewrite historical beliefs. `changes_since` uses record time.

MCP `spatial_where`, `spatial_history`, and `spatial_believed` return planner text,
pose/frame, integrity and keyframe/media references. Tenant/site ownership is
checked before object matching. The trusted hosting layer must derive context
from the authenticated caller; an arbitrary caller-supplied tenant string is
not authentication. Shared maps use `sharing_record` and the existing
organization-pinned Hub `PostgresJournal.put`; site is retained as a scope label.

Each delta is at most 4 KiB; a tenant can publish at most 64 KiB per rolling minute
per store. Historical query output is bounded by the signed journal's row/byte
limits. Keep high-rate geometry and full scene reconstruction on the robot.

# Two-server oblivious recall reference

`commontrace prove recall-oblivious` demonstrates a standard XOR two-server
PIR pattern over fixed-size, per-clearance memory blocks. Authorized clients
choose nearest public cluster centroids locally, create two XOR query shares,
send one share to each independently operated Hub replica, reconstruct selected
blocks, and rerank locally. Any one server's bitmap share is uniformly random
and independent of the selected block. A label hierarchy filters the database
before indexing; the Hub derives the clearance from the authenticated key via
its deployment-provided clearance resolver, never from the request body.

The reference leaks the selected cluster identifiers, number of clusters fetched,
cluster count, plus ordinary network metadata such as timing and client identity.
Every cluster is
padded to the same configured public block capacity (64 blocks by default), and
all positions are fetched in a batched request so record counts are hidden and
the reference does not require one network round trip per record. Privacy requires
the two replicas not to collude and to receive independently randomized shares.
The route is mounted at `/api/v1/oblivious/pir`; deployments must inject a
per-organization/per-clearance replica resolver and key-clearance resolver into
`hub.rest.add_rest_routes`, provision identical filtered block order at both
servers, and provide public centroids to authorized clients. The sample CLI uses
two local replicas over a JSON fixture. This implementation is not single-server
ORAM-over-HNSW (Compass) or verifiable PIR; `ORAMOverHNSWBackend` and
`VerifiablePIRBackend` are the typed adapter boundaries for those future
backends. It is unsuitable for robot control loops; the reference deliberately
fetches a full selected cluster and uses one network round trip per cluster.

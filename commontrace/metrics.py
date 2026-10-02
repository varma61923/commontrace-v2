"""CommonTrace OpenTelemetry Metrics Module.

Provides metrics collection with semantic conventions (memory-semconv v0.1.0).
Designed for minimal overhead with optional dependency on opentelemetry packages.

If OpenTelemetry is not installed, all metrics functions become no-ops.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any, Optional

# Try to import OpenTelemetry metrics - make it optional
try:
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource

    _OTEL_METRICS_AVAILABLE = True
except ImportError:
    _OTEL_METRICS_AVAILABLE = False
    metrics = None  # type: ignore
    MeterProvider = object  # type: ignore

# ---------------------------------------------------------------------------
# Semantic attribute constants (memory-semconv v0.1.0)
# ---------------------------------------------------------------------------

# Latency histograms (unit: ms)
MEMORY_OPERATION_DURATION = "memory.operation.duration"

# Item / data volume counters
MEMORY_ITEMS_STORED = "memory.items.stored"
MEMORY_ITEMS_RETRIEVED = "memory.items.retrieved"
MEMORY_ITEMS_DELETED = "memory.items.deleted"
MEMORY_ITEMS_UPDATED = "memory.items.updated"

# Query result distribution
MEMORY_QUERY_RESULT_COUNT = "memory.query.result_count"

# Data size (bytes)
MEMORY_DATA_BYTES_STORED = "memory.data.bytes.stored"
MEMORY_DATA_BYTES_RETRIEVED = "memory.data.bytes.retrieved"

# Vector index counters
MEMORY_VECTOR_SEARCHES = "memory.vector.searches"
MEMORY_VECTOR_INDEX_SIZE = "memory.vector.index.size"

# Graph counters
MEMORY_GRAPH_EDGES_ADDED = "memory.graph.edges.added"
MEMORY_GRAPH_NODES_ADDED = "memory.graph.nodes.added"
MEMORY_GRAPH_EDGES_DELETED = "memory.graph.edges.deleted"
MEMORY_GRAPH_NODES_DELETED = "memory.graph.nodes.deleted"

# Error counter
MEMORY_OPERATION_ERRORS = "memory.operation.errors"

# Cache metrics
MEMORY_CACHE_HITS = "memory.cache.hits"
MEMORY_CACHE_MISSES = "memory.cache.misses"
MEMORY_CACHE_SIZE = "memory.cache.size"

# System and knowledge graph gauges
PROCESS_MEMORY_USAGE = "process.memory.usage"
MEMORY_GRAPH_NODES_COUNT = "memory.graph.nodes.count"
MEMORY_GRAPH_EDGES_COUNT = "memory.graph.edges.count"
MEMORY_ACTIVE_TRACES_COUNT = "memory.active_traces.count"

# ---------------------------------------------------------------------------
# Null-object pattern for zero overhead when metrics are off
# ---------------------------------------------------------------------------


class _NullInstrument:
    """No-op stand-in for any OTel instrument."""

    def add(self, *args, **kwargs):
        pass

    def record(self, *args, **kwargs):
        pass

    def observe(self, *args, **kwargs):
        pass


_NULL = _NullInstrument()

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_meter: Optional[metrics.Meter] = None
_provider: Optional[MeterProvider] = None
_configured: bool = False
_config_lock = threading.Lock()

# Instruments (populated by setup_metrics)
_op_duration: Any = _NULL
_items_stored: Any = _NULL
_items_retrieved: Any = _NULL
_items_deleted: Any = _NULL
_items_updated: Any = _NULL
_query_result_count: Any = _NULL
_data_bytes_stored: Any = _NULL
_data_bytes_retrieved: Any = _NULL
_vector_searches: Any = _NULL
_vector_index_size: Any = _NULL
_graph_edges_added: Any = _NULL
_graph_nodes_added: Any = _NULL
_graph_edges_deleted: Any = _NULL
_graph_nodes_deleted: Any = _NULL
_op_errors: Any = _NULL
_cache_hits: Any = _NULL
_cache_misses: Any = _NULL
_cache_size: Any = _NULL
_process_memory: Any = _NULL
_graph_nodes_count: Any = _NULL
_graph_edges_count: Any = _NULL
_active_traces_count: Any = _NULL


def _ensure_instruments() -> None:
    """Create metric instruments against the current meter."""
    global _op_duration, _items_stored, _items_retrieved, _items_deleted
    global _items_updated, _query_result_count, _data_bytes_stored
    global _data_bytes_retrieved, _vector_searches, _vector_index_size
    global _graph_edges_added, _graph_nodes_added, _graph_edges_deleted
    global _graph_nodes_deleted, _op_errors, _cache_hits, _cache_misses, _cache_size

    if _meter is None or not _OTEL_METRICS_AVAILABLE:
        return

    # Latency histogram
    _op_duration = _meter.create_histogram(
        name=MEMORY_OPERATION_DURATION,
        description="Duration of memory operations",
        unit="ms",
    )

    # Item counters
    _items_stored = _meter.create_counter(
        name=MEMORY_ITEMS_STORED,
        description="Total number of memory items stored",
        unit="{item}",
    )
    _items_retrieved = _meter.create_counter(
        name=MEMORY_ITEMS_RETRIEVED,
        description="Total number of memory items retrieved",
        unit="{item}",
    )
    _items_deleted = _meter.create_counter(
        name=MEMORY_ITEMS_DELETED,
        description="Total number of memory items deleted",
        unit="{item}",
    )
    _items_updated = _meter.create_counter(
        name=MEMORY_ITEMS_UPDATED,
        description="Total number of memory items updated",
        unit="{item}",
    )

    # Query result histogram
    _query_result_count = _meter.create_histogram(
        name=MEMORY_QUERY_RESULT_COUNT,
        description="Distribution of result counts per query",
        unit="{item}",
    )

    # Data size counters
    _data_bytes_stored = _meter.create_counter(
        name=MEMORY_DATA_BYTES_STORED,
        description="Total bytes stored into the memory system",
        unit="By",
    )
    _data_bytes_retrieved = _meter.create_counter(
        name=MEMORY_DATA_BYTES_RETRIEVED,
        description="Total bytes retrieved from the memory system",
        unit="By",
    )

    # Vector metrics
    _vector_searches = _meter.create_counter(
        name=MEMORY_VECTOR_SEARCHES,
        description="Total number of vector similarity searches executed",
        unit="{search}",
    )
    _vector_index_size = _meter.create_observable_gauge(
        callbacks=[_get_vector_index_size_callback],
        name=MEMORY_VECTOR_INDEX_SIZE,
        description="Current size of the vector index",
        unit="{item}",
    )

    # Graph metrics
    _graph_edges_added = _meter.create_counter(
        name=MEMORY_GRAPH_EDGES_ADDED,
        description="Total graph edges added to the knowledge graph",
        unit="{edge}",
    )
    _graph_nodes_added = _meter.create_counter(
        name=MEMORY_GRAPH_NODES_ADDED,
        description="Total graph nodes added to the knowledge graph",
        unit="{node}",
    )
    _graph_edges_deleted = _meter.create_counter(
        name=MEMORY_GRAPH_EDGES_DELETED,
        description="Total graph edges deleted from the knowledge graph",
        unit="{edge}",
    )
    _graph_nodes_deleted = _meter.create_counter(
        name=MEMORY_GRAPH_NODES_DELETED,
        description="Total graph nodes deleted from the knowledge graph",
        unit="{node}",
    )

    # Error counter
    _op_errors = _meter.create_counter(
        name=MEMORY_OPERATION_ERRORS,
        description="Total number of memory operation errors",
        unit="{error}",
    )

    # Cache metrics
    _cache_hits = _meter.create_counter(
        name=MEMORY_CACHE_HITS,
        description="Total number of cache hits",
        unit="{hit}",
    )
    _cache_misses = _meter.create_counter(
        name=MEMORY_CACHE_MISSES,
        description="Total number of cache misses",
        unit="{miss}",
    )
    _cache_size = _meter.create_observable_gauge(
        callbacks=[_get_cache_size_callback],
        name=MEMORY_CACHE_SIZE,
        description="Current size of the cache",
        unit="{item}",
    )
    _process_memory = _meter.create_observable_gauge(
        callbacks=[_get_process_memory_callback],
        name=PROCESS_MEMORY_USAGE,
        description="Process memory RSS in bytes",
        unit="By",
    )
    _graph_nodes_count = _meter.create_observable_gauge(
        callbacks=[_get_graph_counts_callback],
        name=MEMORY_GRAPH_NODES_COUNT,
        description="Total node and edge counts in knowledge graph",
        unit="{item}",
    )
    _active_traces_count = _meter.create_observable_gauge(
        callbacks=[_get_active_traces_callback],
        name=MEMORY_ACTIVE_TRACES_COUNT,
        description="Number of in-memory active traces",
        unit="{trace}",
    )


_vector_index_size_provider: Callable[[], int] | None = None
_vector_provider_lock = threading.Lock()


def set_vector_index_size_provider(provider: Callable[[], int] | None) -> None:
    """Register a custom callback or callable to report the current vector index size."""
    global _vector_index_size_provider
    with _vector_provider_lock:
        _vector_index_size_provider = provider


_cache_size_provider: Callable[[], int] | None = None
_cache_provider_lock = threading.Lock()


def set_cache_size_provider(provider: Callable[[], int] | None) -> None:
    """Register a custom callback or callable to report the current cache size."""
    global _cache_size_provider
    with _cache_provider_lock:
        _cache_size_provider = provider


def _get_vector_index_size_callback(options: Any) -> list[Any]:
    """Production callback for vector index size observable gauge."""
    from opentelemetry.metrics import Observation

    with _vector_provider_lock:
        provider = _vector_index_size_provider

    if provider is not None:
        try:
            val = provider()
            return [Observation(int(val), {"source": "registered_provider"})]
        except Exception:
            pass

    try:
        from commontrace import semantic_arm
        if hasattr(semantic_arm, "_INDEX") and semantic_arm._INDEX:
            total_items = 0
            for _identity, index_tuple in semantic_arm._INDEX.values():
                if len(index_tuple) >= 3 and hasattr(index_tuple[2], "__len__"):
                    total_items += len(index_tuple[2])
                elif len(index_tuple) >= 2 and hasattr(index_tuple[1], "shape"):
                    total_items += index_tuple[1].shape[0]
            if total_items > 0:
                return [Observation(total_items, {"source": "semantic_arm_cache"})]
    except Exception:
        pass

    try:
        from commontrace import paths
        ipath = os.path.join(paths.memory_dir("."), "attention", "index.npz")
        if os.path.isfile(ipath):
            import numpy as np
            with np.load(ipath, mmap_mode="r", allow_pickle=False) as data:
                if "slugs" in data:
                    count = int(data["slugs"].shape[0])
                    return [Observation(count, {"source": "filesystem"})]
    except Exception:
        pass

    return [Observation(0, {"source": "empty"})]


def _get_cache_size_callback(options: Any) -> list[Any]:
    """Production callback for cache size observable gauge."""
    from opentelemetry.metrics import Observation

    with _cache_provider_lock:
        provider = _cache_size_provider

    if provider is not None:
        try:
            val = provider()
            return [Observation(int(val), {"source": "registered_provider"})]
        except Exception:
            pass

    observations = []
    try:
        from commontrace import retrieval
        if hasattr(retrieval, "_INDEX_CACHE") and retrieval._INDEX_CACHE:
            observations.append(Observation(len(retrieval._INDEX_CACHE), {"cache.layer": "retrieval_index"}))
    except Exception:
        pass

    try:
        from commontrace import semantic_arm
        if hasattr(semantic_arm, "_INDEX") and semantic_arm._INDEX:
            observations.append(Observation(len(semantic_arm._INDEX), {"cache.layer": "semantic_arm"}))
    except Exception:
        pass

    try:
        from commontrace import lesson_cache, paths
        cpath = lesson_cache.cache_path(paths.memory_dir("."))
        if os.path.isfile(cpath):
            entries = lesson_cache._read_cache(cpath)
            lc_count = len(entries.get("entries", entries)) if isinstance(entries, dict) else 0
            observations.append(Observation(lc_count, {"cache.layer": "lesson_cache"}))
    except Exception:
        pass

    if observations:
        return observations
    return [Observation(0, {"source": "empty"})]


def _get_process_memory_callback(options: Any) -> list[Any]:
    """Observable gauge callback collecting process memory RSS in bytes."""
    import resource

    from opentelemetry.metrics import Observation
    try:
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        rss_bytes = rusage.ru_maxrss * 1024
        return [Observation(rss_bytes, {"memory.type": "rss"})]
    except Exception:
        return [Observation(0, {})]


def _get_graph_counts_callback(options: Any) -> list[Any]:
    """Observable gauge callback collecting graph node and edge counts."""
    from opentelemetry.metrics import Observation
    try:
        from commontrace import graph, paths
        root = paths.memory_dir(".")
        nodes = graph.load_nodes(root)
        edges = graph.load_edges(root)
        return [
            Observation(len(nodes), {"entity": "node"}),
            Observation(len(edges), {"entity": "edge"}),
        ]
    except Exception:
        return [Observation(0, {"status": "unavailable"})]


def _get_active_traces_callback(options: Any) -> list[Any]:
    """Observable gauge callback collecting active/in-memory trace count."""
    from opentelemetry.metrics import Observation
    try:
        from commontrace import tracing
        exporter = tracing.get_exporter()
        if exporter is not None and hasattr(exporter, "get_all_traces"):
            return [Observation(len(exporter.get_all_traces()), {})]
    except Exception:
        pass
    return [Observation(0, {})]


def setup_metrics(service_name: str = "commontrace",
                  service_version: str = "2.0.0",
                  console_output: bool = False,
                  otlp_endpoint: str | None = None) -> Optional[metrics.Meter]:
    """Configure OTel MeterProvider for CommonTrace.

    This function is idempotent - calling it multiple times will not
    reconfigure metrics.

    Args:
        service_name: Service name for OpenTelemetry resource
        service_version: Service version
        console_output: If True, export metrics to console for debugging
        otlp_endpoint: Optional OTLP endpoint for remote export

    Returns:
        The configured meter, or None if OpenTelemetry is not available
    """
    global _meter, _provider, _configured

    if not _OTEL_METRICS_AVAILABLE:
        return None

    with _config_lock:
        if _configured:
            return _meter

        deployment_env = (
            os.getenv("DEPLOYMENT_ENVIRONMENT")
            or os.getenv("ENVIRONMENT")
            or "production"
        )
        resource = Resource.create({
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment": deployment_env,
        })

        # Create metric readers
        readers = []

        if console_output:
            console_exporter = ConsoleMetricExporter()
            readers.append(
                PeriodicExportingMetricReader(
                    console_exporter,
                    export_interval_millis=60_000,
                )
            )

        if otlp_endpoint:
            _try_add_otlp_metric_reader(readers, otlp_endpoint)

        # Create provider
        _provider = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(_provider)

        # Get meter
        _meter = _provider.get_meter("commontrace", service_version)
        _ensure_instruments()
        _configured = True

        return _meter


def _try_add_otlp_metric_reader(readers: list, endpoint: str) -> None:
    """Try to add OTLP metric reader; silently fails if package not installed."""
    import logging

    logger = logging.getLogger("commontrace.metrics")

    try:
        # Try HTTP exporter first (more compatible)
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter as OTLPHttpMetricExporter,
        )
        exporter = OTLPHttpMetricExporter(endpoint=endpoint)
        readers.append(
            PeriodicExportingMetricReader(
                exporter,
                export_interval_millis=30_000,
            )
        )
        logger.info("OTel: OTLP HTTP metric exporter registered → %s", endpoint)
    except ImportError:
        try:
            # Fall back to gRPC
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
            exporter = OTLPMetricExporter(endpoint=endpoint)
            readers.append(
                PeriodicExportingMetricReader(
                    exporter,
                    export_interval_millis=30_000,
                )
            )
            logger.info("OTel: OTLP gRPC metric exporter registered → %s", endpoint)
        except ImportError:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but OTLP metric exporter "
                "packages are not installed. Install with: pip install opentelemetry-exporter-otlp"
            )


def get_meter() -> Optional[metrics.Meter]:
    """Get the configured meter, or None if not configured."""
    return _meter


def shutdown_metrics() -> None:
    """Shutdown metrics and reset all instruments to null stand-ins."""
    global _meter, _provider, _configured
    global _op_duration, _items_stored, _items_retrieved, _items_deleted
    global _items_updated, _query_result_count, _data_bytes_stored
    global _data_bytes_retrieved, _vector_searches, _vector_index_size
    global _graph_edges_added, _graph_nodes_added, _graph_edges_deleted
    global _graph_nodes_deleted, _op_errors, _cache_hits, _cache_misses, _cache_size
    global _process_memory, _graph_nodes_count, _graph_edges_count, _active_traces_count

    with _config_lock:
        if _provider:
            try:
                _provider.shutdown()
            except Exception:
                pass
        _meter = None
        _provider = None
        _configured = False

        _op_duration = _NULL
        _items_stored = _NULL
        _items_retrieved = _NULL
        _items_deleted = _NULL
        _items_updated = _NULL
        _query_result_count = _NULL
        _data_bytes_stored = _NULL
        _data_bytes_retrieved = _NULL
        _vector_searches = _NULL
        _vector_index_size = _NULL
        _graph_edges_added = _NULL
        _graph_nodes_added = _NULL
        _graph_edges_deleted = _NULL
        _graph_nodes_deleted = _NULL
        _op_errors = _NULL
        _cache_hits = _NULL
        _cache_misses = _NULL
        _cache_size = _NULL
        _process_memory = _NULL
        _graph_nodes_count = _NULL
        _graph_edges_count = _NULL
        _active_traces_count = _NULL


# ---------------------------------------------------------------------------
# Public metric helpers (no-op when not configured)
# ---------------------------------------------------------------------------

def record_operation_duration(duration_ms: float, attributes: dict | None = None) -> None:
    """Record the duration of a memory operation.

    Args:
        duration_ms: Duration in milliseconds
        attributes: Optional attributes to attach to the metric
    """
    _op_duration.record(duration_ms, attributes or {})


def increment_items_stored(count: int = 1, attributes: dict | None = None) -> None:
    """Increment the items stored counter.

    Args:
        count: Number of items to add (default: 1)
        attributes: Optional attributes to attach to the metric
    """
    _items_stored.add(count, attributes or {})


def increment_items_retrieved(count: int = 1, attributes: dict | None = None) -> None:
    """Increment the items retrieved counter.

    Args:
        count: Number of items to add (default: 1)
        attributes: Optional attributes to attach to the metric
    """
    _items_retrieved.add(count, attributes or {})


def increment_items_deleted(count: int = 1, attributes: dict | None = None) -> None:
    """Increment the items deleted counter.

    Args:
        count: Number of items to add (default: 1)
        attributes: Optional attributes to attach to the metric
    """
    _items_deleted.add(count, attributes or {})


def increment_items_updated(count: int = 1, attributes: dict | None = None) -> None:
    """Increment the items updated counter.

    Args:
        count: Number of items to add (default: 1)
        attributes: Optional attributes to attach to the metric
    """
    _items_updated.add(count, attributes or {})


def record_query_results(count: int, attributes: dict | None = None) -> None:
    """Record the number of results returned by a query.

    Args:
        count: Number of results
        attributes: Optional attributes to attach to the metric
    """
    _query_result_count.record(count, attributes or {})


def increment_bytes_stored(byte_count: int, attributes: dict | None = None) -> None:
    """Increment the bytes stored counter.

    Args:
        byte_count: Number of bytes to add
        attributes: Optional attributes to attach to the metric
    """
    _data_bytes_stored.add(byte_count, attributes or {})


def increment_bytes_retrieved(byte_count: int, attributes: dict | None = None) -> None:
    """Increment the bytes retrieved counter.

    Args:
        byte_count: Number of bytes to add
        attributes: Optional attributes to attach to the metric
    """
    _data_bytes_retrieved.add(byte_count, attributes or {})


def increment_vector_searches(attributes: dict | None = None) -> None:
    """Increment the vector searches counter.

    Args:
        attributes: Optional attributes to attach to the metric
    """
    _vector_searches.add(1, attributes or {})


def increment_graph_edges(count: int = 1, attributes: dict | None = None) -> None:
    """Increment the graph edges added counter.

    Args:
        count: Number of edges to add (default: 1)
        attributes: Optional attributes to attach to the metric
    """
    _graph_edges_added.add(count, attributes or {})


def increment_graph_nodes(count: int = 1, attributes: dict | None = None) -> None:
    """Increment the graph nodes added counter.

    Args:
        count: Number of nodes to add (default: 1)
        attributes: Optional attributes to attach to the metric
    """
    _graph_nodes_added.add(count, attributes or {})


def increment_graph_edges_deleted(count: int = 1, attributes: dict | None = None) -> None:
    """Increment the graph edges deleted counter.

    Args:
        count: Number of edges to add (default: 1)
        attributes: Optional attributes to attach to the metric
    """
    _graph_edges_deleted.add(count, attributes or {})


def increment_graph_nodes_deleted(count: int = 1, attributes: dict | None = None) -> None:
    """Increment the graph nodes deleted counter.

    Args:
        count: Number of nodes to add (default: 1)
        attributes: Optional attributes to attach to the metric
    """
    _graph_nodes_deleted.add(count, attributes or {})


def increment_operation_errors(attributes: dict | None = None) -> None:
    """Increment the operation errors counter.

    Args:
        attributes: Optional attributes to attach to the metric
    """
    _op_errors.add(1, attributes or {})


def increment_cache_hits(attributes: dict | None = None) -> None:
    """Increment the cache hits counter.

    Args:
        attributes: Optional attributes to attach to the metric
    """
    _cache_hits.add(1, attributes or {})


def increment_cache_misses(attributes: dict | None = None) -> None:
    """Increment the cache misses counter.

    Args:
        attributes: Optional attributes to attach to the metric
    """
    _cache_misses.add(1, attributes or {})

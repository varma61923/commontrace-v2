package io.unimem

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.node.ObjectNode

/** Kotlin facade; the same pinned context, redirect policy and JSON contracts. */
class KotlinMemoryClient(url: String, token: String = "", agent: String = "", context: List<String> = emptyList()) {
    private val memory = MemoryClient(url, token, agent, context)
    fun add(text: String): JsonNode = memory.add(text)
    fun search(query: String): JsonNode = memory.search(query)
    fun profile(query: String = ""): JsonNode = memory.profile(query)
    fun reflect(query: String, occasion: String, budget: Int = 600): JsonNode = memory.reflect(query, occasion, budget)
    fun outcome(occasion: String, succeeded: Boolean): JsonNode = memory.outcome(occasion, succeeded)
    fun putRecord(record: ObjectNode): JsonNode = memory.putRecord(record)
    fun searchRecords(query: String = "", limit: Int = 20): JsonNode = memory.searchRecords(query, limit)
    fun forgetRecord(id: String, reason: String): JsonNode = memory.forgetRecord(id, reason)
    fun reapDueRecords(limit: Int = 16): JsonNode = memory.reapDueRecords(limit)
    fun turn(session: String, query: String, options: ObjectNode? = null): JsonNode =
        if (options == null) memory.turn(session, query) else memory.turn(session, query, options)
    fun call(operation: String, arguments: ObjectNode): JsonNode = memory.call(operation, arguments)
}
